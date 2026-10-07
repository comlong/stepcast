"""Slides with the author's animations, with the local PowerPoint: a deck is built and animated by PowerPoint itself (click / with previous / after previous,
a build by paragraph, an exit, a video followed by callouts, a card that comes in behind text that is already there, a wipe from the top, a fly-in from a
corner, a dot that appears and then moves along a path), imported, voiced and rendered.

Starts PowerPoint, so it only runs while PowerPoint isn't open (run_all.py --ppt checks first) and quits only the instance it started."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "anim_ppt_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ.pop("VT_DISABLE_POWERPOINT", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh", "language": "en-US", "voice": "en-US-JennyNeural"})
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import llm, slide_reveal as R, slide_sequence, slides, video  # noqa: E402


def _no_ai(*a, **k):                 # tests never spend AI credits
    raise llm.LLMError("test: no AI")


llm.get_client = _no_ai
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
FFMPEG = shutil.which("ffmpeg")
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def wait(j, timeout=1200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.3)
    raise TimeoutError


def pp_running(wait_s=15):
    for _ in range(wait_s * 2):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"], capture_output=True, text=True).stdout
        if "POWERPNT" not in out.upper():
            return False
        time.sleep(0.5)
    return True


check("本机 PowerPoint 可用", slides.powerpoint_available())
was_running = pp_running(wait_s=0)

print("\n== 1. 用 PowerPoint 做一份带动画的 PPT ==")
RGB = lambda r, g, b: r + g * 256 + b * 65536            # noqa: E731
PANEL, TITLE, CARD, BADGE = (235, 240, 250), (30, 60, 120), (40, 120, 200), (230, 90, 40)
NOTE, CALL_A, CALL_B, YELLOW, INK = (200, 60, 60), (220, 120, 0), (0, 150, 100), (255, 230, 120), (20, 20, 20)
DECK = SP / "anim_deck.pptx"


def build_deck():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("PowerPoint.Application")
    try:
        pres = app.Presentations.Add(WithWindow=False)
        pres.PageSetup.SlideWidth, pres.PageSetup.SlideHeight = 960, 540

        def shape(sl, kind, x, y, w, h, color, text="", name="", fg=(255, 255, 255)):
            sh = sl.Shapes.AddShape(kind, x, y, w, h)
            sh.Fill.ForeColor.RGB = RGB(*color)
            sh.Line.Visible = 0
            sh.Name = name
            if text:
                sh.TextFrame2.TextRange.Text = text
                sh.TextFrame2.TextRange.Font.Fill.ForeColor.RGB = RGB(*fg)
            return sh

        blank = pres.SlideMaster.CustomLayouts(7)
        s1 = pres.Slides.AddSlide(1, blank)
        q = s1.TimeLine.MainSequence
        shape(s1, 1, 40, 120, 880, 380, PANEL, name="panel")
        shape(s1, 1, 40, 20, 880, 70, TITLE, "Title", "title")
        card = shape(s1, 5, 60, 150, 400, 120, CARD, name="card1")
        txt = s1.Shapes.AddTextbox(1, 80, 170, 360, 80)
        txt.TextFrame2.TextRange.Text = "Card one text"
        txt.TextFrame2.TextRange.Font.Fill.ForeColor.RGB = RGB(255, 255, 255)
        badge = shape(s1, 9, 420, 135, 50, 50, BADGE, "1", "badge1")
        q.AddEffect(card, 10, 0, 1)
        q.AddEffect(txt, 10, 0, 2)
        q.AddEffect(badge, 1, 0, 3)
        bl = s1.Shapes.AddTextbox(1, 500, 150, 400, 200)
        bl.TextFrame2.TextRange.Text = "First point\rSecond point\r  sub of second\rThird point"
        bl.TextFrame2.TextRange.Paragraphs.Item(3).ParagraphFormat.IndentLevel = 2
        q.AddEffect(bl, 2, 2, 1)
        note = shape(s1, 1, 60, 300, 400, 60, NOTE, "Gone later", "note")
        q.AddEffect(note, 10, 0, 1)
        xe = q.AddEffect(note, 10, 0, 3)
        xe.Exit = -1
        xe.Timing.TriggerDelayTime = 1.5

        s2 = pres.Slides.AddSlide(2, blank)
        q2 = s2.TimeLine.MainSequence
        vid = s2.Shapes.AddMediaObject2(str((SP / "vid" / "clip10.mp4").resolve()), False, True, 200, 100, 480, 270)
        ca = shape(s2, 5, 230, 120, 260, 50, CALL_A, "Callout A", "callA")
        cb = shape(s2, 5, 230, 300, 260, 50, CALL_B, "Callout B", "callB")
        q2.AddEffect(vid, 83, 0, 3)
        q2.AddEffect(ca, 10, 0, 3)
        q2.AddEffect(cb, 10, 0, 1)

        s3 = pres.Slides.AddSlide(3, blank)
        q3 = s3.TimeLine.MainSequence
        yellow = shape(s3, 1, 100, 150, 600, 200, YELLOW, name="card")                       # at the back
        ink = shape(s3, 1, 200, 200, 400, 100, INK, "Hello", "ink")                          # in front of it
        q3.AddEffect(ink, 10, 0, 1)                      # the text comes first
        q3.AddEffect(yellow, 10, 0, 1)                   # the card behind it comes in afterwards

        s4 = pres.Slides.AddSlide(4, blank)
        q4 = s4.TimeLine.MainSequence
        shape(s4, 1, 0, 0, 960, 540, PANEL, name="bg4")
        wcard = shape(s4, 1, 80, 80, 300, 120, CARD, "Wiped in", "wiped")
        q4.AddEffect(wcard, 22, 0, 1).EffectParameters.Direction = 1        # wipe, from the top (msoAnimDirectionUp)
        fcard = shape(s4, 1, 560, 300, 300, 120, CALL_B, "Flown in", "flown")
        q4.AddEffect(fcard, 2, 0, 1).EffectParameters.Direction = 6         # fly in, from the top-left corner (msoAnimDirectionUpLeft)
        dot = shape(s4, 9, 100, 380, 40, 40, BADGE, name="dot")
        q4.AddEffect(dot, 1, 0, 1)                                          # the dot appears on a click …
        mv = q4.AddEffect(dot, 149, 0, 3)                                   # … then moves right along a straight path (msoAnimEffectPathRight)
        mv.Timing.Duration = 2.0
        pres.SaveAs(str(DECK.resolve()))
        pres.Close()
    finally:
        try:
            while app.Presentations.Count:
                app.Presentations(1).Saved = -1
                app.Presentations(1).Close()
            app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()


build_deck()
check("PPT 做好了", DECK.exists())
pp_running()

print("\n== 2. 导入：按 PPT 的动画拆成层 ==")
with open(DECK, "rb") as f:
    man = wait(c.post("/api/import/slides", files={"file": ("anim_deck.pptx", f)}, headers=H).json())["result"]
body = {"notes_mode": "ignore", "missing": "empty", "language": "en-US", "auto_voice": False, "reveal": True}
t0 = time.time()
r = wait(c.post(f"/api/import/slides/{man['id']}/create", json=body, headers=H).json())
print(f"     导入用了 {time.time() - t0:.1f} 秒")
check("导入成功", r["status"] == "done", r.get("error"))
pid = r["result"]["project_id"]
p = c.get(f"/api/projects/{pid}").json()
kinds = [s["kind"] for s in p["steps"]]
check("四页 + 一个视频步骤", kinds == ["slide", "slide", "video", "slide", "slide"], kinds)
s1, s2, v2, s3, s4 = p["steps"]
rv1, rv2, rv3, rv4 = s1["reveal"], s2["reveal"], s3["reveal"], s4["reveal"]
check("四页都是按动画拆的", all(rv and rv["mode"] == "timeline" for rv in (rv1, rv2, rv3, rv4)), [rv and rv["mode"] for rv in (rv1, rv2, rv3, rv4)])
check("封面规则不影响有动画的页：都启用逐条出现", all(rv["enabled"] for rv in (rv1, rv2, rv3)))
beats1 = sorted({it["beat"] for it in rv1["items"] if it["beat"] >= 0})
check("第 1 页五步：卡片组、三个要点步骤（子要点同一步）、会消失的提示", beats1 == [0, 1, 2, 3, 4], beats1)
texts1 = {it["text"]: it for it in rv1["items"] if it["text"]}
check("卡片、文字同一步，角标晚 0.5 秒且是「出现」", texts1["Card one text"]["beat"] == 0 and texts1["1"]["beat"] == 0
      and abs(texts1["1"]["offset"] - 0.5) < 0.01 and texts1["1"]["anim"] == "appear", {k: (v["beat"], v["offset"]) for k, v in texts1.items()})
check("要点按段落各一层，子要点和「Second point」同一步", texts1["First point"]["beat"] == 1 and texts1["Second point"]["beat"] == 2
      and texts1["sub of second"]["beat"] == 2 and texts1["Third point"]["beat"] == 3)
check("提示：第 5 步出现，2 秒后消失", texts1["Gone later"]["beat"] == 4 and abs(texts1["Gone later"]["exit_offset"] - 2.0) < 0.01
      and texts1["Gone later"]["exit_beat"] == 4)
static = [it for it in rv1["items"] if it["beat"] < 0]
check("没有动画的（底板、标题）是静态层，排在最下面", len(static) == 1 and rv1["items"][0]["beat"] < 0, [it["beat"] for it in rv1["items"]])
check("第 2 页：视频自己一层，两条提示都在视频之后", sum(1 for it in rv2["items"] if it["media"]) == 1
      and [it["after_media"] for it in rv2["items"] if it["text"]] == [True, True], [(it["text"], it["after_media"]) for it in rv2["items"]])
check("作者让提示在视频之后出现：自动选了「先播视频」", s2["sequence"] == "video_first", s2["sequence"])
check("第 1、3 页没有视频：保持默认顺序", s1["sequence"] == "" and s3["sequence"] == "")
z3 = [(it["text"], it["beat"]) for it in rv3["items"]]
check("第 3 页：文字在黄色底板前面，文字先出现（第 1 步），底板后出现（第 2 步）", len(z3) == 2 and [b for _, b in z3] == [1, 0], z3)
t4 = {it["text"]: it for it in rv4["items"] if it["text"]}
dot4 = [it for it in rv4["items"] if it["motion_path"]]
check("第 4 页：擦除读出了方向「从上」（PowerPoint 的接口不给，从文件里读）", t4["Wiped in"]["anim"] == "wipe" and t4["Wiped in"]["side"] == "top",
      (t4["Wiped in"]["anim"], t4["Wiped in"]["side"]))
check("飞入读出了「从左上角」", t4["Flown in"]["anim"] == "fly" and t4["Flown in"]["side"] == "top-left", (t4["Flown in"]["anim"], t4["Flown in"]["side"]))
check("光点：「出现」+ 同一步里接着沿路径右移 2 秒", len(dot4) == 1 and dot4[0]["anim"] == "appear" and dot4[0]["motion_beat"] == dot4[0]["beat"] == 2
      and abs(dot4[0]["motion_dur"] - 2.0) < 0.01 and R.parse_path(dot4[0]["motion_path"])[-1][0] > 0.2, dot4 and {k: dot4[0][k] for k in ("anim", "beat",
      "motion_beat", "motion_dur", "motion_path")})

print("\n== 3. 把所有层按 PPT 的上下顺序叠回去，和 PowerPoint 导出的整页一样 ==")
shots = storage.screenshots_dir(pid)
for n, s in ((1, s1), (2, s2), (3, s3), (4, s4)):
    pv = Image.open(__import__("io").BytesIO(c.get(f"/api/projects/{pid}/steps/{s['id']}/preview?scale=1&t=0.5").content)).convert("RGB")
    ref = Image.open(shots / s["screenshot"]).convert("RGB")
    # the preview is the page drawn into the video frame: compare the page area only
    th = video.Theme.from_config(video.effective_config(storage.load(pid)))
    box, _ = video.StepRenderer.layout_box(storage.load(pid).steps[[x["id"] for x in p["steps"]].index(s["id"])], shots / s["screenshot"], th)
    crop = pv.crop(box).resize(ref.size, Image.LANCZOS)
    d = np.abs(np.asarray(crop, np.float32) - np.asarray(ref, np.float32)).mean()
    check(f"第 {n} 页：叠好的整页和 PowerPoint 自己导出的几乎一样", d < 3.0, f"平均相差 {d:.2f}")

print("\n== 4. 配上解说，渲染，看每一步的画面 ==")
audio = storage.audio_dir(pid)
audio.mkdir(parents=True, exist_ok=True)
NARR = {
    s1["id"]: ("First, Card one text. Then First point. Then Second point and sub of second. Then Third point. Finally Gone later.",
               ["First, Card one text.", "Then First point.", "Then Second point and sub of second.", "Then Third point.", "Finally Gone later."]),
    s2["id"]: ("Callout A says one thing. Callout B says another.", ["Callout A says one thing.", "Callout B says another."]),
    s3["id"]: ("First the Hello text. Then its yellow card.", ["First the Hello text.", "Then its yellow card."]),
    s4["id"]: ("First the wiped in card. Then the flown in card. Then the dot.", ["First the wiped in card.", "Then the flown in card.", "Then the dot."]),
}
sent_len = 2.0


def set_audio(proj):
    proj.settings.update({"intro_enabled": False, "outro_enabled": False, "burn_subtitles": False})
    for st in proj.steps:
        if st.id in NARR:
            text, sents = NARR[st.id]
            dur = sent_len * len(sents) + 0.5
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000", "-t", f"{dur}",
                            str(audio / f"{st.id}.mp3")], check=True)
            st.narration = st.caption = text
            st.audio, st.audio_duration = f"{st.id}.mp3", dur
            st.boundaries = [{"t": k * sent_len, "d": sent_len - 0.1, "text": s} for k, s in enumerate(sents)]


storage.update(pid, set_audio)
OV = {"video_width": 1280, "video_height": 720, "video_fps": 30}
res = video.render_project(storage.load(pid), overrides=OV)
mp4 = storage.output_dir(pid) / res["file"]
pj = storage.load(pid)
cfg = video.effective_config(pj, OV)
theme = video.Theme.from_config(cfg)
order, owner = slide_sequence.render_order(pj.steps)
starts, t = {}, 0.0
for st in order:
    starts[st.id] = t
    t += video.step_duration(st, cfg)
print("     总长", round(res["duration"], 1), "各段开始", {k[-4:]: round(v, 1) for k, v in starts.items()})
check("先播视频的页：视频在它那一页前面", [s.id for s in order].index(v2["id"]) < [s.id for s in order].index(s2["id"]))


def renderer_of(step):
    th = video.Theme.from_config(cfg)
    return video.StepRenderer(step=step, screenshot_path=shots / step.screenshot, theme=th, duration=step.duration or video.step_duration(step, cfg),
                              speech_offset=video.speech_lead(step), **video._video_first_args(pj, step, th))


def frame_px(t_abs, points):
    out = SP / "anim_ppt_px.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{t_abs:.3f}", "-i", str(mp4), "-frames:v", "1", str(out)], check=True)
    with Image.open(out) as im:
        im = im.convert("RGB")
        return {k: im.getpixel(v) for k, v in points.items()}, im


def page_point(step, x_pt, y_pt):
    """Point on the slide (PowerPoint points, 960 x 540) -> pixel in the video frame."""
    box, _ = video.StepRenderer.layout_box(step, shots / step.screenshot, theme)
    return int(box[0] + (box[2] - box[0]) * x_pt / 960), int(box[1] + (box[3] - box[1]) * y_pt / 540)


def ink(im, step, x0, y0, x1, y1, bg):
    """Share of pixels in a region of the slide that differ from the given background colour."""
    a, b = page_point(step, x0, y0), page_point(step, x1, y1)
    arr = np.asarray(im.crop((a[0], a[1], b[0], b[1])), np.float32)
    return float((np.abs(arr - np.array(bg, np.float32)).max(axis=2) > 60).mean())


st1 = next(s for s in pj.steps if s.id == s1["id"])
ra = renderer_of(st1).reveal
check("第 1 页用「按动画」的合成", isinstance(ra, R.TimelineAnim), type(ra).__name__)
bt = ra.beat_times
print("     第 1 页各步时间", {b: round(v, 2) for b, v in bt.items()})
check("五步按解说排好（每一步在说到它的那句话处）", sorted(bt) == [0, 1, 2, 3, 4] and all(bt[i] < bt[i + 1] for i in range(4)), bt)
base = starts[s1["id"]]
P1 = {"card": page_point(st1, 260, 160), "badge": page_point(st1, 428, 160), "note": page_point(st1, 260, 330)}   # (off the text)
v, _ = frame_px(base + bt[0] - 0.25, P1)
check("第一步之前：卡片、角标、提示都还没有（只有底板）", near(v["card"], PANEL) and near(v["badge"], PANEL) and near(v["note"], PANEL), v)
v, im = frame_px(base + bt[0] + 0.9, P1)
check("第一步：卡片出现，角标晚半秒「出现」", near(v["card"], CARD) and near(v["badge"], BADGE), v)
_, im = frame_px(base + bt[1] + 0.9, {})
check("第二步：第一个要点出现在右边", ink(im, st1, 500, 150, 900, 190, PANEL) > 0.01, ink(im, st1, 500, 150, 900, 190, PANEL))
_, im = frame_px(base + bt[1] - 0.2, {})
check("第二步之前：右边还是空的", ink(im, st1, 500, 150, 900, 350, PANEL) < 0.002, ink(im, st1, 500, 150, 900, 350, PANEL))
v, _ = frame_px(base + bt[4] + 0.9, P1)
check("最后一步：提示出现", near(v["note"], NOTE), v["note"])
v, _ = frame_px(base + bt[4] + 2.0 + 1.0, P1)
check("提示按 PPT 的设置 2 秒后消失", near(v["note"], PANEL), v["note"])

st3 = next(s for s in pj.steps if s.id == s3["id"])
ra3 = renderer_of(st3).reveal
b3 = ra3.beat_times
base3 = starts[s3["id"]]
P3 = {"ink": page_point(st3, 212, 212), "card_only": page_point(st3, 150, 170)}                                  # (off the text)
v, _ = frame_px(base3 + b3[0] + 0.9, P3)
check("第 3 页第一步：文字块先出现（白底上）", near(v["ink"], INK) and near(v["card_only"], (255, 255, 255)), v)
v, _ = frame_px(base3 + b3[1] + 0.6, P3)
check("第二步：黄色底板在后面出现，文字块仍在它前面、颜色不变（底板没有文字，不抢走焦点，也没有盖住文字）",
      near(v["card_only"], YELLOW, 50) and near(v["ink"], INK, 40), v)
v, _ = frame_px(base3 + st3.audio_duration + 0.35 + 0.3, P3)
check("最后：文字块仍在黄色底板前面", near(v["ink"], INK, 30) and near(v["card_only"], YELLOW, 50), v)

st2 = next(s for s in pj.steps if s.id == s2["id"])
ra2 = renderer_of(st2).reveal
base2 = starts[s2["id"]]
vstart = starts[v2["id"]]
P2 = {"callA": page_point(st2, 240, 145), "callB": page_point(st2, 240, 325)}                                    # (off the text)
v, _ = frame_px(vstart + 3.0, P2)
check("视频播放时：两条提示都还没出现", not near(v["callA"], CALL_A, 30) and not near(v["callB"], CALL_B, 30), v)
b2 = ra2.beat_times
v, _ = frame_px(base2 + b2[0] + 0.9, P2)
check("视频播完后：第一条提示随解说出现在视频画面上面", near(v["callA"], CALL_A) and not near(v["callB"], CALL_B, 30), v)
v, _ = frame_px(base2 + b2[1] + 0.9, P2)
check("第二条提示接着出现", near(v["callB"], CALL_B), v)

st4 = next(s for s in pj.steps if s.id == s4["id"])
ra4 = renderer_of(st4).reveal
b4 = ra4.beat_times
base4 = starts[s4["id"]]
P4 = {"w_top": page_point(st4, 230, 95), "w_bottom": page_point(st4, 230, 190), "fly": page_point(st4, 710, 360),
      "dot_start": page_point(st4, 120, 400), "dot_end": page_point(st4, 120 + 240, 400)}
v, _ = frame_px(base4 + b4[0] + 0.2, P4)
check("第 4 页擦除（从上）进行到一半：卡片上边已经露出来，下边还没有", near(v["w_top"], CARD) and near(v["w_bottom"], PANEL), v)
v, _ = frame_px(base4 + b4[0] + 0.9, P4)
check("擦除完：整张卡片都在", near(v["w_top"], CARD) and near(v["w_bottom"], CARD), v)
v, _ = frame_px(base4 + b4[1] + 0.05, P4)
check("从左上角飞入刚开始：原位置还是空的", near(v["fly"], PANEL), v)
v, _ = frame_px(base4 + b4[1] + 0.9, P4)
check("飞入完：卡片在原位置", near(v["fly"], CALL_B), v)
v, _ = frame_px(base4 + b4[2] + 0.2, P4)
check("光点先出现在原位置", near(v["dot_start"], BADGE) and near(v["dot_end"], PANEL), v)
v, _ = frame_px(base4 + b4[2] + 2.5, P4)
check("2 秒后光点沿路径移到了右边，原位置空了", near(v["dot_end"], BADGE) and near(v["dot_start"], PANEL), v)

print("\n== 5. 没有 PowerPoint 动画的页仍然按位置分组 ==")
with open(SP / "fx" / "bullets.pptx", "rb") as f:
    man = wait(c.post("/api/import/slides", files={"file": ("bullets.pptx", f)}, headers=H).json())["result"]
r = wait(c.post(f"/api/import/slides/{man['id']}/create", json=body, headers=H).json())
pb = c.get(f"/api/projects/{r['result']['project_id']}").json()
rvb = pb["steps"][0].get("reveal") or {}
check("没有动画的 PPT：和以前一样按要点拆", rvb.get("mode", "") == "" and len(rvb.get("items") or []) == 5, (rvb.get("mode"), len(rvb.get("items") or [])))
check("最后 PowerPoint 没有留下（原来没开着的话）", was_running or not pp_running())

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
