"""Fixes from the second review: rotated / duration-less / non-square-pixel videos, posters of inserted videos, transcribed subtitles following the trim,
narration not overwriting original-sound subtitles, translating original-sound subtitles, parallel renders stopping at the first error, step numbers, hard links, probe cache."""
import os, shutil, subprocess, sys, time
from pathlib import Path

SP = Path(sys.argv[1]); DATA = SP / "review2_data"; VID = SP / "vid"; V2 = SP / "vid2"
PROBE = shutil.which("ffprobe")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects"); os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
config.save({"ui_language": "zh", "asr_model": "small"})
from fastapi.testclient import TestClient
from PIL import Image
from backend import main, storage
from backend.models import Step, VideoClip
from backend.services import clips, slides as S, subtitles as subs, video
S._soffice = lambda: None

c = TestClient(main.app, base_url="http://127.0.0.1:8756"); H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(n, cond, d=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {n}" + (f"  —— {d}" if d != "" else ""), flush=True)
    cond or fails.append(n)


def wait(j, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)
    raise TimeoutError


def steps(pid):
    return c.get(f"/api/projects/{pid}").json()["steps"]


def insert(pid, path, after=""):
    with open(path, "rb") as f:
        r = wait(c.post(f"/api/projects/{pid}/steps/video", files={"file": (path.name, f)},
                        data={"after": after}, headers=H).json())
    assert r["status"] == "done", r.get("error")
    return r["result"]["step"]


print("\n== 1. 各种视频文件的探测 ==")
p = clips.probe(V2 / "rotated.mp4")
check("手机竖拍（旋转 90°）：尺寸按转正后的算", (p["width"], p["height"]) == (360, 640), p)
p = clips.probe(V2 / "nodur.webm")
check("文件头没写时长的 webm：扫一遍算出时长", abs(p["duration"] - 6.0) < 0.1, p["duration"])
p = clips.probe(V2 / "anamorphic.mp4")
check("非方形像素：按显示宽度算", (p["width"], p["height"]) == (1047, 576), p)
t0 = time.perf_counter()
for _ in range(20):
    clips.probe(V2 / "anamorphic.mp4")
check("同一文件探测有缓存", time.perf_counter() - t0 < 0.05, f"{(time.perf_counter() - t0) * 1000:.1f} ms / 20 次")

print("\n== 2. 在录屏项目里插入视频 ==")
proj = storage.create("插入视频测试", "zh-CN")
pid = proj.id
sid_rot = insert(pid, V2 / "rotated.mp4")
sid_web = insert(pid, V2 / "nodur.webm")
st = {s["id"]: s for s in steps(pid)}
rot, web = st[sid_rot], st[sid_web]
check("插入的视频有封面截图（竖的）", rot["screenshot"] and (rot["img_w"], rot["img_h"]) == (360, 640)
      and (storage.screenshots_dir(pid) / rot["screenshot"]).exists(), (rot["screenshot"], rot["img_w"], rot["img_h"]))
check("webm 时长正确", abs(web["clip"]["duration"] - 6.0) < 0.1, web["clip"]["duration"])
tmp_left = list((config.DATA_DIR / "_tmp").glob("*"))
check("上传的临时文件没留下", not tmp_left, tmp_left[:2])

# "poster only": inserted videos used to have no base image, so the frame was empty
c.patch(f"/api/projects/{pid}/steps/{sid_rot}/video", json={"mode": "poster"}, headers=H)
img = Image.open(__import__("io").BytesIO(c.get(f"/api/projects/{pid}/steps/{sid_rot}/preview?scale=1").content)).convert("RGB")
mid = img.crop((img.width // 2 - 40, img.height // 2 - 40, img.width // 2 + 40, img.height // 2 + 40))
extrema = [e[1] - e[0] for e in mid.getextrema()]
check("「只显示封面」有画面（不是空白）", max(extrema) > 60, extrema)
c.patch(f"/api/projects/{pid}/steps/{sid_rot}/video", json={"mode": "fullscreen"}, headers=H)

# replacing the video: the poster changes too and the old one is deleted
old_shot = rot["screenshot"]
with open(V2 / "plain.mp4", "rb") as f:
    wait(c.post(f"/api/projects/{pid}/steps/{sid_rot}/video", files={"file": ("plain.mp4", f)}, headers=H).json())
new = next(s for s in steps(pid) if s["id"] == sid_rot)
check("换视频后封面更新、旧封面删除", new["screenshot"] != old_shot and (new["img_w"], new["img_h"]) == (640, 360)
      and not (storage.screenshots_dir(pid) / old_shot).exists())

print("\n== 3. 渲染：竖视频全屏、webm 时长 ==")
with open(V2 / "rotated.mp4", "rb") as f:
    wait(c.post(f"/api/projects/{pid}/steps/{sid_rot}/video", files={"file": ("rotated.mp4", f)}, headers=H).json())
pj0 = storage.load(pid); pj0.settings = {"intro_enabled": False, "outro_enabled": False}; storage.save(pj0)
r = wait(c.post(f"/api/projects/{pid}/render", json={"width": 960, "height": 540, "fps": 15}, headers=H).json())
check("渲染成功", r["status"] == "done", r.get("error"))
pj = c.get(f"/api/projects/{pid}").json()
by = {s["id"]: s for s in pj["steps"]}
check("webm 这一步 6 秒（以前会变成 0.1 秒）", abs(by[sid_web]["duration"] - 6.0) < 0.1, by[sid_web]["duration"])
mp4 = DATA / "projects" / pid / "output" / pj["output"]
intro = 0
out = subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                     capture_output=True, text=True).stdout
frame = SP / "review2_frame.png"
t_rot = [x for x in pj["steps"] if x["id"] == sid_rot][0]
# intro + portrait video: take a frame from the middle of the portrait video; black bars left and right, picture in the middle
subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2.0", "-i", str(mp4), "-frames:v", "1", str(frame)])
fr = Image.open(frame).convert("L")
side = fr.crop((20, 100, 200, 400)).getextrema()
center = fr.crop((430, 100, 530, 400)).getextrema()
check("竖视频全屏：左右是黑边、中间有画面（没被拉成横的）", side[1] < 30 and center[1] - center[0] > 60, (side, center))

print("\n== 4. 讲话转字幕：改截取后字幕跟着移动；解说不覆盖字幕 ==")
sid_sp = insert(pid, VID / "speech.mp4")
r = wait(c.post(f"/api/projects/{pid}/steps/{sid_sp}/video/transcribe", headers=H).json())
sp = next(s for s in steps(pid) if s["id"] == sid_sp)
words = sp["clip"]["words"]
check("转写成功", r["status"] == "done" and words, r.get("error"))


def cues_for(step_json):
    s = Step(**step_json)
    b = clips.clip_words(s.clip)
    return subs.segment_to_cues(subs.Segment(0, clips.clip_length(s.clip), s.subtitle_text(), b))


c0 = cues_for(sp)
first_word = words[0]["t"]
sid_late = insert(pid, V2 / "late_speech.mp4")
wait(c.post(f"/api/projects/{pid}/steps/{sid_late}/video/transcribe", headers=H).json())
late = next(s for s in steps(pid) if s["id"] == sid_late)
cl = cues_for(late)
check("第一句字幕等开口才出来（前 2 秒没说话就不显示）", cl and cl[0].start > 1.8, (cl[0].start if cl else None, late["clip"]["words"][:1]))
c.patch(f"/api/projects/{pid}/steps/{sid_sp}/video", json={"start": 0.5}, headers=H)
sp2 = next(s for s in steps(pid) if s["id"] == sid_sp)
c1 = cues_for(sp2)
check("截取起点往后挪 0.5 秒，字幕也提前 0.5 秒", c1 and abs((c0[-1].end - c1[-1].end) - 0.5) < 0.05,
      (c0[-1].end, c1[-1].end if c1 else None, len(c0)))
# the frontend no longer sends caption when editing the narration; a backend narration edit must not touch the subtitle's word times either
c.patch(f"/api/projects/{pid}/steps/{sid_sp}", json={"narration": "随便写的解说"}, headers=H)
sp3 = next(s for s in steps(pid) if s["id"] == sid_sp)
check("写了解说：字幕和逐词时间都还在", sp3["caption"] == sp["caption"] and sp3["clip"]["words"] == words)
s_obj = Step(**sp3)
check("播原声：字幕 = 转写内容", s_obj.subtitle_text() == sp["caption"])
s_obj.clip.audio = "mute"
check("改成静音配解说：字幕换成解说词", s_obj.subtitle_text() == "随便写的解说")

print("\n== 5. 翻译：原声视频的字幕翻译成目标语言 ==")
from backend.services import script_gen


class FakeLLM:
    def chat_json(self, msgs, temperature=0.3):
        import json as _j, re as _re
        items = _j.loads(msgs[-1]["content"].split("\n\n", 1)[1])
        return {"items": [{"k": it["k"], "t": "EN:" + it["t"]} for it in items]}


pj = storage.load(pid)
cap_before = next(s for s in pj.steps if s.id == sid_sp).caption
script_gen.translate_project(pj, "en-US", client=FakeLLM())
spx = next(s for s in pj.steps if s.id == sid_sp)
check("字幕翻译了，没被解说的译文覆盖", spx.caption == "EN:" + cap_before and spx.narration == "EN:随便写的解说",
      (spx.caption[:30], spx.narration[:30]))

print("\n== 6. 步骤序号：排除的步骤不算 ==")
seen = {}
orig_init = video.StepRenderer.__init__


def spy(self, *a, **k):
    orig_init(self, *a, **k)
    seen[self.step.id] = (self.step_no, self.total_steps)


video.StepRenderer.__init__ = spy
pr = storage.create("序号", "zh-CN")
for i in range(4):
    st_ = Step(kind="manual", title=f"第{i}步", screenshot="")
    st_.index = i
    pr.steps.append(st_)
pr.steps[1].include = False
storage.save(pr)
r = wait(c.post(f"/api/projects/{pr.id}/render", json={"width": 640, "height": 360, "fps": 10}, headers=H).json())
video.StepRenderer.__init__ = orig_init
nums = [seen.get(s.id) for s in pr.steps]
check("序号 1/3、2/3、3/3（第 2 步排除了）", nums == [(1, 3), None, (2, 3), (3, 3)], nums)

print("\n== 7. 并行渲染：一段出错马上停，不等其余几十段画完 ==")
pr2 = storage.create("出错", "zh-CN")
for i in range(24):
    st_ = Step(kind="manual", title=f"第{i}步", narration="x" * 40)
    st_.index = i
    pr2.steps.append(st_)
storage.save(pr2)
started = []
orig_encode = video._encode_clip


def bad_encode(make_frames, *a, **k):
    started.append(a[4].name if len(a) > 4 else "?")
    if len(started) == 3:
        raise video.FFmpegError("模拟编码失败")
    return orig_encode(make_frames, *a, **k)


video._encode_clip = bad_encode
config.save({**config.load(), "render_workers": 3})
t0 = time.time()
r = wait(c.post(f"/api/projects/{pr2.id}/render", json={"width": 640, "height": 360, "fps": 10}, headers=H).json())
video._encode_clip = orig_encode
check("报错", r["status"] == "error" and "模拟编码失败" in r["error"], r.get("error", "")[:60])
check("出错后没再开始新的片段", len(started) <= 6, f"开始了 {len(started)} / 26 段，用时 {time.time() - t0:.1f}s")

print("\n== 8. PPT 里的视频：用硬链接，不再多拷一份 ==")
with open(VID / "deck_with_video.pptx", "rb") as f:
    man = wait(c.post("/api/import/slides", files={"file": ("v.pptx", f)}, headers=H).json())["result"]
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "ignore", "missing": "empty", "language": "zh-CN", "auto_voice": False},
                headers=H).json())
vp = r["result"]["project_id"]
vs = [s for s in steps(vp) if s["kind"] == "video"][0]
mf = clips.media_dir(vp) / vs["clip"]["file"]
check("视频文件是硬链接（和导入目录共用一份数据）", mf.exists() and os.stat(mf).st_nlink == 2, os.stat(mf).st_nlink)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
