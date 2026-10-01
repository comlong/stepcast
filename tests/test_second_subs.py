"""Second-language subtitles: main subtitles recorded at render time, room left for the second subtitle, sentence merging, AI translation (fake), VTT/SRT, outdated tracks, deletion, web player package, old videos."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "second_subs_data"
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
from backend.services import llm as llm_mod, second_subs, tts, video  # noqa: E402
from backend.services.renderer import Theme, draw_subtitle  # noqa: E402

fails = []
FF = shutil.which("ffmpeg")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def fake_synth(text, v, out, rate="", volume="", pitch=""):
    dur = round(0.8 + 0.12 * len(text), 2)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:sample_rate=24000", "-t", str(dur), "-ac", "1",
                    str(out)], check=True)
    return tts.ffmpeg_util.probe_duration(out), [{"t": 0.0, "d": dur - 0.05, "text": text}]


tts.synth = fake_synth


class FakeLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        u = messages[-1]["content"]
        FakeLLM.calls.append(u)
        items = json.loads(u[u.index("\n\n") + 2:])
        lang = "EN" if "English" in u else "DE"
        out = []
        for it in items:
            if lang == "DE" and it["i"] == 1 and len(FakeLLM.calls) == 1:
                continue                                              # drop one sentence the first time to see whether it is asked again
            t = f"[{lang}] " + it["t"]
            if "长句" in it["t"]:
                t = f"[{lang}] " + "This is a very long translated sentence, with a comma in the middle, " * 2
            out.append({"i": it["i"], "t": t})
        return {"items": out}


llm_mod.get_client = FakeLLM
second_subs.get_client = FakeLLM

print("\n== 1. 给第二字幕留位置 ==")
th = Theme.from_config({"video_width": 1280, "video_height": 720, "burn_subtitles": True})
th2 = Theme.from_config({"video_width": 1280, "video_height": 720, "burn_subtitles": True, "second_sub_space": False})


def sub_box(theme):
    """Top and bottom edge of a one-line main subtitle box (fraction of the frame height)"""
    img = Image.new("RGB", (1280, 720), (0, 0, 0))
    draw_subtitle(img, "主字幕测试", theme)
    ys = [y for y in range(720) if img.getpixel((640, y)) != (0, 0, 0)]
    return min(ys) / 720, max(ys) / 720


def sub_bottom(theme):
    return sub_box(theme)[1]


check("默认留位置：主字幕底边在约 93.5%，下面一行给第二字幕", th.sub_space and 0.925 < sub_bottom(th) < 0.94, sub_bottom(th))
check("关掉：主字幕更靠下（约 95.5%）", 0.945 < sub_bottom(th2) < 0.96, sub_bottom(th2))
st = Step(kind="slide", screenshot="x.png", img_w=1600, img_h=900, viewport_w=1600, viewport_h=900)
shots = DATA / "shots"
shots.mkdir()
Image.new("RGB", (1600, 900), (200, 200, 200)).save(shots / "x.png")
b1 = video.StepRenderer(step=st, screenshot_path=shots / "x.png", theme=th, duration=3).draw_box
b2 = video.StepRenderer(step=st, screenshot_path=shots / "x.png", theme=th2, duration=3).draw_box
top, bot = sub_box(th)
check("主字幕底框紧贴文字：一行高约 5.5%（以前 7.7%）", 0.05 < bot - top < 0.06, round(bot - top, 4))
check("幻灯片上沿离画面顶边只留约 1.2%", b1[1] <= 720 * 0.015 and b2[1] <= 720 * 0.015, (b1, b2))
check("幻灯片下沿到约 87.5%（关掉留位置时更大），一行主字幕不压到页面", 720 * 0.87 < b1[3] <= 720 * 0.876 and b1[3] < b2[3]
      and b1[3] <= 720 * top + 1 and b2[3] <= 720 * sub_box(th2)[0] + 1, (b1, b2, top))

print("\n== 2. 渲染时记下主字幕 ==")
proj = storage.create("第二字幕", "zh-CN")
proj.source = "slides"
proj.title = "第二字幕测试"
proj.settings = {"intro_enabled": False, "outro_enabled": False, "zoom_enabled": False, "show_cursor": False,
                 "browser_frame": False, "show_step_badge": False}
ss = storage.screenshots_dir(proj.id)
texts = ["第一页讲的是背景，这里先说明为什么要做这件事。然后说明目标。",
         "这是一个长句，后面的译文会很长需要拆成两条字幕来显示。最后一句。"]
for k, tx in enumerate(texts):
    Image.new("RGB", (1600, 900), (230, 230, 240)).save(ss / f"p{k}.png")
    proj.steps.append(Step(kind="slide", index=k, screenshot=f"p{k}.png", img_w=1600, img_h=900, viewport_w=1600,
                           viewport_h=900, zoom=False, highlight=False, narration=tx, caption=tx))
storage.save(proj)
pj = storage.load(proj.id)
tts.synth_project(pj)
storage.save(pj)
res = video.render_project(storage.load(proj.id), overrides={"video_width": 640, "video_height": 360, "video_fps": 10})
pj = storage.load(proj.id)
pj.output = res["file"]
storage.save(pj)
out_dir = storage.output_dir(proj.id)
cj = json.loads((out_dir / "第二字幕测试.cues.json").read_text(encoding="utf-8"))
check("主字幕时间轴和位置存成 视频名.cues.json", cj["video"] == res["file"] and cj["space"] is True and cj["burned"] is True
      and cj["bottom"] == 0.935 and len(cj["cues"]) >= 4, (cj["video"], cj.get("bottom"), len(cj["cues"])))

print("\n== 3. 按句合并 ==")
sents = second_subs.group_sentences(cj["cues"])
check("主字幕的半句合并成整句", [x["t"] for x in sents][:2] == ["第一页讲的是背景，这里先说明为什么要做这件事。", "然后说明目标。"],
      [x["t"] for x in sents])
check("每句用它在视频里的时间", sents[0]["s"] == cj["cues"][0][0] and all(a["e"] <= b["s"] + 1e-6 for a, b in zip(sents, sents[1:])))
en = second_subs.group_sentences([[0, 1, "Open the page,"], [1, 2, "then click Save."], [2.1, 3, "Done"], [5, 6, "Next one."]])
check("西文按空格拼，停顿长也断开", [x["t"] for x in en] == ["Open the page, then click Save.", "Done", "Next one."], en)

print("\n== 4. 生成（接口 + 后台任务）==")
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
st0 = c.get(f"/api/projects/{proj.id}/subtitles2").json()
check("状态：有主字幕、已烧录、留了位置、还没有第二字幕", st0["has_primary"] and st0["burned"] and st0["space"]
      and not st0["legacy"] and st0["tracks"] == [], st0)
r = c.post(f"/api/projects/{proj.id}/subtitles2", headers=H, json={"languages": ["zh-CN"]})
check("和视频同一种语言（简体中文）不行", r.status_code == 400, r.text[:100])
jid = c.post(f"/api/projects/{proj.id}/subtitles2", headers=H, json={"languages": ["en-US", "de-DE"]}).json()["id"]
while c.get(f"/api/jobs/{jid}").json()["status"] in ("pending", "running"):
    time.sleep(0.2)
j = c.get(f"/api/jobs/{jid}").json()
check("生成成功", j["status"] == "done", j.get("error"))
st1 = j["result"]
check("两种语言都有，都没过期", [(x["lang"], x["stale"]) for x in st1["tracks"]] == [("de-DE", False), ("en-US", False)],
      st1["tracks"])
vtt = (out_dir / "第二字幕测试.en-US.vtt").read_text(encoding="utf-8")
check("VTT：放在最底下一行、字号小一些", vtt.startswith("WEBVTT") and "::cue" in vtt and "line:-1" in vtt, vtt[:200])
srt = (out_dir / "第二字幕测试.de-DE.srt").read_text(encoding="utf-8")
check("SRT 也有", "[DE] 第一页讲的是背景" in srt)
check("漏翻的句子补问了一次", "[DE] 然后说明目标。" in srt, srt[:400])
check("第二字幕默认斜体：VTT / SRT 里用 <i>", "<i>[EN]" in vtt and "<i>[DE] 第一页讲的是背景" in srt, vtt[200:400])
from backend.services import second_subs as SS  # noqa: E402
check("中文、日文等也斜体，只有阿拉伯文不斜", SS.italic("en-US") and SS.italic("sv-SE") and SS.italic("zh-CN")
      and SS.italic("zh-TW") and SS.italic("ja-JP") and not SS.italic("ar-SA"))
SS.write_srt([[0.1, 2.0, "一"], [2.0, 4.5, "二"]], DATA / "lead.srt", "zh-CN")
SS.write_vtt([[0.1, 2.0, "一"], [2.0, 4.5, "二"]], DATA / "lead.vtt", "zh-CN")
srt_l, vtt_l = (DATA / "lead.srt").read_text(encoding="utf-8"), (DATA / "lead.vtt").read_text(encoding="utf-8")
check("SRT（本地播放器）整条提前 0.25 秒、不叠在一起；VTT（网页）时间不变",
      "00:00:00,000 --> 00:00:01,750" in srt_l and "00:00:01,750 --> 00:00:04,250" in srt_l and "<i>一</i>" in srt_l
      and "00:00:00.100 --> 00:00:02.000" in vtt_l and "00:00:02.000 --> 00:00:04.500" in vtt_l, srt_l)
check("时间格式：毫秒进位不丢一秒", [SS._fmt(t, ",") for t in (3.9996, 59.9999, 1.234)]
      == ["00:00:04,000", "00:01:00,000", "00:00:01,234"])
en = "This demo shows you how to create a new organization in the sample admin panel in just a few steps."
parts = SS._split(en, 0.0, 6.552, 96, [4.293])
check("长译文拆两条时，在主字幕换条的那一刻一起换", [p[:2] for p in parts] == [[0.0, 4.293], [4.293, 6.552]], parts)
check("附近没有主字幕换条：按字数分", SS._split(en, 0.0, 6.552, 96, [1.0])[0][1] != 1.0)
g = SS.group_sentences([[0.0, 4.2, "第一段，"], [4.2, 6.5, "第二段。"], [6.6, 7.4, "对。"], [7.5, 12.0, "下一句说得长一点。"]])
check("合并句子时记下中间主字幕换条的时刻", [x["cuts"] for x in g] == [[4.2], [7.5]], g)
th = "สไลด์นี้อธิบายว่าระบบใหม่ทำงานอย่างไรและทีมงานสามารถช่วยลูกค้าได้อย่างไร" * 2
import unicodedata  # noqa: E402
check("泰文这类没有空格的长句：不从声调 / 元音符号前面切开",
      all(unicodedata.category(p[2][0])[0] != "M" for p in SS._split(th, 0, 6, 38)))
SS.write_vtt([[0, 1, "A & B <c>"]], DATA / "esc.vtt", "en-US")
check("VTT 里 & < > 转义", "<i>A &amp; B &lt;c&gt;</i>" in (DATA / "esc.vtt").read_text(encoding="utf-8"))
cues = c.get(f"/api/projects/{proj.id}/subtitles2/en-US").json()["cues"]
long = [x for x in cues if "very long" in x[2]]
check("译文太长拆成两条，时间按字数分", len(long) == 2 and abs(long[0][1] - long[1][0]) < 0.01 and all(len(x[2]) <= 96 for x in long),
      long)
first = next(x for x in cues if "背景" in x[2])
check("第一句的时间 = 主字幕里这句的时间", abs(first[0] - sents[0]["s"]) < 0.01 and abs(first[1] - sents[0]["e"]) < 0.01)
check("翻译时提示里说清楚源语言和目标语言", "简体中文" in FakeLLM.calls[0] and "English" in FakeLLM.calls[0])

print("\n== 5. 网页播放包 ==")
r = c.get(f"/api/projects/{proj.id}/export/player")
check("下载 zip", r.status_code == 200 and r.headers["content-type"] == "application/zip", r.status_code)
z = zipfile.ZipFile(io.BytesIO(r.content))
names = set(z.namelist())
check("包里有播放页、视频、两种字幕", {"index.html", res["file"], "第二字幕测试.en-US.vtt", "第二字幕测试.de-DE.srt"} <= names, names)
page = z.read("index.html").decode("utf-8")
check("播放页引用视频（文件名转义）、内嵌两种字幕、默认不显示、第二字幕贴着主字幕", "%E7%AC%AC%E4%BA%8C" in page
      and "const BOTTOM = 0.935;" in page and '"en-US"' in page
      and '<option value="">&mdash;</option>' in page and "[EN]" in page, page[:300])
check("播放页：?t= 在视频信息已经到了时也能跳到那里", "if (v.readyState >= 1) v.currentTime = t0;" in page)
check("播放页：第二字幕默认斜体、底框紧挨着主字幕", "font-style: italic" in page and "const GAP = 0;" in page
      and "UPRIGHT" in page)
script = page[page.index("<script>") + 8:page.rindex("</script>")]
(DATA / "player.js").write_text(script, encoding="utf-8")
chk = subprocess.run(["node", "--check", str(DATA / "player.js")], capture_output=True, text=True)
check("播放页的脚本语法没问题", chk.returncode == 0, chk.stderr[:200])

print("\n== 6. 重新渲染后过期；删除 ==")
c.patch(f"/api/projects/{proj.id}/steps/{pj.steps[0].id}", headers=H, json={"narration": "改过的解说。", "caption": "改过的解说。"})
pj = storage.load(proj.id)
tts.synth_project(pj)
storage.save(pj)
video.render_project(storage.load(proj.id), overrides={"video_width": 640, "video_height": 360, "video_fps": 10})
st2 = c.get(f"/api/projects/{proj.id}/subtitles2").json()
check("字幕改了、重新渲染：之前的第二字幕标成过期", all(x["stale"] for x in st2["tracks"]), st2["tracks"])
z2 = zipfile.ZipFile(io.BytesIO(c.get(f"/api/projects/{proj.id}/export/player").content))
check("播放包不放过期的字幕", not any(n.endswith(".vtt") for n in z2.namelist()), z2.namelist())
st3 = c.delete(f"/api/projects/{proj.id}/subtitles2/de-DE", headers=H).json()
check("删除：记录和文件都没了", [x["lang"] for x in st3["tracks"]] == ["en-US"]
      and not (out_dir / "第二字幕测试.de-DE.vtt").exists())

print("\n== 7. 老视频（没有 .cues.json）==")
(out_dir / "第二字幕测试.cues.json").unlink()
st4 = c.get(f"/api/projects/{proj.id}/subtitles2").json()
check("读 .srt，标成「没留位置」提醒重新渲染；第二字幕放在老位置（94.5%）下面", st4["has_primary"] and st4["legacy"]
      and not st4["space"] and st4["bottom"] == 0.945, st4)
prim = second_subs.load_primary(storage.load(proj.id))
check("SRT 解析出时间和文字", prim["cues"][0][2] == "改过的解说。" and prim["cues"][0][1] > prim["cues"][0][0], prim["cues"][:2])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
