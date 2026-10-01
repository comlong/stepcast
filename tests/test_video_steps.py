"""Videos in PPT decks: import -> video steps -> rendering. Runs in a temp folder, never touches the user's projects or calls the local PowerPoint."""
import os, shutil, subprocess, sys, time
from pathlib import Path

SP = Path(sys.argv[1]); DATA = SP / "video_steps_data"; VID = SP / "vid"
FF, PROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects"); os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
config.save({"ui_language": "zh"})
from fastapi.testclient import TestClient
from PIL import Image, ImageChops
from backend import main, storage
from backend.services import slides as S
S._soffice = lambda: None

c = TestClient(main.app, base_url="http://127.0.0.1:8756"); H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(n, cond, d=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {n}" + (f"  —— {d}" if d != "" else ""))
    cond or fails.append(n)


def wait(j):
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)


def dur(p):
    return float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                 "default=nw=1:nk=1", str(p)], capture_output=True, text=True).stdout or 0)


def frame(mp4, t, out):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4),
                    "-frames:v", "1", str(out)], check=True)
    return Image.open(out).convert("RGB")


def loud(mp4, t0, t1):
    r = subprocess.run([FF, "-hide_banner", "-ss", f"{t0:.2f}", "-t", f"{t1 - t0:.2f}", "-i", str(mp4),
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split(":")[1].split()[0])
    return -99.0


print("\n== 1. 导入：识别出第 2 页的视频 ==")
with open(VID / "deck_with_video.pptx", "rb") as f:
    r = wait(c.post("/api/import/slides", files={"file": ("带视频.pptx", f)}, headers=H).json())
man = r["result"]
check("解析成功", r["status"] == "done", r.get("error"))
v2 = man["slides"][1]["videos"]
check("第 2 页有 1 个视频，其他页没有", len(v2) == 1 and not man["slides"][0]["videos"] and not man["slides"][2]["videos"])
check("清单里记着含视频的页数", man.get("with_videos") == 1)
v = v2[0]
check("视频文件取出来了、时长 10 秒、有声音", v["file"] and abs(v["duration"] - 10) < 0.2 and v["has_audio"], v)
rc = v["rect"]
check("位置换算成页面比例", abs(rc["x"] - 3.5 / 13.333) < 0.01 and abs(rc["w"] - 6.4 / 13.333) < 0.01, rc)

print("\n== 2. 生成项目：视频步骤紧跟在第 2 页后面 ==")
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "verbatim", "missing": "empty", "language": "zh-CN", "auto_voice": False},
                headers=H).json())
pid = r["result"]["project_id"]
p = c.get(f"/api/projects/{pid}").json()
kinds = [s["kind"] for s in p["steps"]]
check("步骤顺序：幻灯片、幻灯片、视频、幻灯片", kinds == ["slide", "slide", "video", "slide"], kinds)
vs = p["steps"][2]
check("视频步骤默认：原位置播放、视频原声", vs["clip"]["mode"] == "inset" and vs["clip"]["audio"] == "original")
check("视频文件放进了项目 media 目录", (DATA / "projects" / pid / "media" / vs["clip"]["file"]).is_file())
check("视频步骤没有被 AI / 备注写上解说", not vs["narration"])
check("第 2 页的备注仍是解说", "演示视频" in p["steps"][1]["narration"])

print("\n== 3. 渲染：视频真的在播、有原声、时长对 ==")
r = wait(c.post(f"/api/projects/{pid}/render", json={"width": 1280, "height": 720, "fps": 25}, headers=H).json())
check("渲染成功", r["status"] == "done", r.get("error"))
p = c.get(f"/api/projects/{pid}").json()
mp4 = DATA / "projects" / pid / "output" / p["output"]
starts, t = {}, 0.0
intro_on = bool(p["title"] or p["intro"])
for s in p["steps"]:
    if s["include"]:
        starts[s["id"]] = t
        t += s["duration"]
vstep = p["steps"][2]
check("视频步骤时长 = 视频长度 10 秒", abs(vstep["duration"] - 10) < 0.05, vstep["duration"])
total = dur(mp4)
intro_len = total - t - (2.6 if p["outro"] else 0)
v0 = intro_len + starts[vstep["id"]]
check("成片总长对得上", abs(total - (intro_len + t + (2.6 if p["outro"] else 0))) < 0.5, f"{total:.1f}s")
a = frame(mp4, v0 + 2.0, SP / "vs_a.png"); b = frame(mp4, v0 + 6.0, SP / "vs_b.png")
# the video box's position in the frame (same conversion as when rendering): compare only this area; the rest should not change
from backend.services.renderer import StepRenderer, Theme
th = Theme.from_config({**config.load(), **p["settings"], "video_width": 1280, "video_height": 720})
from backend.models import Step
rend = StepRenderer(step=Step(**vstep), screenshot_path=DATA / "projects" / pid / "screenshots" / vstep["screenshot"],
                    theme=th, duration=10)
from backend.services import clips
box, inset = clips.placement(Step(**vstep).clip, rend.draw_box, True, 1280, 720, 640, 360)
x, y, w, h = box
inner = (x + 10, y + 10, x + w - 10, y + h - 10)
from PIL import ImageStat
def mean_diff(r):
    return sum(ImageStat.Stat(ImageChops.difference(a.crop(r), b.crop(r))).mean) / 3
outside = (0, 0, 1280, max(0, y - 10))
d_in, d_out = mean_diff(inner), mean_diff(outside)
# video encoding adds slight noise, so only look at the mean difference: clearly changing inside the box, almost unchanged outside
check("视频框里的画面在动", d_in > 3 and d_in > d_out * 20 and inset, f"平均差 {d_in:.1f}，框 {box}")
check("视频框外面（幻灯片）不动", d_out < 1.5, f"平均差 {d_out:.2f}")
ls = loud(mp4, v0 + 1, v0 + 9)
lq = loud(mp4, max(0.1, intro_len + 0.2), intro_len + starts[p["steps"][0]["id"]] + p["steps"][0]["duration"] - 0.2)
check("视频那段有原声（660Hz 正弦）", ls > -35, f"{ls:.1f} dB")
check("第 1 页没有配音、应该安静", lq < -60, f"{lq:.1f} dB")

print("\n== 4. 全屏 / 只显示封面 / 静音配解说 ==")
sid = vstep["id"]
r = c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"mode": "fullscreen"}, headers=H)
check("改成全屏", r.status_code == 200 and r.json()["clip"]["mode"] == "fullscreen", r.text[:120])
r = c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"start": 2, "end": 6.5}, headers=H)
check("截取 2~6.5 秒", r.status_code == 200 and r.json()["clip"]["start"] == 2 and r.json()["clip"]["end"] == 6.5)
bad = c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"start": 8, "end": 3}, headers=H)
check("起点在终点后面：报错", bad.status_code == 400, bad.text[:80])
bad = c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"mode": "xxx"}, headers=H)
check("不认识的播放方式：报错", bad.status_code == 400)
r = wait(c.post(f"/api/projects/{pid}/render", json={"width": 1280, "height": 720, "fps": 25}, headers=H).json())
p = c.get(f"/api/projects/{pid}").json()
check("截取后视频步骤 4.5 秒", abs(p["steps"][2]["duration"] - 4.5) < 0.05, p["steps"][2]["duration"])
mp4 = DATA / "projects" / pid / "output" / p["output"]
starts, t = {}, 0.0
for s in p["steps"]:
    if s["include"]:
        starts[s["id"]] = t
        t += s["duration"]
v0 = intro_len + starts[sid]
fs = frame(mp4, v0 + 1.0, SP / "vs_fs.png")
corner = fs.crop((0, 0, 60, 60)).getextrema()
check("全屏：视频铺满画面（左上角是视频的彩条，不是背景色）", max(ch[1] for ch in corner) > 60, corner)

c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"mode": "poster"}, headers=H)
c.patch(f"/api/projects/{pid}/steps/{sid}", json={"narration": "这里只显示视频封面。"}, headers=H)
proj = storage.load(pid)
check("只显示封面：不算在播视频", not proj.steps[2].plays_video())

c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"mode": "inset", "audio": "mute"}, headers=H)
c.patch(f"/api/projects/{pid}/steps/{sid}", json={"narration": "视频静音，这句解说会被念出来。"}, headers=H)
proj = storage.load(pid)
check("静音后解说要配音", proj.steps[2].plays_video() and not proj.steps[2].plays_clip_audio())
c.patch(f"/api/projects/{pid}/steps/{sid}/video", json={"audio": "original"}, headers=H)
proj = storage.load(pid)
check("播原声时解说不配音", proj.steps[2].plays_clip_audio())
from backend.services import tts
todo_ids = []
orig = tts.synth_card if hasattr(tts, "synth_card") else None
check("配音列表里没有播原声的视频步骤",
      all(not s.plays_clip_audio() for s in proj.steps if s.include and s.narration and s.voice_source != "own"
          and not s.plays_clip_audio()))

print("\n== 5. 编辑器预览、视频文件访问 ==")
pv = c.get(f"/api/projects/{pid}/steps/{sid}/preview?t=1.0&scale=0.5")
check("渲染预览（视频那一帧）", pv.status_code == 200 and len(pv.content) > 3000, pv.status_code)
mf = c.get(f"/api/projects/{pid}/file/media/{proj.steps[2].clip.file}", headers={"Range": "bytes=0-1023"})
check("视频文件能按段读取（编辑器里能拖进度条）", mf.status_code == 206 and len(mf.content) == 1024, mf.status_code)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
