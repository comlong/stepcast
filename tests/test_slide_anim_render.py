"""Slides with the author's PowerPoint animations, rendered: layers keep PowerPoint's z-order, come in and go out on the steps the narration
reaches, and work together with "video first". Synthetic layers drawn here; no PowerPoint, no AI."""
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "slide_anim_render_data"
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
from backend.models import Rect, RevealItem, SlideReveal, Step, VideoClip  # noqa: E402
from backend.services import clips, slide_reveal as R, script_gen, video  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def near(a, b, tol=30):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


PW, PH = 1920, 1080
BG, PANEL, PLATE, TEXT, LABEL, FLASH = (20, 22, 28), (230, 235, 245), (40, 120, 200), (255, 200, 0), (200, 0, 100), (0, 160, 80)
OV = {"video_width": 1280, "video_height": 720, "video_fps": 30}

print("\n== 1. 准备：底板（静态）、后出现的底板、先出现的文字、盖在最上面的静态标签、闪现又消失的块 ==")
proj = storage.create("动画页", "zh-CN")
proj.source = "slides"
proj.settings = {"slides_reveal": True, "intro_enabled": False, "outro_enabled": False, "burn_subtitles": False}
shots = storage.screenshots_dir(proj.id)


def layer(name, box, color, **kw):
    Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color + (255,)).save(shots / f"{name}.png")
    return RevealItem(file=f"{name}.png", x=box[0], y=box[1], **kw)


# z-order, back first. The plate (second step) lies BEHIND the text (first step); the label (static) lies above both.
items = [
    layer("panel", (100, 100, 1800, 1000), PANEL),
    layer("plate", (300, 300, 1000, 600), PLATE, text="第二部分底板", beat=1, offset=0.0),
    layer("text", (400, 380, 900, 520), TEXT, text="第一部分文字", beat=0, offset=0.0),
    layer("label", (850, 330, 1100, 420), LABEL),
    layer("flash", (1200, 300, 1500, 500), FLASH, text="闪现", beat=0, offset=0.0, anim="appear", exit_beat=1, exit_offset=0.0),
]
clean = Image.new("RGB", (PW, PH), BG)
clean.save(shots / "clean.png")
full = clean.copy()
for it in items:
    ly = Image.open(shots / it.file).convert("RGBA")
    full.paste(ly, (it.x, it.y), ly.getchannel("A"))
full.save(shots / "full.png")
audio = storage.audio_dir(proj.id)
audio.mkdir(parents=True, exist_ok=True)
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000", "-t", "6.0", str(audio / "s.mp3")], check=True)
nar = "先看第一部分文字。再看第二部分底板。"
bounds = [{"t": 0.0, "d": 2.8, "text": "先看第一部分文字。"}, {"t": 3.0, "d": 2.8, "text": "再看第二部分底板。"}]
st = Step(kind="slide", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False, highlight=False,
          narration=nar, caption=nar, audio="s.mp3", audio_duration=6.0, boundaries=bounds, index=0, page_title="动画页",
          reveal=SlideReveal(clean="clean.png", items=items, mode="timeline"))
proj.steps = [st]
storage.save(proj)

res = video.render_project(storage.load(proj.id), overrides=OV)
mp4 = storage.output_dir(proj.id) / res["file"]
pj = storage.load(proj.id)
theme = video.Theme.from_config(video.effective_config(pj, OV))
box, _ = video.StepRenderer.layout_box(pj.steps[0], shots / "full.png", theme)
f = (box[2] - box[0]) / PW


def at(x, y):
    return int(box[0] + x * f), int(box[1] + y * f)


PTS = {"panel_only": at(200, 200), "plate_only": at(350, 570), "text_only": at(500, 450), "text_on_plate": at(650, 450),
       "label_over_text": at(870, 400), "label_over_plate": at(950, 400), "flash": at(1350, 400)}


def frame(t, src=None):
    out = SP / "slide_anim_px.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src or mp4), "-frames:v", "1", str(out)], check=True)
    with Image.open(out) as im:
        im = im.convert("RGB")
        return {k: im.getpixel(p) for k, p in PTS.items()}


rend = video.StepRenderer(step=pj.steps[0], screenshot_path=shots / "full.png", theme=theme, duration=res["duration"],
                          speech_offset=video.speech_lead(pj.steps[0]))
ra = rend.reveal
check("动画页用上了「按动画」的合成", isinstance(ra, R.TimelineAnim), type(ra).__name__)
b0, b1 = ra.beat_times[0], ra.beat_times[1]
print("     两步的时间", round(b0, 2), round(b1, 2), "总长", round(res["duration"], 2))
check("按解说：第二步比第一步晚", 0 < b0 < b1, (b0, b1))
check("没有动画的层一开始就在，有动画的从各自那一步起", [round(s, 2) for s in ra.times][0] == 0 and ra.times[2] == b0 and ra.times[1] == b1 and ra.times[3] == 0
      and ra.times[4] == b0, ra.times)

v = frame(b0 - 0.25)
check("第一步之前：只有静态的（底板、盖在最上面的标签）", near(v["panel_only"], PANEL) and near(v["plate_only"], PANEL) and near(v["text_only"], PANEL)
      and near(v["flash"], PANEL) and near(v["label_over_text"], LABEL), v)
v = frame(b0 + 0.9)
check("第一步：文字和闪现的块出现；后出现的底板还没有", near(v["text_only"], TEXT) and near(v["flash"], FLASH) and near(v["plate_only"], PANEL), v)
check("标签盖在先出现的文字上面（静态层的上下关系不变）", near(v["label_over_text"], LABEL), v["label_over_text"])
v = frame(b1 + 0.9)
check("第二步：底板出现了，但仍在文字后面：先出现的文字变暗后仍然不透明（底板不会从它后面透出来）",
      near(v["plate_only"], PLATE) and not near(v["text_on_plate"], PLATE, 25) and not near(v["text_on_plate"], TEXT, 25), v)
mix = tuple(int(PANEL[i] + (TEXT[i] - PANEL[i]) * R.FOCUS_ALPHA) for i in range(3))
check("变暗 = 把颜色往它下面的背景（页面底色）拉 60%，而不是变半透明", near(v["text_on_plate"], mix, 30), (v["text_on_plate"], mix))
check("标签仍盖在底板和文字上面", near(v["label_over_plate"], LABEL) and near(v["label_over_text"], LABEL), v)
check("退出：闪现的块在第二步消失", near(v["flash"], PANEL), v["flash"])
v = frame(res["duration"] - 0.2)
check("最后：文字恢复正常亮度，仍在底板上面", near(v["text_on_plate"], TEXT) and near(v["plate_only"], PLATE), v)
check("最后：标签仍在最上面", near(v["label_over_plate"], LABEL) and near(v["label_over_text"], LABEL), v)

print("\n== 2. 其他动画方式 ==")
check("「出现」是瞬间的，不淡入（第一帧就是满色）", [lk.anim for lk in ra.looks] == ["fade", "fade", "fade", "fade", "appear"],
      [lk.anim for lk in ra.looks])
k = 4
t0 = ra.starts[k]
img0 = ra.compose(t0 + 0.01).getpixel(at(1350, 400))
check("「出现」：刚到时间就已经是满色", near(img0, FLASH, 10), img0)
img1 = ra.compose(ra.starts[2] + 0.05).getpixel(at(500, 450))
check("淡入的第一瞬间还没有满色", not near(img1, TEXT, 10), img1)
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
pv = Image.open(io.BytesIO(c.get(f"/api/projects/{proj.id}/steps/{st.id}/preview?scale=1&t=0.5").content)).convert("RGB")
pbox, _ = video.StepRenderer.layout_box(pj.steps[0], shots / "full.png", video.Theme.from_config(video.effective_config(pj)))
pf = (pbox[2] - pbox[0]) / PW


def pat(x, y):
    return int(pbox[0] + x * pf), int(pbox[1] + y * pf)


vals = {k2: pv.getpixel(p) for k2, p in {"plate_only": pat(350, 570), "text_on_plate": pat(650, 450), "label_over_text": pat(870, 400),
                                          "flash": pat(1350, 400)}.items()}
check("编辑器预览是完整的一页：全部层按上下顺序叠好（会消失的也在）", near(vals["plate_only"], PLATE) and near(vals["text_on_plate"], TEXT) and near(vals["label_over_text"], LABEL)
      and near(vals["flash"], FLASH), vals)

print("\n== 3. 说的单位是「步」，不是一个个层 ==")
check("两步：第一步的文字和闪现块是同一步", R.reveal_texts(pj.steps[0]) == ["第一部分文字\n闪现", "第二部分底板"], R.reveal_texts(pj.steps[0]))
check("按解说对齐的单位也是步", R.needs_align(pj.steps[0]))


class RecLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        RecLLM.calls.append(messages)
        idx = [int(x) for x in re.findall(r'"i": (\d+)', messages[-1]["content"])]
        return {"title": "T", "intro": "", "outro": "", "steps": [{"i": i, "title": f"页{i}", "narration": f"第 {i} 页。"} for i in idx]}


script_gen.generate_slides_script(storage.load(proj.id), notes_mode="ignore", client=RecLLM())
um = RecLLM.calls[-1][-1]["content"]
check("写解说时把每一步（按动画顺序）交给 AI", '"content_items"' in um and um.index("第一部分文字") < um.index("第二部分底板"), um[-260:])

print("\n== 4. 先播视频：视频之后才出现的提示，盖在视频最后一帧上面 ==")
proj2 = storage.create("动画页加视频", "zh-CN")
proj2.source = "slides"
proj2.settings = dict(proj.settings)
shots2, media2 = storage.screenshots_dir(proj2.id), clips.media_dir(proj2.id)
media2.mkdir(parents=True, exist_ok=True)
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xFFD000:s=640x360:r=30:d=1.5",
                "-f", "lavfi", "-i", "color=c=0xC000C0:s=640x360:r=30:d=1.5",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p", str(media2 / "v.mp4")], check=True)
POSTER = (128, 128, 128)
VBOX = (960, 324, 1728, 756)


def layer2(name, box, color, **kw):
    Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color + (255,)).save(shots2 / f"{name}.png")
    return RevealItem(file=f"{name}.png", x=box[0], y=box[1], **kw)


items2 = [layer2("poster", VBOX, POSTER, media=True),
          layer2("callA", (1000, 360, 1600, 460), (220, 40, 40), text="第一条提示", beat=0, after_media=True),
          layer2("callB", (1000, 690, 1600, 790), (40, 60, 220), text="第二条提示", beat=1, after_media=True),
          layer2("title", (100, 100, 800, 200), PANEL),
          # comes in together with the video (the same click as the first callout, but before the video has played): there from the start
          layer2("early", (100, 400, 500, 520), (0, 150, 150), text="跟视频一起出现", beat=0, after_media=False)]
clean.save(shots2 / "clean.png")
full2 = clean.copy()
for it in items2:
    ly = Image.open(shots2 / it.file).convert("RGBA")
    full2.paste(ly, (it.x, it.y), ly.getchannel("A"))
full2.save(shots2 / "full.png")
storage.audio_dir(proj2.id).mkdir(parents=True, exist_ok=True)
shutil.copyfile(audio / "s.mp3", storage.audio_dir(proj2.id) / "s.mp3")
nar2 = "先看第一条提示。再看第二条提示。"
bounds2 = [{"t": 0.0, "d": 1.8, "text": "先看第一条提示。"}, {"t": 2.0, "d": 1.8, "text": "再看第二条提示。"}]
sl2 = Step(kind="slide", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False, highlight=False,
           narration=nar2, caption=nar2, audio="s.mp3", audio_duration=4.0, boundaries=bounds2, index=0, page_title="视频页",
           sequence="video_first", reveal=SlideReveal(clean="clean.png", items=items2, mode="timeline"))
vd2 = Step(kind="video", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False, highlight=False, index=1,
           clip=VideoClip(file="v.mp4", source="v.mp4", duration=3.0, has_audio=False, mode="inset",
                          rect=Rect(x=0.5, y=0.3, w=0.4, h=0.4), audio="mute"))
proj2.steps = [sl2, vd2]
storage.save(proj2)
res2 = video.render_project(storage.load(proj2.id), overrides=OV)
mp2 = storage.output_dir(proj2.id) / res2["file"]
pj2 = storage.load(proj2.id)
th2 = video.Theme.from_config(video.effective_config(pj2, OV))
box2, _ = video.StepRenderer.layout_box(pj2.steps[0], shots2 / "full.png", th2)
f2 = (box2[2] - box2[0]) / PW


def at2(x, y):
    return int(box2[0] + x * f2), int(box2[1] + y * f2)


P2 = {"title": at2(300, 150), "video": at2(1344, 560), "callA": at2(1300, 410), "callB": at2(1300, 740), "early": at2(300, 460)}


def frame2(t):
    out = SP / "slide_anim_px2.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mp2), "-frames:v", "1", str(out)], check=True)
    with Image.open(out) as im:
        im = im.convert("RGB")
        return {k2: im.getpixel(p) for k2, p in P2.items()}


v = frame2(0.8)
check("视频先放：黄色的视频在播，提示还没出现，其他内容（标题）在", near(v["video"], (252, 202, 0), 40) and near(v["callA"], (252, 202, 0), 40)
      and near(v["title"], PANEL), v)
check("和视频一起出现的元素，视频一播放就在", near(v["early"], (0, 150, 150)), v["early"])
ra2 = video.StepRenderer(step=pj2.steps[0], screenshot_path=shots2 / "full.png", theme=th2, duration=4.5,
                         speech_offset=video.speech_lead(pj2.steps[0]), **video._video_first_args(pj2, pj2.steps[0], th2)).reveal
check("先播视频的页用「按动画」的合成，视频之后出现的提示按解说排", isinstance(ra2, R.TimelineAnim) and sorted(ra2.beat_times) == [0, 1], ra2 and ra2.beat_times)
t_slide = 3.0
v = frame2(t_slide + 0.04)
check("视频播完：画面停在视频最后一帧（品红），不是 PPT 里的灰色封面", near(v["video"], (192, 0, 192), 40), v["video"])
check("视频播完那一刻：先就在的元素还在，不会跟着第一条提示一起往后延", near(v["early"], (0, 150, 150)) and not near(v["callA"], (220, 40, 40), 30), v)
v = frame2(t_slide + ra2.beat_times[0] + 0.9)
check("第一条提示出现在视频画面上面", near(v["callA"], (220, 40, 40)) and near(v["callB"], (192, 0, 192), 40), v)
v = frame2(t_slide + ra2.beat_times[1] + 0.9)
check("第二条提示接着出现", near(v["callB"], (40, 60, 220)), v)
script_gen.generate_slides_script(storage.load(proj2.id), notes_mode="ignore", client=RecLLM())
d2 = RecLLM.calls[-1][-1]["content"]
check("标记了先播视频，条目是那两步（视频之前就有的不讲）", '"video_first": true' in d2 and d2.count("第一条提示") == 1 and d2.count("第二条提示") == 1
      and "跟视频一起出现" not in d2.split("content_items")[-1], d2[-240:])

print("\n== 5. 只有带文字的步骤才会让前面的内容变淡 ==")
canvas = Image.new("RGB", (200, 100), (240, 240, 240))
plate_l = (Image.new("RGBA", (120, 60), PLATE + (255,)), 20, 20)           # a card behind the text: a step of its own, nothing to say
text_l = (Image.new("RGBA", (40, 20), TEXT + (255,)), 40, 40)


def anim(talk):
    return R.TimelineAnim(canvas, [plate_l, text_l], starts=[3.0, 1.0], exits=[None, None], beats=[1, 0], looks=[R.Look(), R.Look()],
                          media=[False, False], beat_times={0: 1.0, 1: 3.0}, duration=8.0, rise=0.0, focus=True, talk_beats=talk)


px = anim([0]).compose(4.5).getpixel((50, 50))
check("没有文字的底板出现：前面的文字原样不变，仍在底板前面", near(px, TEXT, 5), px)
px = anim([0, 1]).compose(4.5).getpixel((50, 50))
mix = tuple(int(240 + (TEXT[i] - 240) * R.FOCUS_ALPHA) for i in range(3))
check("底板上有文字的话：前面的文字变暗（往背景色拉），仍不透明，底板透不出来", near(px, mix, 6) and not near(px, PLATE, 40), (px, mix))
check("状态键随之变化，不会把不同的画面当成同一帧", anim([0]).key(4.5) != anim([0]).key(2.0) and anim([0]).key(4.5) == anim([0]).key(5.0))


print("\n== 6. 擦除、飞入：只露出一部分 / 从边上滑进来 ==")
cv = Image.new("RGB", (300, 200), (240, 240, 240))
BOX = (Image.new("RGBA", (100, 40), (200, 0, 0, 255)), 100, 80)           # x 100-200, y 80-120
BOUNDS = (0, 0, 300, 200)


def one(look, start=1.0, motion=None):
    look.motion = motion
    return R.TimelineAnim(cv, [BOX], starts=[start], exits=[None], beats=[0], looks=[look], media=[False], beat_times={0: start},
                          duration=10.0, rise=0.0, focus=False, bounds=BOUNDS)


def px(an, t, x, y):
    return an.compose(t).getpixel((x, y))


RED = (200, 0, 0)
w = one(R.Look("wipe", 1.0, "top"))
check("擦除（从上边）：到一半时只有上半", near(px(w, 1.5, 150, 90), RED) and near(px(w, 1.5, 150, 115), (240, 240, 240)), (px(w, 1.5, 150, 90), px(w, 1.5, 150, 115)))
check("擦除完了：全在", near(px(w, 2.1, 150, 115), RED))
w = one(R.Look("wipe", 1.0, "bottom"))
check("擦除（从下边）：到一半时只有下半", near(px(w, 1.5, 150, 115), RED) and near(px(w, 1.5, 150, 90), (240, 240, 240)))
w = one(R.Look("wipe", 1.0, "left"))
check("擦除（从左边）：到一半时只有左半", near(px(w, 1.5, 110, 100), RED) and near(px(w, 1.5, 190, 100), (240, 240, 240)))
w = one(R.Look("wipe", 1.0, "right"))
check("擦除（从右边）：到一半时只有右半", near(px(w, 1.5, 190, 100), RED) and near(px(w, 1.5, 110, 100), (240, 240, 240)))
w = one(R.Look("wipe", 1.0, ""))
check("不知道方向：按 PowerPoint 默认的从下边", near(px(w, 1.5, 150, 115), RED) and near(px(w, 1.5, 150, 90), (240, 240, 240)))
check("擦除不变半透明（露出的部分是满色）", near(px(one(R.Look("wipe", 1.0, "top")), 1.4, 150, 85), RED, 5))
f = one(R.Look("fly", 1.0, "left"))
check("飞入（从左边）：刚开始在页面左边外面，原位置是空的", near(px(f, 1.05, 150, 100), (240, 240, 240)))
check("飞入：飞到一半，在左边和原位置之间", any(near(px(f, 1.3, x, 100), RED) for x in range(0, 100, 5)) and not near(px(f, 1.3, 150, 100), RED))
check("飞入完了：回到原位置", near(px(f, 2.1, 150, 100), RED))
f = one(R.Look("fly", 1.0, "bottom"))
check("飞入（从下边）：开始时在页面下面", near(px(f, 1.05, 150, 100), (240, 240, 240)) and near(px(f, 2.1, 150, 100), RED))
f = one(R.Look("fly", 1.0, "top-left"))
fx, fy = f._fly_from(0, "top-left")
check("从左上角飞入：起点在页面左上方之外，飞到一半在左上和原位置之间", fx < 0 and fy < 0 and fx <= -200 and fy <= -120
      and near(px(f, 1.05, 150, 100), (240, 240, 240)) and near(px(f, 2.1, 150, 100), RED), (fx, fy))
check("从右下角飞入：起点在右下方之外", all(v > 0 for v in one(R.Look("fly", 1.0, "bottom-right"))._fly_from(0, "bottom-right")))
check("瞬间出现：到时间就是满色", near(px(one(R.Look("appear", 0.0)), 1.0, 150, 100), RED))
check("擦除时每一帧状态都变，完了才稳定", one(R.Look("wipe", 1.0, "top")).key(1.5) is None and one(R.Look("wipe", 1.0, "top")).key(3.0) is not None)

print("\n== 7. 动作路径：光点沿线移动 ==")
pts = R.parse_path("M -8.33333E-7 2.77556E-17 L 0.45 0.6 ")
check("读 PowerPoint 写的路径（含科学计数法）", len(pts) == 2 and abs(pts[1][0] - 0.45) < 1e-9 and abs(pts[1][1] - 0.6) < 1e-9, pts)
cur = R.parse_path("M 0 0 C 0.1 0 0.2 0.1 0.3 0.1 E")
check("曲线取样成折线，终点对", len(cur) > 5 and abs(cur[-1][0] - 0.3) < 1e-9 and abs(cur[-1][1] - 0.1) < 1e-9, cur[-3:])
check("读不懂的路径：没有点（不动，不报错）", R.parse_path("") == [] and R.parse_path("M x y") == [])
m = R.Motion(1.0, 2.0, [(0.0, 0.0), (100.0, 40.0)], 0.24, 0.28)
check("开始前没动、结束后停在终点", m.offset(0.5) == (0, 0) and m.offset(3.0) == (100, 40) and m.offset(9.0) == (100, 40))
offs = [m.offset(1.0 + 2.0 * k / 20)[0] for k in range(21)]
check("有缓入缓出：一路往前、从慢到快再到慢", all(b >= a for a, b in zip(offs, offs[1:])) and offs[1] - offs[0] < offs[8] - offs[7] and offs[20] - offs[19] < offs[12] - offs[11], offs)
check("走到一半的时间 = 走了一半多一点点（先慢后快再慢，对称时恰好一半）", abs(R.Motion(0, 2, [(0.0, 0.0), (100.0, 0.0)], 0.3, 0.3).offset(1.0)[0] - 50) <= 1)
dot = (Image.new("RGBA", (20, 20), (0, 0, 255, 255)), 20, 20)
an = R.TimelineAnim(cv, [dot], starts=[1.0], exits=[None], beats=[0], looks=[R.Look("appear", 0.0, "", R.Motion(1.0, 2.0, [(0.0, 0.0), (100.0, 0.0)]))],
                    media=[False], beat_times={0: 1.0}, duration=10.0, rise=0.0, focus=False, bounds=BOUNDS)
check("光点：先出现在原位置", near(px(an, 1.0, 30, 30), (0, 0, 255)) and near(px(an, 1.0, 130, 30), (240, 240, 240)))
check("移动当中：位置在路上", near(px(an, 2.0, 80, 30), (0, 0, 255)) and near(px(an, 2.0, 30, 30), (240, 240, 240)))
check("移动完：停在路径终点，原位置空了", near(px(an, 4.0, 130, 30), (0, 0, 255)) and near(px(an, 4.0, 30, 30), (240, 240, 240)))
check("移动期间状态一直变，移完才稳定", an.key(2.0) is None and an.key(4.0) is not None and an.key(4.0) == an.key(5.0) and an.key(0.5) != an.key(4.0))
still = R.TimelineAnim(cv, [dot], starts=[0.0], exits=[None], beats=[-1], looks=[R.Look("fade", 0.5, "", R.Motion(1.0, 2.0, [(0.0, 0.0), (50.0, 0.0)]))],
                       media=[False], beat_times={}, duration=10.0, rise=0.0, focus=False, bounds=BOUNDS)
check("一开始就在的东西也能沿路径移动", near(px(still, 0.5, 30, 30), (0, 0, 255)) and near(px(still, 4.0, 80, 30), (0, 0, 255)) and still.floor_n == 0)
dots = R.parse_path("M 0 0 L .5 -.25 L 1. 0 E")
check("数字省略了 0（.5、1.）也读得懂", dots == [(0.0, 0.0), (0.5, -0.25), (1.0, 0.0)], dots)
rel = R.parse_path("M 0 0 l 0.036 0 l 0 0.036 l 0.036 0 E")
check("小写是相对坐标（PowerPoint 内置的一些路径这样写）：从当前点往前走", [tuple(round(v, 3) for v in q) for q in rel]
      == [(0.0, 0.0), (0.036, 0.0), (0.036, 0.036), (0.072, 0.036)], rel)
crel = R.parse_path("M 0 0 c 0 0.1 0.1 0.1 0.1 0 c 0 -0.1 0.1 -0.1 0.1 0 E")
check("相对的曲线：两段接起来，终点在 (0.2, 0)", abs(crel[-1][0] - 0.2) < 1e-9 and abs(crel[-1][1]) < 1e-9 and len(crel) == 25, crel[-1])
box = R.parse_path("M 0 0 L 0.25 0 L 0.25 0.25 Z")
check("Z 把路径闭合回起点", box[-1] == (0.0, 0.0) and len(box) == 4, box)
check("一个 L 后面跟几对坐标：都是直线", R.parse_path("M 0.1 0.1 L 0.2 0.1 0.3 0.1 E") == [(0.1, 0.1), (0.2, 0.1), (0.3, 0.1)])

print("\n== 8. 出现的先后永远是作者的点击顺序，不被解说的顺序打乱 ==")
pj = storage.load(proj.id)
stp = pj.steps[0]
stp.narration = stp.caption = "先看第二部分底板。再看第一部分文字。"
stp.boundaries = [{"t": 0.0, "d": 2.8, "text": "先看第二部分底板。"}, {"t": 3.0, "d": 2.8, "text": "再看第一部分文字。"}]
stp.reveal.align = [stp.narration.index("再看"), 0]                         # the AI says: step 2 is talked about first
stp.reveal.align_key = R.align_key(stp.narration, R.reveal_texts(stp))
th0 = video.Theme.from_config(video.effective_config(pj, OV))
rr = video.StepRenderer(step=stp, screenshot_path=shots / "full.png", theme=th0, duration=8.0, speech_offset=video.speech_lead(stp))
bt0 = rr.reveal.beat_times
check("解说先讲第二步，画面仍然先出第一步", bt0[0] < bt0[1] and bt0[1] - bt0[0] >= R.STEP_GAP - 1e-9, bt0)
stp.reveal.align = [0, stp.narration.index("再看")]
stp.reveal.align_key = R.align_key(stp.narration, R.reveal_texts(stp))
bt1 = video.StepRenderer(step=stp, screenshot_path=shots / "full.png", theme=th0, duration=8.0, speech_offset=video.speech_lead(stp)).reveal.beat_times
check("顺序本来就对的：照解说的位置，不被拉开", bt1[0] < bt1[1] and bt1[1] - bt1[0] > 1.0, bt1)
stp.reveal.align = [5, 5]
stp.reveal.align_key = R.align_key(stp.narration, R.reveal_texts(stp))
bt2 = video.StepRenderer(step=stp, screenshot_path=shots / "full.png", theme=th0, duration=8.0, speech_offset=video.speech_lead(stp)).reveal.beat_times
check("两步对到同一句：错开一点，一步一步出来", abs((bt2[1] - bt2[0]) - R.STEP_GAP) < 1e-9, bt2)

print("\n== 9. 项目里存的光点路径，渲染时按页面大小换算 ==")
proj3 = storage.create("光点", "zh-CN")
proj3.source = "slides"
proj3.settings = dict(proj.settings)
shots3 = storage.screenshots_dir(proj3.id)
Image.new("RGB", (PW, PH), (20, 22, 28)).save(shots3 / "clean.png")
Image.new("RGBA", (200, 200), (255, 160, 0, 255)).save(shots3 / "spot.png")
Image.new("RGBA", (600, 300), (0, 120, 200, 255)).save(shots3 / "card.png")
Image.new("RGB", (PW, PH), (20, 22, 28)).save(shots3 / "full.png")
items3 = [RevealItem(file="card.png", x=300, y=600, text="卡片", beat=0, anim="wipe", side="top", dur=0.5),
          RevealItem(file="spot.png", x=100, y=100, beat=1, anim="appear", dur=0.0, motion_beat=1, motion_offset=0.0, motion_dur=2.0,
                     motion_path="M 0 0 L 0.5 0.25 E", motion_accel=0.2, motion_decel=0.2)]
st3 = Step(kind="slide", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False, highlight=False, index=0,
           narration="先看卡片。再看光点。", caption="先看卡片。再看光点。", reveal=SlideReveal(clean="clean.png", items=items3, mode="timeline"))
proj3.steps = [st3]
storage.save(proj3)
th3 = video.Theme.from_config(video.effective_config(storage.load(proj3.id), OV))
r3 = video.StepRenderer(step=st3, screenshot_path=shots3 / "full.png", theme=th3, duration=8.0).reveal
sb, _ = video.StepRenderer.layout_box(st3, shots3 / "full.png", th3)
mo = r3.looks[1].motion
check("光点的路径存进去了，渲染时有路径", mo is not None and abs(mo.dur - 2.0) < 1e-9 and r3.looks[0].anim == "wipe" and r3.looks[0].side == "top")
check("路径按页面在画面里的大小换算（终点 = 页宽的 50%、页高的 25%）",
      abs(mo.offset(mo.end + 1)[0] - 0.5 * (sb[2] - sb[0])) <= 1 and abs(mo.offset(mo.end + 1)[1] - 0.25 * (sb[3] - sb[1])) <= 1, (mo.offset(mo.end + 1), sb))
check("路径从那一步出现的时间开始", abs(mo.start - r3.beat_times[1]) < 1e-9, (mo.start, r3.beat_times))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
