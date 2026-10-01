"""逐条出现：分组 / 阅读顺序 / 出现时间 / 渲染（不需要 PowerPoint，图是自己画的）。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "reveal_unit_data"
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
from PIL import Image, ImageDraw  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.models import RevealItem, SlideReveal, Step  # noqa: E402
from backend.services import slide_reveal as R, video  # noqa: E402
from backend.services.slide_reveal import Unit  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


print("\n== 1. 分组和阅读顺序 ==")
# 标题 + 一行三张卡片（卡片里有编号和文字）+ 底部一条横幅 + 页码
units = [
    Unit(0, [0.05, 0.05, 0.6, 0.08], "五件事同步启动", 1),
    Unit(1, [0.05, 0.25, 0.28, 0.4]), Unit(2, [0.07, 0.27, 0.2, 0.05], "专家"),
    Unit(3, [0.07, 0.34, 0.24, 0.2], "面向一个岗位的 AI 助手"),
    Unit(4, [0.36, 0.25, 0.28, 0.4]), Unit(5, [0.38, 0.27, 0.2, 0.05], "技能"),
    Unit(6, [0.67, 0.25, 0.28, 0.4]), Unit(7, [0.69, 0.27, 0.2, 0.05], "连接器"),
    Unit(8, [0.335, 0.43, 0.02, 0.02]),                        # 两张卡片之间的小箭头
    Unit(9, [0.05, 0.72, 0.9, 0.08], "缺一块都不算完整"),
    Unit(10, [0.9, 0.93, 0.05, 0.03], "05"),
]
keep, items = R.group(units)
texts = [" ".join(units[i].text for i in it if units[i].text) for it in items]
check("卡片一行从左到右，横幅最后", texts == ["专家 面向一个岗位的 AI 助手", "技能", "连接器", "缺一块都不算完整"], texts)
check("标题和页码常显", 0 in keep and 10 in keep, keep)
check("两张卡片之间的箭头跟后一张卡片一起出现", 8 in items[1], items)

# 左右两栏：左边三条，右边一个大块 → 先读完左栏
units = [Unit(0, [0.05, 0.05, 0.6, 0.08], "标题", 1)]
for r in range(3):
    units.append(Unit(len(units), [0.05, 0.25 + r * 0.2, 0.4, 0.15], f"左{r + 1}"))
units.append(Unit(len(units), [0.55, 0.25, 0.4, 0.55], "右边"))
_, items = R.group(units)
check("左右两栏：先左栏从上到下，再右栏", [units[it[0]].text for it in items] == ["左1", "左2", "左3", "右边"],
      [units[it[0]].text for it in items])

# 小标题跟它下面的第一条一起出现
units = [Unit(0, [0.05, 0.05, 0.6, 0.08], "标题", 1), Unit(1, [0.05, 0.22, 0.3, 0.04], "专项流程"),
         Unit(2, [0.05, 0.3, 0.25, 0.15], "需求评估"), Unit(3, [0.35, 0.3, 0.25, 0.15], "链路梳理")]
_, items = R.group(units)
check("小标题跟下面第一条一起出现", len(items) == 2 and items[0] == [1, 2], items)

# 一个文本框里的要点（已经按段落拆成 unit）：各自一条，不合并
units = [Unit(0, [0.05, 0.05, 0.6, 0.08], "标题", 1)] + \
        [Unit(1, [0.1, 0.25 + k * 0.08, 0.6, 0.06], f"要点{k}", 2, [k + 1]) for k in range(4)]
_, items = R.group(units)
check("文本框里的要点各自成条", len(items) == 4, items)
check("只有一条时整页一起出现", R.group(units[:2])[1] == [])

print("\n== 2. 出现时间：解说说到哪条 ==")
nar = "下一步，五件事。第一，选定共性案例。第二，确定重点部门。第三，发布红线清单。"
bounds = [{"t": 0.0, "d": 1.5, "text": "下一步，五件事。"}, {"t": 1.6, "d": 2.0, "text": "第一，选定共性案例。"},
          {"t": 3.7, "d": 2.0, "text": "第二，确定重点部门。"}, {"t": 5.8, "d": 2.0, "text": "第三，发布红线清单。"}]
ts = R.reveal_times(["1 选定 3-5 个共性案例", "2 确定首批重点部门", "3 发布连接器红线清单"], nar, bounds, 7.8, 9.0)
check("中文：每条在说到它的那句话开头出现", abs(ts[0] - 1.45) < 0.4 and abs(ts[1] - 3.55) < 0.4 and abs(ts[2] - 5.65) < 0.4,
      [round(x, 2) for x in ts])
nar = "First, open the Finance portal. Then click New report. Finally, submit and wait for approval."
ts = R.reveal_times(["Open the Finance portal and sign in", "Click New report in the top menu",
                     "Submit and wait for approval"], nar, [], 6.0, 7.0)
check("英文（没有逐词时间时按字数估）：顺序和位置合理", ts[0] < 1.0 and ts[0] < ts[1] < ts[2] and ts[2] > 3.0, [round(x, 2) for x in ts])
ts = R.reveal_times(["甲", "乙", "丙"], "", [], 0, 9.0)
check("没有解说：在整段时间里均分", ts[0] < ts[1] < ts[2] <= 9.0 * 0.8, [round(x, 2) for x in ts])
ts = R.reveal_times(["完全不相干的内容", "第二，确定重点部门"], "第一句话。第二，确定重点部门。", [], 4.0, 5.0)
check("找不到的条目放在前后之间，不会晚于后一条", ts[0] <= ts[1], [round(x, 2) for x in ts])
ts = R.reveal_times(["甲条目", "乙条目", "丙条目", "丁条目"], "English narration about something else.", [], 10.0, 12.0)
gaps = [b - a for a, b in zip(ts, ts[1:])]
check("一条都对不上（比如解说翻译成了别的语言）：均匀分布", max(gaps) - min(gaps) < 0.01 and ts[0] < 1.0,
      [round(x, 2) for x in ts])
ts = R.reveal_times(["第一条内容", "对不上一", "对不上二", "第四条内容"], "先讲第一条内容。中间闲聊。最后讲第四条内容。", [], 9.0, 10.0)
gaps = [b - a for a, b in zip(ts, ts[1:])]
check("中间连续几条对不上：在前后两条之间均匀排开", max(gaps) - min(gaps) < 0.01, [round(x, 2) for x in ts])

print("\n== 3. 渲染：该出现时出现、讲到下一条时前面变淡、最后恢复 ==")
proj = storage.create("逐条出现", "zh-CN")
proj.source = "slides"
proj.settings = {"slides_reveal": True, "intro_enabled": False, "outro_enabled": False, "burn_subtitles": False}
shots = storage.screenshots_dir(proj.id)
W, H = 1920, 1080
BG, COL = (245, 245, 245), [(220, 40, 40), (40, 160, 40), (40, 60, 220)]
clean = Image.new("RGB", (W, H), BG)
ImageDraw.Draw(clean).rectangle((100, 60, 1200, 160), fill=(30, 30, 30))      # 标题
full = clean.copy()
boxes = [(150, 300, 1750, 450), (150, 500, 1750, 650), (150, 700, 1750, 850)]
for c, b in zip(COL, boxes):
    ImageDraw.Draw(full).rectangle(b, fill=c)
clean.save(shots / "s_clean.png")
full.save(shots / "s_full.png")
items = []
for k, (c, b) in enumerate(zip(COL, boxes), 1):
    lay = Image.new("RGBA", (b[2] - b[0], b[3] - b[1]), c + (255,))
    lay.save(shots / f"s_r{k}.png")
    items.append(RevealItem(file=f"s_r{k}.png", x=b[0], y=b[1], text=["红色", "绿色", "蓝色"][k - 1]))
nar = "先看红色。再看绿色。最后是蓝色。"
bounds = [{"t": 0.0, "d": 1.8, "text": "先看红色。"}, {"t": 2.0, "d": 1.8, "text": "再看绿色。"},
          {"t": 4.0, "d": 1.8, "text": "最后是蓝色。"}]
audio = storage.audio_dir(proj.id)
audio.mkdir(parents=True, exist_ok=True)
subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000",
                "-t", "5.8", str(audio / "s.mp3")], check=True)
st = Step(kind="slide", screenshot="s_full.png", img_w=W, img_h=H, viewport_w=W, viewport_h=H, zoom=False,
          highlight=False, narration=nar, caption=nar, audio="s.mp3", audio_duration=5.8, boundaries=bounds,
          reveal=SlideReveal(clean="s_clean.png", items=items))
proj.steps = [st]
storage.save(proj)

calls = {"n": 0}
orig_frame = video.StepRenderer.frame


def counting(self, t):
    calls["n"] += 1
    return orig_frame(self, t)


video.StepRenderer.frame = counting
t0 = time.time()
res = video.render_project(storage.load(proj.id), overrides={"video_width": W, "video_height": H, "video_fps": 30})
video.StepRenderer.frame = orig_frame
total_frames = int(round(res["duration"] * 30))
print(f"     渲染 {res['duration']:.1f}s，{time.time() - t0:.1f}s 完成；实际画了 {calls['n']} / {total_frames} 帧")
check("静止的时候复用上一帧（只画动画那几帧）", calls["n"] < total_frames * 0.45, f"{calls['n']} / {total_frames}")
mp4 = storage.output_dir(proj.id) / res["file"]
p = storage.load(proj.id).steps[0]
rend = video.StepRenderer(step=p, screenshot_path=shots / p.screenshot, theme=video.Theme.from_config(
    video.effective_config(storage.load(proj.id))), duration=p.duration, speech_offset=video.speech_lead(p))
x0, y0, x1, y1 = rend.draw_box
f = (x1 - x0) / W
centers = [(int(x0 + (b[0] + b[2]) / 2 * f), int(y0 + (b[1] + b[3]) / 2 * f)) for b in boxes]


def px(t):
    out = SP / "reveal_px.png"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                    str(out)], check=True)
    with Image.open(out) as im:
        im = im.convert("RGB")
        return [im.getpixel(c) for c in centers]


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


times = rend.reveal.times
print("     出现时间", [round(x, 2) for x in times])
v = px(times[0] - 0.3)
check("第一条出现之前：三条都还没出现", all(near(c, BG) for c in v), v)
v = px(times[0] + 0.8)
check("讲到第一条：红色出现，其余还没有", near(v[0], COL[0]) and near(v[1], BG) and near(v[2], BG), v)
v = px(times[1] + 0.8)
dim = tuple(int(BG[i] + (COL[0][i] - BG[i]) * R.FOCUS_ALPHA) for i in range(3))
check("讲到第二条：绿色出现，红色变淡", near(v[1], COL[1]) and near(v[0], dim, 30) and near(v[2], BG), (v, dim))
v = px(res["duration"] - 0.2)
check("最后：全部恢复正常亮度", all(near(c, col) for c, col in zip(v, COL)), v)

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
Hh = {"Origin": "http://127.0.0.1:8756"}
import io  # noqa: E402

pv = Image.open(io.BytesIO(c.get(f"/api/projects/{proj.id}/steps/{st.id}/preview?scale=1&t=0.5").content)).convert("RGB")
check("编辑器预览显示完整页面（全部条目）", all(near(pv.getpixel(cc), col) for cc, col in zip(centers, COL)),
      [pv.getpixel(cc) for cc in centers])

r = c.patch(f"/api/projects/{proj.id}/steps/{st.id}", json={"reveal_enabled": False}, headers=Hh)
check("单页开关能保存", r.status_code == 200 and storage.load(proj.id).steps[0].reveal.enabled is False)
res = video.render_project(storage.load(proj.id), overrides={"video_width": W, "video_height": H, "video_fps": 30})
v = px(0.6)
check("这一页关掉后：一开始就是完整页面", all(near(c_, col) for c_, col in zip(v, COL)), v)

c.patch(f"/api/projects/{proj.id}/steps/{st.id}", json={"reveal_enabled": True}, headers=Hh)
c.patch(f"/api/projects/{proj.id}", json={"settings": {"slides_reveal": False}}, headers=Hh)
res = video.render_project(storage.load(proj.id), overrides={"video_width": W, "video_height": H, "video_fps": 30})
v = px(0.6)
check("整个项目关掉后：也是完整页面", all(near(c_, col) for c_, col in zip(v, COL)), v)

pj = storage.load(proj.id)
pj.settings["slides_reveal"] = True
pj.steps[0].redactions = [__import__("backend.models", fromlist=["Redaction"]).Redaction(x=10, y=10, w=50, h=50)]
storage.save(pj)
rend = video.StepRenderer(step=pj.steps[0], screenshot_path=shots / "s_full.png",
                          theme=video.Theme.from_config(video.effective_config(pj)), duration=6.0)
check("打了码的页不逐条出现（条目图是没打码的原图）", rend.reveal is None)


print("\n== 4. 出现计划：对不上时怎么办、图片跟谁、同一句讲到的几条 ==")
CT = [(0.2, 0.3), (0.2, 0.5), (0.2, 0.7), (0.8, 0.7)]
pl = R.plan(["第一条内容", "第二条内容", "第三条内容", ""], CT, "先讲第一条内容。再讲第二条内容。最后第三条内容。",
            [], 9.0, 10.0)
check("原文对得上：跟着解说出现，突出当前", pl is not None and pl[1] and pl[0][0] < pl[0][1] < pl[0][2],
      pl and [round(x, 2) for x in pl[0]])
check("图片条目跟离它最近的文字条目一起出现", pl is not None and pl[0][3] == pl[0][2], pl and pl[0])
pl = R.plan(["甲条目", "乙条目", "丙条目"], CT[:3], "The narration is in English and says something else.", [], 9.0, 10.0)
check("原文对不上（跨语言）：开头依次快速出现，不突出当前",
      pl is not None and not pl[1] and all(abs(t - (R.FIRST_AT + k * R.CASCADE)) < 1e-6 for k, t in enumerate(pl[0])),
      pl)
pi = R.plan(["", ""], CT[:2], "随便讲讲。", [], 3.0, 4.0)
check("整页只有图片：翻页后一张张快速出现（不突出当前）", pi is not None and not pi[1] and pi[0][0] < pi[0][1] < 2.0, pi)
nar = "Blue comes first. Then green. Red is last."
off = [s[0] for s in R.sentences(nar)]
check("解说切句", len(off) == 3 and nar[off[1]:].startswith("Then"), R.sentences(nar))
pl = R.plan(["红色", "绿色", "蓝色"], CT[:3], nar, [], 6.0, 7.0, align=[off[2], off[1], off[0]])
check("有 AI 对齐：按解说讲的顺序出现（先蓝后绿再红）", pl is not None and pl[1] and pl[0][2] < pl[0][1] < pl[0][0],
      pl and [round(x, 2) for x in pl[0]])
pl = R.plan(["红色", "绿色", "蓝色"], CT[:3], nar, [], 6.0, 7.0, align=[-1, off[1], off[2]])
check("解说没讲到的条目：排在前后之间依次出现（不是一开始就全显示）", pl is not None and R.FIRST_AT <= pl[0][0] < pl[0][1] < pl[0][2], pl)
pn = R.plan(["红色", "绿色"], CT[:2], nar, [], 6.0, 7.0, align=[-1, -1])
check("解说一条都没讲到：依次快速出现、不突出当前", pn is not None and not pn[1] and pn[0][0] < pn[0][1] < 2.0, pn)
pm = R.plan(["一", "二", "三", "四"], CT, nar, [], 6.0, 7.0, align=[off[1], -1, -1, off[2]])
check("中间两条没讲到：排在讲到的两条之间", pm is not None and pm[0][0] < pm[0][1] < pm[0][2] < pm[0][3], pm)
check("英文句号后没空格也能断句", len(R.sentences("It opens the chapter.Nova is fast. Next one.")) == 3,
      R.sentences("It opens the chapter.Nova is fast. Next one."))

bg = Image.new("RGB", (400, 300), BG)
lay = [(Image.new("RGBA", (40, 40), c + (255,)), 50 + 100 * k, 100) for k, c in enumerate(COL)]
an = R.RevealAnim(bg, lay, [1.0, 2.0, 2.0], 6.0, rise=0)
check("同一句讲到的两条：依次错开出现", an.start == [1.0, 2.0, 2.0 + R.STAGGER], an.start)
im = an.compose(3.0)
check("同一拍的两条互不变淡，前一句的那条变淡",
      im.getpixel((170, 120)) == COL[1] and im.getpixel((270, 120)) == COL[2] and im.getpixel((70, 120)) != COL[0],
      [im.getpixel((x, 120)) for x in (70, 170, 270)])
an = R.RevealAnim(bg, lay, [0.0, 2.0, 3.0], 6.0, rise=0)
check("时间为 0 的条目直接画进底图", an.compose(0.1).getpixel((70, 120)) == COL[0] and an.animated == [1, 2])

print("\n== 5. AI 对齐：渲染前自动做，解说没变就不再问 ==")
from backend.services import llm as llm_mod  # noqa: E402
import json as _json  # noqa: E402
import re as _re  # noqa: E402


class FakeAlign:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        prompt = messages[-1]["content"]
        FakeAlign.calls.append(prompt)
        pages = _json.loads(prompt[prompt.rindex("\n\n[") + 2:])
        out = []
        for pg in pages:
            n_s = len(pg["sentences"])
            words = {"红色": "Red", "绿色": "green", "蓝色": "Blue"}
            mp = []
            for it in pg["items"]:
                txt = _re.sub(r"^\d+\.\s*", "", it)
                hit = [j for j, x in enumerate(pg["sentences"], 1) if words.get(txt, "@@") in x]
                mp.append(hit[0] if hit else 0)
            out.append({"page": pg["page"], "map": mp[:len(pg["items"])] if n_s else [0] * len(mp)})
        return {"pages": out}


def wait(j):
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)


pj = storage.load(proj.id)
pj.settings["slides_reveal"] = True
pj.steps[0].redactions = []
pj.steps[0].narration = pj.steps[0].caption = nar          # 英文解说、中文页面
pj.steps[0].boundaries = []
storage.save(pj)
orig_get = llm_mod.get_client
llm_mod.get_client = FakeAlign
job = wait(c.post(f"/api/projects/{proj.id}/render", json={"width": 960, "height": 540, "fps": 15}, headers=Hh).json())
st5 = storage.load(proj.id).steps[0]
check("渲染成功", job["status"] == "done", job.get("error"))
check("对齐结果存进项目（带指纹）", st5.reveal.align == [off[2], off[1], off[0]] and
      st5.reveal.align_key == R.align_key(nar, ["红色", "绿色", "蓝色"]), (st5.reveal.align, st5.reveal.align_key))
check("进度里显示对齐这一步", len(FakeAlign.calls) == 1)
rend = video.StepRenderer(step=st5, screenshot_path=shots / st5.screenshot, theme=video.Theme.from_config(
    video.effective_config(storage.load(proj.id))), duration=st5.duration, speech_offset=video.speech_lead(st5))
tt = rend.reveal.times
check("渲染用上对齐结果：先蓝后绿再红，并突出当前", tt[2] < tt[1] < tt[0] and rend.reveal.focus, [round(x, 2) for x in tt])
wait(c.post(f"/api/projects/{proj.id}/render", json={"width": 960, "height": 540, "fps": 15}, headers=Hh).json())
check("解说没变：再渲染不再问 AI", len(FakeAlign.calls) == 1, len(FakeAlign.calls))
c.patch(f"/api/projects/{proj.id}/steps/{st.id}", json={"narration": "Green first. Then blue. Red is last."}, headers=Hh)
wait(c.post(f"/api/projects/{proj.id}/render", json={"width": 960, "height": 540, "fps": 15}, headers=Hh).json())
st5 = storage.load(proj.id).steps[0]
check("解说改了：重新对齐", len(FakeAlign.calls) == 2 and st5.reveal.align_key == R.align_key(st5.narration, ["红色", "绿色", "蓝色"]),
      (len(FakeAlign.calls), st5.reveal.align))


def no_ai(*a, **k):
    raise llm_mod.LLMError("没配 AI")


llm_mod.get_client = no_ai
c.patch(f"/api/projects/{proj.id}/steps/{st.id}", json={"narration": "Something else entirely."}, headers=Hh)
job = wait(c.post(f"/api/projects/{proj.id}/render", json={"width": 960, "height": 540, "fps": 15}, headers=Hh).json())
check("没配 AI：跳过对齐，照常渲染", job["status"] == "done", job.get("error"))
st5 = storage.load(proj.id).steps[0]
rend = video.StepRenderer(step=st5, screenshot_path=shots / st5.screenshot, theme=video.Theme.from_config(
    video.effective_config(storage.load(proj.id))), duration=st5.duration, speech_offset=video.speech_lead(st5))
check("旧的对齐结果（解说已改）不会被误用：对不上就开头依次出现",
      rend.reveal is not None and not rend.reveal.focus, rend.reveal and rend.reveal.times)
pj = storage.load(proj.id)
pj.steps[0].reveal.enabled = False
check("关掉的页不去对齐（不花 AI 的钱）", not R.needs_align(pj.steps[0]))
llm_mod.get_client = orig_get

print("\n== 6. 翻页：停一拍再开口、字幕跟着晚一拍、上一页淡到下一页（不再闪黑） ==")
proj6 = storage.create("翻页", "zh-CN")
proj6.source = "slides"
proj6.settings = {"slides_reveal": True, "intro_enabled": False, "outro_enabled": False, "burn_subtitles": False}
shots6 = storage.screenshots_dir(proj6.id)
audio6 = storage.audio_dir(proj6.id)
audio6.mkdir(parents=True, exist_ok=True)
steps6 = []
for k, col in enumerate([(230, 30, 30), (30, 30, 230)]):
    Image.new("RGB", (W, H), col).save(shots6 / f"p{k}.png")
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:sample_rate=24000",
                    "-t", "1.5", str(audio6 / f"p{k}.mp3")], check=True)
    steps6.append(Step(kind="slide", screenshot=f"p{k}.png", img_w=W, img_h=H, viewport_w=W, viewport_h=H, zoom=False,
                       highlight=False, narration=f"第 {k + 1} 页。", caption=f"第 {k + 1} 页。", audio=f"p{k}.mp3",
                       audio_duration=1.5, boundaries=[]))
proj6.steps = steps6
storage.save(proj6)
cfg6 = video.effective_config(storage.load(proj6.id))
d0 = video.step_duration(storage.load(proj6.id).steps[0], cfg6)
check("幻灯片时长多出开口前的那一拍", abs(video.speech_lead(steps6[0]) - R.LEAD) < 1e-9 and d0 >= 1.5 + R.LEAD, d0)
res6 = video.render_project(storage.load(proj6.id), overrides={"video_width": 640, "video_height": 360, "video_fps": 30})
mp6 = storage.output_dir(proj6.id) / res6["file"]


def vol(ss, dur):
    out = subprocess.run([shutil.which("ffmpeg"), "-v", "info", "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-i", str(mp6),
                          "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = _re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    return -200.0 if not m or m.group(1) == "-inf" else float(m.group(1))


check("翻页后先静一拍", vol(0.0, 0.25) < -60, vol(0.0, 0.25))
check("然后才开口", vol(0.5, 0.5) > -30, vol(0.5, 0.5))
srt = (storage.output_dir(proj6.id) / res6["srt"]).read_text(encoding="utf-8")
first = _re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d) -->", srt)
t_first = int(first.group(3)) + int(first.group(4)) / 1000 if first else -1
check("字幕跟配音一起晚一拍出现", abs(t_first - R.LEAD) < 0.06, srt[:80])


def frame_at(t):
    out = SP / "reveal_px6.png"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mp6), "-frames:v", "1",
                    str(out)], check=True)
    with Image.open(out) as im:
        return im.convert("RGB").getpixel((320, 180))


a, b, z = frame_at(d0 + 0.07), frame_at(d0 + 0.2), frame_at(d0 + 0.6)
check("翻页瞬间是红蓝过渡，不是黑屏", sum(a) > 200 and a[0] > 60 and a[2] > 60, a)
check("过渡中红色越来越少", b[0] < a[0] and b[2] > a[2], (a, b))
check("翻页完成后是第二页", z[2] > 180 and z[0] < 60, z)

print("\n== 7. 导入和写解说 ==")
from backend.services import slides as slides_mod, script_gen  # noqa: E402

man = {"count": 10}
check("封面：第 1 页、字少", slides_mod._is_cover({"i": 1, "text": "年度计划\n2026 年 9 月"}, man))
check("第 1 页是正文就不算封面", not slides_mod._is_cover({"i": 1, "text": "字" * 200}, man))
check("只有一页的 PPT 不算封面", not slides_mod._is_cover({"i": 1, "text": "标题"}, {"count": 1}))


class RecLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        RecLLM.calls.append(messages)
        idx = [int(x) for x in _re.findall(r'"i": (\d+)', messages[-1]["content"])]
        return {"title": "T", "intro": "", "outro": "",
                "steps": [{"i": i, "title": f"页{i}", "narration": f"第 {i} 页。"} for i in idx]}


pj = storage.load(proj.id)
pj.steps[0].reveal.enabled = True
pj.steps[0].slide_text = "红色 绿色 蓝色"
storage.save(pj)
script_gen.generate_slides_script(storage.load(proj.id), notes_mode="ignore", client=RecLLM())
um = RecLLM.calls[-1][-1]["content"]
check("写解说时把页面条目（阅读顺序）交给 AI", '"content_items"' in um and um.index("红色") < um.index("绿色") < um.index("蓝色"),
      um[-300:])
check("系统提示要求按条目顺序讲", "content_items" in RecLLM.calls[-1][0]["content"])
pj = storage.load(proj.id)
pj.steps[0].reveal.enabled = False
storage.save(pj)
script_gen.generate_slides_script(storage.load(proj.id), notes_mode="ignore", client=RecLLM())
check("关掉逐条出现的页不给条目", '"content_items"' not in RecLLM.calls[-1][-1]["content"])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
