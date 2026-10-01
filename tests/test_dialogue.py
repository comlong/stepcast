"""两人问答：写台词（不给名字、去掉前缀和称呼、不压缩、分批衔接）/ 逐句配音 / 字幕不跨人 / 画面不变 /
编辑台词 / 讲者 / 换语言 / 导出（假 AI、假配音，不联网）。"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "dialogue_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.models import Step  # noqa: E402
from backend.services import dialogue, llm as llm_mod, script_gen, subtitles as subs, tts, video  # noqa: E402

fails = []
FF = shutil.which("ffmpeg")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


# ---- 假配音：按字数生成一段正弦波，记下每句用的音色 ----
USED = []


def fake_synth(text, voice, out_path, rate="", volume="", pitch=""):
    dur = round(0.6 + 0.08 * len(text), 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=400:sample_rate=24000",
                    "-t", str(dur), "-ac", "1", str(out_path)], check=True)
    USED.append((text, voice))
    return tts.ffmpeg_util.probe_duration(out_path), [{"t": 0.0, "d": dur - 0.05, "text": text}]


tts.synth = fake_synth


class FakeLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        u = messages[-1]["content"]
        FakeLLM.calls.append((messages[0]["content"], u))
        if "翻译成" in u:
            items = json.loads(u[u.rindex("\n\n[") + 2:])
            return {"items": [{"k": x["k"], "t": "EN " + x["t"]} for x in items]}
        if "改写要求" in u:
            return {"lines": [{"who": "host", "text": "晓晓：改写后的问题？"}, {"who": "expert", "text": "改写后的回答，晓晓。"}]}
        a = u.index("（JSON）：\n") + len("（JSON）：\n")
        pages = json.loads(u[a:u.index("\n\n严格按以下")])
        en = "English" in u
        steps = []
        for pg in pages:
            i = pg["i"]
            q = f"Host: Q{i} about {pg['slide_title']}?" if en else f"晓晓：第{i}页讲什么？"
            a_ = f"A{i}: it covers {pg['slide_text']}." if en else f"云希：这一页讲{pg['slide_text']}。"
            steps.append({"i": i, "title": f"T{i}", "lines": [{"who": "host", "text": q}, {"who": "讲师", "text": a_},
                                                               {"who": "host", "text": "明白了，云希。" if not en else "Got it."}]})
        return {"title": "问答标题" if not en else "QA Title", "intro": "晓晓：开场白。" if not en else "Welcome.",
                "outro": "再见。" if not en else "Bye.", "steps": steps}


script_gen.get_client = FakeLLM
llm_mod.get_client = FakeLLM

print("\n== 1. 讲者默认值、去掉名字和前缀 ==")
sps = dialogue.default_speakers("zh-CN")
check("中文：主持人晓晓（女声），讲师云希（男声）",
      [(x.role, x.name, x.voice) for x in sps] == [("host", "晓晓", "zh-CN-XiaoxiaoNeural"), ("expert", "云希", "zh-CN-YunxiNeural")],
      [(x.role, x.name, x.voice) for x in sps])
check("英文音色名", dialogue.voice_name("en-US-AndrewMultilingualNeural") == "Andrew" and dialogue.voice_name("en-US-GuyNeural") == "Guy")
N = ["晓晓", "云希", "Aria", "Guy"]
cases = [("晓晓：那 Nova Z 到底是一款什么样的车？", "那 Nova Z 到底是一款什么样的车？"),
         ("云希: 它是首款电动运动轿跑。", "它是首款电动运动轿跑。"),
         ("【主持人】好的，我们继续。", "好的，我们继续。"),
         ("主持人：先看前脸。", "先看前脸。"),
         ("云希，你来讲讲这一块。", "你来讲讲这一块。"),
         ("明白了。晓晓，你说得对。", "明白了。你说得对。"),
         ("对吧，晓晓？", "对吧？"),
         ("Guy: It is fast.", "It is fast."),
         ("Right, Aria.", "Right."),
         ("注意：这里要小心。", "注意：这里要小心。"),
         ("问题：客户最关心什么？", "问题：客户最关心什么？"),
         ("晓晓说得对，这很关键。", "晓晓说得对，这很关键。")]
bad = [(a, dialogue.clean_text(a, N), b) for a, b in cases if dialogue.clean_text(a, N) != b]
check("去掉说话人前缀和称呼；正常的「注意：」「问题：」保留", not bad, bad)

print("\n== 2. 写台词 ==")
proj = storage.create("问答", "zh-CN")
proj.source = "slides"
proj.settings = {"dialogue": True, "slides_notes_mode": "verbatim", "intro_enabled": False, "outro_enabled": False,
                 "zoom_enabled": False, "show_cursor": False, "browser_frame": False, "show_step_badge": False}
proj.speakers = dialogue.default_speakers("zh-CN")
proj.voice = proj.speakers[0].voice
shots = storage.screenshots_dir(proj.id)
steps = []
for k in range(7):
    Image.new("RGB", (1600, 900), (240, 240 - k * 10, 250)).save(shots / f"p{k}.png")
    steps.append(Step(kind="slide", index=k, screenshot=f"p{k}.png", img_w=1600, img_h=900, viewport_w=1600,
                      viewport_h=900, zoom=False, highlight=False, page_title=f"标题{k}", slide_text=f"要点{k}",
                      slide_notes=f"备注{k}"))
proj.steps = steps
storage.save(proj)
FakeLLM.calls.clear()
pj = storage.load(proj.id)
res = script_gen.generate_slides_script(pj, notes_mode="verbatim")
sysmsg, um = FakeLLM.calls[0]
check("备注原文模式也交给 AI 写成对话（带备注）", res["ai"] == 7 and "备注0" in um, res)
check("提示里不给名字（AI 就不会念出来）", not any(n in sysmsg + um for c in FakeLLM.calls for n in ("晓晓", "云希")
                                            for sysmsg, um in [c]))
check("提示要求内容不压缩、按内容块逐块讲、像同事聊天", "不要因为是对话就压缩内容" in sysmsg and "一块一块地讲" in sysmsg
      and "培训播客" in sysmsg and "不要称呼对方的名字" in sysmsg)
check("详细程度给了篇幅", "220~380 字" in um)
check("一次最多 5 页：7 页分两次", len(FakeLLM.calls) == 2 and um.count('"slide_title"') == 5, len(FakeLLM.calls))
check("后一批带上前一页最后几句，接得上", "上一页最后几句" in FakeLLM.calls[1][1] and "明白了。" in FakeLLM.calls[1][1])
check("第一页、最后一页标了位置", '"position": "first"' in FakeLLM.calls[0][1] and '"position": "last"' in FakeLLM.calls[1][1])
s0 = pj.steps[0]
check("台词写进步骤，说话人认得中文别名", [ln.who for ln in s0.lines] == ["host", "expert", "host"], [ln.who for ln in s0.lines])
check("AI 写上的「晓晓：」前缀和称呼都去掉了", s0.narration == "第0页讲什么？\n这一页讲要点0。\n明白了。" and s0.caption == s0.narration,
      s0.narration)
check("片头也去掉名字", pj.intro == "开场白。", pj.intro)
storage.save(pj)

print("\n== 3. 逐句配音 ==")
USED.clear()
pj = storage.load(proj.id)
tts.synth_project(pj)
storage.save(pj)
s0 = pj.steps[0]
by_text = dict(USED)
check("主持人的句子用主持人的音色，讲师的用讲师的",
      by_text["第0页讲什么？"] == "zh-CN-XiaoxiaoNeural" and by_text["这一页讲要点0。"] == "zh-CN-YunxiNeural", by_text)
lt = s0.line_times
check("记下每句的起止时间，接话停顿短", len(lt) == 3 and abs(lt[1][0] - (lt[0][1] + dialogue.GAP)) < 0.02
      and dialogue.GAP <= 0.25, lt)
check("整段配音时长 = 各句 + 停顿", abs(s0.audio_duration - lt[-1][1]) < 0.15, (s0.audio_duration, lt[-1][1]))
cues = subs.segment_to_cues(subs.Segment(0.0, s0.audio_duration, s0.caption, s0.boundaries))
check("一条字幕不跨两个人", [c.text for c in cues] == ["第0页讲什么？", "这一页讲要点0。", "明白了。"], [c.text for c in cues])
check("字幕在这句开口时出现（不提前）", abs(cues[1].start - lt[1][0]) < 0.05, (cues[1].start, lt[1][0]))

print("\n== 4. 画面和单人讲解一样 ==")
cfg = video.effective_config(pj, {"video_width": 1280, "video_height": 720, "burn_subtitles": True})
th = video.Theme.from_config(cfg)
rend = video.StepRenderer(step=s0, screenshot_path=shots / s0.screenshot, theme=th, duration=6.0,
                          speech_offset=video.speech_lead(s0))
plain = Step(kind="slide", screenshot="p0.png", img_w=1600, img_h=900, viewport_w=1600, viewport_h=900, zoom=False,
             highlight=False, narration="单人。")
r2 = video.StepRenderer(step=plain, screenshot_path=shots / "p0.png", theme=th, duration=6.0)
check("幻灯片大小不变（没有两侧头像）", rend.draw_box == r2.draw_box and not hasattr(th, "cast"), rend.draw_box)
out = video.render_project(storage.load(proj.id), overrides={"video_width": 1280, "video_height": 720, "video_fps": 15})
check("渲染成功", (storage.output_dir(proj.id) / out["file"]).exists(), out.get("warning"))
srt = (storage.output_dir(proj.id) / out["srt"]).read_text(encoding="utf-8")
check("字幕里没有名字、一句一条", "晓晓" not in srt and "云希" not in srt and "第0页讲什么？" in srt
      and "第0页讲什么？\n这一页" not in srt)

print("\n== 5. 编辑器：改台词、讲者、AI 改写 ==")
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
sid = s0.id
r = c.patch(f"/api/projects/{proj.id}/steps/{sid}", headers=H,
            json={"lines": [{"who": "expert", "text": "直接讲。"}, {"who": "host", "text": "  "}, {"who": "host", "text": "好的？"}]})
st = storage.load(proj.id).steps[0]
check("改台词：空句去掉，解说和字幕跟着，旧配音作废", r.status_code == 200 and [ln.who for ln in st.lines] == ["expert", "host"]
      and st.narration == "直接讲。\n好的？" and st.caption == st.narration and st.audio == "" and st.line_times == [],
      (st.narration, st.audio))
r = c.post(f"/api/projects/{proj.id}/steps/{sid}/tts", headers=H, json={})
st = storage.load(proj.id).steps[0]
check("单步重新合成也逐句配音", r.status_code == 200 and len(st.line_times) == 2, r.text[:100])
r = c.put(f"/api/projects/{proj.id}/speakers", headers=H,
          json={"speakers": [{"role": "expert", "name": "张老师", "voice": "zh-CN-YunjianNeural"}]})
pj = storage.load(proj.id)
check("换讲师音色：讲师说过话的页配音作废，名字改了", r.status_code == 200 and pj.speakers[1].name == "张老师"
      and pj.speakers[1].voice == "zh-CN-YunjianNeural" and pj.steps[1].audio == "", r.text[:200])
tts.synth_project(pj)
storage.save(pj)
r = c.put(f"/api/projects/{proj.id}/speakers", headers=H, json={"speakers": [{"role": "host", "name": "小王"}]})
pj = storage.load(proj.id)
check("只改名字不动配音", pj.speakers[0].name == "小王" and all(s.audio for s in pj.steps), [s.audio for s in pj.steps])
r = c.post(f"/api/projects/{proj.id}/speakers/host/avatar", headers=H, files={"file": ("a.png", b"x", "image/png")})
check("头像接口已经去掉", r.status_code in (404, 405))
r = c.post(f"/api/projects/{proj.id}/steps/{sid}/rewrite", headers=H, json={"instruction": "更短"})
st = storage.load(proj.id).steps[0]
check("AI 改写整段对话，名字和前缀去掉", r.status_code == 200 and st.narration == "改写后的问题？\n改写后的回答。"
      and "小王" not in FakeLLM.calls[-1][1] and "张老师" not in FakeLLM.calls[-1][1], (r.text[:200], st.narration))
md = c.get(f"/api/projects/{proj.id}/export/markdown").text
check("导出文档：名字：台词", "**小王**：改写后的问题？" in md and "**张老师**：改写后的回答。" in md, md[:300])
js = c.get(f"/api/projects/{proj.id}/export/script").json()
check("导出脚本带台词", js["steps"][0]["lines"][0]["who"] == "host")
js["steps"][0]["lines"] = [{"who": "expert", "text": "导入的台词。"}]
c.post(f"/api/projects/{proj.id}/import/script", headers=H, json=js)
st = storage.load(proj.id).steps[0]
check("导入脚本里的台词", [ln.text for ln in st.lines] == ["导入的台词。"] and st.narration == "导入的台词。")

print("\n== 6. 换语言：讲者换成英文音色，按 PPT 原文重写成英文对话 ==")
r = c.post(f"/api/projects/{proj.id}/translate", headers=H, json={"target_language": "en-US", "voice": "", "apply": True})
jid = r.json()["id"]
while c.get(f"/api/jobs/{jid}").json()["status"] in ("pending", "running"):
    time.sleep(0.2)
j = c.get(f"/api/jobs/{jid}").json()
pj = storage.load(proj.id)
check("换语言成功", j["status"] == "done", j.get("error"))
check("讲者换成英文音色；自己起的名字保留",
      [(x.name, x.voice) for x in pj.speakers] == [("小王", "en-US-AriaNeural"), ("张老师", "en-US-GuyNeural")],
      [(x.name, x.voice) for x in pj.speakers])
check("台词按原文用英文重写成对话，Host: 前缀去掉", pj.steps[1].lines[0].text.startswith("Q1") and pj.language == "en-US",
      pj.steps[1].narration)

print("\n== 7. 普通项目不受影响 ==")
p2 = storage.create("普通", "zh-CN")
p2.source = "slides"
Image.new("RGB", (1600, 900), (200, 200, 200)).save(storage.screenshots_dir(p2.id) / "a.png")
p2.steps = [Step(kind="slide", screenshot="a.png", img_w=1600, img_h=900, viewport_w=1600, viewport_h=900,
                 narration="单人讲解。", caption="单人讲解。")]
storage.save(p2)
USED.clear()
tts.synth_project(p2)
check("单人项目：整段一次合成", len(USED) == 1 and p2.steps[0].line_times == [])
FakeLLM.calls.clear()
script_gen.generate_slides_script(p2, notes_mode="ignore", client=FakeLLM())
check("单人项目：还是单人解说的提示", FakeLLM.calls and "两人" not in FakeLLM.calls[0][0])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
