"""Slide sequence "video first": the slide's video plays first, then the text over the video appears with the narration
(slide_sequence). Synthetic slide and video drawn here; no PowerPoint, no AI."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "video_first_data"
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
from backend.services import clips, script_gen, slide_reveal as R, slide_sequence as SQ, video  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def near(a, b, tol=45):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


print("\n== 1. 准备：一页幻灯片 + 它后面的视频 ==")
PW, PH = 1920, 1080                                     # page image size
BG, POSTER = (245, 245, 245), (128, 128, 128)
GREEN, RED, BLUE, ORANGE = (40, 160, 40), (220, 40, 40), (40, 60, 220), (240, 140, 0)
YELLOW, MAGENTA = (255, 208, 0), (192, 0, 192)          # the video: yellow, then magenta (its last frame)
VBOX = (960, 324, 1728, 756)                            # video on the page: x 0.5, y 0.3, w 0.4, h 0.4
ITEMS = [  # (text, box, colour): left text / two notes over the video (the second sticks out below) / one mostly beside it
    ("左侧说明", (100, 300, 800, 450), GREEN),
    ("第一条提示", (1000, 360, 1600, 460), RED),
    ("第二条提示", (1000, 690, 1600, 790), BLUE),
    ("跨边框", (700, 700, 1100, 800), ORANGE),
]
proj = storage.create("先播视频", "zh-CN")
proj.source = "slides"
proj.settings = {"slides_reveal": True, "intro_enabled": False, "outro_enabled": False, "burn_subtitles": False}
shots = storage.screenshots_dir(proj.id)
clean = Image.new("RGB", (PW, PH), BG)
ImageDraw.Draw(clean).rectangle(VBOX, fill=POSTER)                     # the video's poster as exported by PowerPoint
full = clean.copy()
items = []
for k, (text, b, col) in enumerate(ITEMS):
    ImageDraw.Draw(full).rectangle(b, fill=col)
    Image.new("RGBA", (b[2] - b[0], b[3] - b[1]), col + (255,)).save(shots / f"r{k}.png")
    items.append(RevealItem(file=f"r{k}.png", x=b[0], y=b[1], text=text))
clean.save(shots / "clean.png")
full.save(shots / "full.png")
Image.new("RGB", (PW, PH), (60, 60, 200)).save(shots / "p0.png")
media = clips.media_dir(proj.id)
media.mkdir(parents=True, exist_ok=True)
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xFFD000:s=640x360:r=30:d=1.5",
                "-f", "lavfi", "-i", "color=c=0xC000C0:s=640x360:r=30:d=1.5",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p", str(media / "v.mp4")], check=True)
audio = storage.audio_dir(proj.id)
audio.mkdir(parents=True, exist_ok=True)
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000", "-t", "4.0",
                str(audio / "s.mp3")], check=True)
nar = "先看第一条提示。再看第二条提示。"
bounds = [{"t": 0.0, "d": 1.8, "text": "先看第一条提示。"}, {"t": 2.0, "d": 1.8, "text": "再看第二条提示。"}]
p0 = Step(kind="slide", screenshot="p0.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False,
          highlight=False, duration_override=1.0, page_title="封面", index=0)
sl = Step(kind="slide", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False,
          highlight=False, narration=nar, caption=nar, audio="s.mp3", audio_duration=4.0, boundaries=bounds,
          index=1, page_title="带视频的一页", slide_text="左侧说明 第一条提示 第二条提示 跨边框",
          reveal=SlideReveal(clean="clean.png", items=items))
vd = Step(kind="video", screenshot="full.png", img_w=PW, img_h=PH, viewport_w=PW, viewport_h=PH, zoom=False,
          highlight=False, clip=VideoClip(file="v.mp4", source="v.mp4", duration=3.0, has_audio=False, mode="inset",
                                          rect=Rect(x=0.5, y=0.3, w=0.4, h=0.4), audio="mute"))
proj.steps = [p0, sl, vd]
storage.save(proj)

check("视频在这页上面：哪些文字叠在视频上", SQ.over_video(sl, [vd], shots) == [False, True, True, False],
      SQ.over_video(sl, [vd], shots))
order, owner = SQ.render_order(proj.steps)
check("默认顺序不变：先讲这页，再放视频", [s.id for s in order] == [p0.id, sl.id, vd.id] and not owner)
sl.sequence = "video_first"
order, owner = SQ.render_order(proj.steps)
check("先播视频：视频挪到这页前面", [s.id for s in order] == [p0.id, vd.id, sl.id] and owner.get(vd.id) is sl)
sl.include = False
check("这页没放进成片：视频不挪", [s.id for s in SQ.render_order(proj.steps)[0]] == [p0.id, sl.id, vd.id])
sl.include = True

print("\n== 2. 修改播放顺序（接口） ==")
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
Hh = {"Origin": "http://127.0.0.1:8756"}
r = c.patch(f"/api/projects/{proj.id}/steps/{sl.id}", json={"sequence": "video_first"}, headers=Hh)
check("改成先播视频", r.status_code == 200 and storage.load(proj.id).steps[1].sequence == "video_first", r.text[:80])
r = c.patch(f"/api/projects/{proj.id}/steps/{sl.id}", json={"sequence": "sideways"}, headers=Hh)
check("不认识的顺序：拒绝", r.status_code == 400 and storage.load(proj.id).steps[1].sequence == "video_first", r.text[:80])
r = c.patch(f"/api/projects/{proj.id}/steps/{vd.id}", json={"sequence": "video_first"}, headers=Hh)
check("视频步骤不能设顺序", r.status_code == 400, r.text[:80])

print("\n== 3. 渲染：先播视频，播完停在最后一帧，叠在上面的文字随解说出现 ==")
W, H = 1280, 720
OV = {"video_width": W, "video_height": H, "video_fps": 30}
res = video.render_project(storage.load(proj.id), overrides=OV)
mp4 = storage.output_dir(proj.id) / res["file"]
pj = storage.load(proj.id)
theme = video.Theme.from_config(video.effective_config(pj, OV))
box, _ = video.StepRenderer.layout_box(pj.steps[1], shots / "full.png", theme)
f = (box[2] - box[0]) / PW


def at(x, y):
    return int(box[0] + x * f), int(box[1] + y * f)


PTS = {"left": at(450, 375), "note1": at(1300, 410), "note2_out": at(1300, 775), "beside": at(800, 760),
       "video": at(1344, 560)}


def frame(t, src=None):
    out = SP / "video_first_px.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src or mp4), "-frames:v", "1", str(out)],
                   check=True)
    with Image.open(out) as im:
        im = im.convert("RGB")
        return {k: im.getpixel(p) for k, p in PTS.items()}


T_VID = 1.0                                   # the cover slide lasts 1 s, then the video (3 s), then the slide
T_SLIDE = T_VID + 3.0
check("总时长 = 封面 + 视频 + 这一页", abs(res["duration"] - (T_SLIDE + video.step_duration(pj.steps[1], video.effective_config(pj, OV))))
      < 0.1, res["duration"])
v = frame(T_VID + 0.8)
check("视频一开始就在播（黄色）", near(v["video"], YELLOW), v["video"])
check("视频播放时：不叠在视频上的文字照常显示", near(v["left"], GREEN) and near(v["beside"], ORANGE), (v["left"], v["beside"]))
check("视频播放时：叠在视频上的文字先藏起来（伸出视频框的部分也看不到）", near(v["note2_out"], BG), v["note2_out"])
v = frame(T_VID + 2.5)
check("视频后半段（品红）", near(v["video"], MAGENTA), v["video"])
v = frame(T_VID + 0.05)
check("从上一页淡入到视频，不闪黑", sum(v["left"]) > 200, v["left"])

DUR = video.step_duration(pj.steps[1], video.effective_config(pj, OV))
rend = video.StepRenderer(step=pj.steps[1], screenshot_path=shots / "full.png", theme=theme, duration=DUR,
                          speech_offset=video.speech_lead(pj.steps[1]), **video._video_first_args(pj, pj.steps[1], theme))
times = rend.reveal.times
print("     出现时间", [round(x, 2) for x in times])
check("不叠在视频上的条目一开始就在；叠在上面的在视频后按解说出现",
      times[0] <= 0 and times[3] <= 0 and 0 < times[1] < times[2], [round(x, 2) for x in times])
v = frame(T_SLIDE + 0.04)
check("视频播完：画面停在视频最后一帧，不闪回封面", near(v["video"], MAGENTA), v["video"])
check("切到这一页时没有淡入（文字没闪一下）", near(v["left"], GREEN) and near(v["note1"], MAGENTA) and near(v["note2_out"], BG),
      v)
v = frame(T_SLIDE + times[1] + 0.6)
check("讲到第一条：它出现在视频画面上", near(v["note1"], RED) and near(v["note2_out"], BG) and near(v["video"], MAGENTA), v)
v = frame(T_SLIDE + times[2] + 0.6)
check("讲到第二条：第二条也出现", near(v["note2_out"], BLUE), v["note2_out"])
v = frame(res["duration"] - 0.15)
check("最后：所有内容都在，视频位置是最后一帧", near(v["note1"], RED) and near(v["note2_out"], BLUE)
      and near(v["left"], GREEN) and near(v["video"], MAGENTA), v)
srt = (storage.output_dir(proj.id) / res["srt"]).read_text(encoding="utf-8")
m = re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d) -->", srt)
t_first = int(m.group(3)) + int(m.group(4)) / 1000 if m else -1
check("解说字幕在视频之后才出现", t_first >= T_SLIDE, srt[:60])

print("\n== 4. 编辑器预览 ==")
cfg_theme = video.Theme.from_config(video.effective_config(pj))
pbox, _ = video.StepRenderer.layout_box(pj.steps[1], shots / "full.png", cfg_theme)
pf = (pbox[2] - pbox[0]) / PW


def pv(sid, t):
    im = Image.open(io.BytesIO(c.get(f"/api/projects/{proj.id}/steps/{sid}/preview?scale=1&t={t}").content)).convert("RGB")
    return {k: im.getpixel((int(pbox[0] + x * pf), int(pbox[1] + y * pf)))
            for k, (x, y) in {"note1": (1300, 410), "note2_out": (1300, 775), "video": (1344, 560)}.items()}


v = pv(sl.id, 0.5)
check("这一页的预览：视频位置是最后一帧，叠在上面的文字都在", near(v["video"], MAGENTA) and near(v["note1"], RED)
      and near(v["note2_out"], BLUE), v)
v = pv(vd.id, 0.5)
check("视频步骤的预览：叠在视频上的文字藏着", near(v["video"], YELLOW) and near(v["note2_out"], BG), v)

print("\n== 5. 对照：改回默认顺序 ==")
c.patch(f"/api/projects/{proj.id}/steps/{sl.id}", json={"sequence": ""}, headers=Hh)
res2 = video.render_project(storage.load(proj.id), overrides=OV)
v = frame(T_VID + 0.8, storage.output_dir(proj.id) / res2["file"])
check("默认顺序：封面之后先是这一页（视频位置是封面图，不在播放）", near(v["video"], POSTER), v)
check("两种顺序总时长一样", abs(res2["duration"] - res["duration"]) < 0.1, (res2["duration"], res["duration"]))

print("\n== 6. 这一页在最前面，后面跟两个视频（第二个全屏） ==")
proj2 = storage.create("两个视频", "zh-CN")
proj2.source = "slides"
proj2.settings = {"slides_reveal": True, "intro_enabled": False, "outro_enabled": False, "burn_subtitles": False}
for d in (storage.screenshots_dir, storage.audio_dir, clips.media_dir):
    shutil.copytree(d(proj.id), d(proj2.id), dirs_exist_ok=True)
src_steps = storage.load(proj.id).steps
sl2 = src_steps[1].model_copy(deep=True, update={"sequence": "video_first"})
vd2 = src_steps[2].model_copy(deep=True)
full2 = vd2.model_copy(deep=True, update={"id": "s_full_screen"})
full2.clip.mode = "fullscreen"
proj2.steps = [sl2, vd2, full2]
storage.save(proj2)
res3 = video.render_project(storage.load(proj2.id), overrides=OV)
mp3 = storage.output_dir(proj2.id) / res3["file"]
bg = video.Theme.from_config(video.effective_config(storage.load(proj2.id), OV)).bg
v = frame(0.0, mp3)
check("第一个视频像翻页一样从背景淡入", near(v["left"], bg) and near(v["video"], bg), (v, bg))
v = frame(0.6, mp3)
check("然后正常播放", near(v["video"], YELLOW) and near(v["left"], GREEN), v)
v = frame(3.04, mp3)
check("第二个视频（全屏）直接接上，不再淡入", near(v["left"], YELLOW) and near(v["video"], YELLOW), v)
v = frame(6.0, mp3)
check("最后放的是全屏视频：这一页像翻页一样淡入", near(v["left"], bg), (v["left"], bg))
v = frame(6.6, mp3)
check("这一页：原位置视频的最后一帧垫在下面", near(v["video"], MAGENTA) and near(v["left"], GREEN) and near(v["note2_out"], BG), v)

print("\n== 7. 关掉逐条出现：视频后的文字依次很快出现 ==")
pj = storage.load(proj.id)
pj.steps[1].sequence = "video_first"
pj.settings["slides_reveal"] = False
storage.save(pj)
pj = storage.load(proj.id)
th = video.Theme.from_config(video.effective_config(pj, OV))
rend = video.StepRenderer(step=pj.steps[1], screenshot_path=shots / "full.png", theme=th, duration=6.0,
                          **video._video_first_args(pj, pj.steps[1], th))
tt = rend.reveal.times if rend.reveal else []
check("还是只藏叠在视频上的条目，播完后依次出现", len(tt) == 4 and tt[0] <= 0 and tt[3] <= 0
      and abs(tt[1] - R.FIRST_AT) < 1e-6 and abs(tt[2] - R.FIRST_AT - R.CASCADE) < 1e-6, tt)

print("\n== 8. 写解说：只讲叠在视频上的文字 ==")


class RecLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        RecLLM.calls.append(messages)
        idx = [int(x) for x in re.findall(r'"i": (\d+)', messages[-1]["content"])]
        return {"title": "T", "intro": "", "outro": "",
                "steps": [{"i": i, "title": f"页{i}", "narration": f"第 {i} 页。"} for i in idx]}


def payload(msg):
    raw = msg[-1]["content"]
    k = raw.index(chr(10) + "[") + 1
    data, _ = json.JSONDecoder().raw_decode(raw[k:])
    return {d["i"]: d for d in data if isinstance(d, dict) and "i" in d}


pj = storage.load(proj.id)
pj.settings["slides_reveal"] = True
storage.save(pj)
script_gen.generate_slides_script(storage.load(proj.id), notes_mode="ignore", client=RecLLM())
d = payload(RecLLM.calls[-1]).get(pj.steps[1].index, {})
check("先播视频的页：标记 video_first，条目只给叠在视频上的两条",
      d.get("video_first") is True and d.get("content_items") == ["第一条提示", "第二条提示"], d)
check("系统提示说明了 video_first 的讲法", '"video_first": true' in RecLLM.calls[-1][0]["content"])
dd = script_gen._slide_payload(storage.load(proj.id), storage.load(proj.id).steps[1], "ignore")
check("双人问答也一样", dd.get("video_first") is True and dd.get("content_items") == ["第一条提示", "第二条提示"], dd)
check("问答的系统提示也说明了", '"video_first": true' in script_gen.DIALOGUE_SYSTEM)
pj = storage.load(proj.id)
pj.steps[1].sequence = ""
storage.save(pj)
script_gen.generate_slides_script(storage.load(proj.id), notes_mode="ignore", client=RecLLM())
d = payload(RecLLM.calls[-1]).get(pj.steps[1].index, {})
check("默认顺序：没有标记，条目是整页四条", "video_first" not in d and len(d.get("content_items") or []) == 4, d)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
