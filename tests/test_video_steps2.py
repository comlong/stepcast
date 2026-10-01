"""视频步骤的其余情况：剪辑设置、链接 / 在线视频补传、组合里的视频、插入视频、讲话转字幕、删除、渲染中途停止。"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

SP = Path(sys.argv[1]); DATA = SP / "video_steps2_data"; VID = SP / "vid"
PROBE = shutil.which("ffprobe")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects"); os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
config.save({"ui_language": "zh", "asr_model": "small"})
from fastapi.testclient import TestClient
from backend import main, storage
from backend.services import slides as S
S._soffice = lambda: None

c = TestClient(main.app, base_url="http://127.0.0.1:8756"); H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(n, cond, d=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {n}" + (f"  —— {d}" if d != "" else ""))
    cond or fails.append(n)


def wait(j, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)
    raise TimeoutError


def ffmpeg_procs_on(path_part):
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name like 'ffmpeg%'\" | ForEach-Object { $_.CommandLine }"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if path_part.lower() in l.lower()]


print("\n== 1. 导入：各种视频 ==")
with open(VID / "deck_cases.pptx", "rb") as f:
    man = wait(c.post("/api/import/slides", files={"file": ("cases.pptx", f)}, headers=H).json())["result"]
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "ignore", "missing": "empty", "language": "zh-CN", "auto_voice": False},
                headers=H).json())
res = r.get("result") or {}
check("没配 AI 也能导入：项目照样生成，只给出提示", r["status"] == "done" and "API Key" in (res.get("warning") or ""),
      r.get("error") or res.get("warning", "")[:60])
pid = res["project_id"]
p = c.get(f"/api/projects/{pid}").json()
kinds = [s["kind"] for s in p["steps"]]
check("5 页 + 4 个视频步骤，各跟在自己那页后面", kinds == ["slide", "slide", "video", "slide", "video",
                                                         "slide", "video", "slide", "video"], kinds)
v_trim, v_link, v_online, v_group = p["steps"][2], p["steps"][4], p["steps"][6], p["steps"][8]
check("PowerPoint 的剪辑：2 秒到 7 秒", v_trim["clip"]["start"] == 2 and v_trim["clip"]["end"] == 7, v_trim["clip"])
check("链接的视频：没有文件、先不放进成片", not v_link["clip"]["file"] and not v_link["include"]
      and v_link["clip"]["missing"] == "linked" and "product-demo.mp4" in v_link["clip"]["source"])
check("在线视频：没有文件、先不放进成片", not v_online["clip"]["file"] and not v_online["include"]
      and v_online["clip"]["missing"] == "online")
g = v_group["clip"]["rect"]
check("组合里的视频位置换算正确", abs(g["x"] - 0.6) < 0.01 and abs(g["w"] - 0.3) < 0.01, g)

print("\n== 2. 给链接的视频补传文件 ==")
with open(VID / "clip10.mp4", "rb") as f:
    r = wait(c.post(f"/api/projects/{pid}/steps/{v_link['id']}/video", files={"file": ("product-demo.mp4", f)},
                    headers=H).json())
check("上传成功", r["status"] == "done", r.get("error"))
st = next(s for s in c.get(f"/api/projects/{pid}").json()["steps"] if s["id"] == v_link["id"])
check("有了文件、自动放进成片、位置保留", st["clip"]["file"] and st["include"] and not st["clip"]["missing"]
      and st["clip"]["mode"] == "inset" and st["clip"]["rect"] is not None, st["clip"])
bad = c.post(f"/api/projects/{pid}/steps/{v_link['id']}/video", files={"file": ("x.txt", b"hello")}, headers=H)
check("不是视频格式：直接拒绝", bad.status_code == 400, bad.text[:80])
with open(VID / "clip10.mp4", "rb") as f:
    bad2 = wait(c.post(f"/api/projects/{pid}/steps/{v_link['id']}/video", files={"file": ("fake.mp4", b"not a video" * 100)},
                       headers=H).json())
check("坏视频文件：报清楚的错，原来的视频不动", bad2["status"] == "error" and "视频" in bad2["error"], bad2.get("error"))

print("\n== 3. 插入一段视频（放在第 1 页后面）==")
with open(VID / "speech.mp4", "rb") as f:
    r = wait(c.post(f"/api/projects/{pid}/steps/video", files={"file": ("讲解.mp4", f)},
                    data={"after": p["steps"][0]["id"]}, headers=H).json())
check("插入成功", r["status"] == "done", r.get("error"))
p = c.get(f"/api/projects/{pid}").json()
ins = p["steps"][1]
check("插在第 1 页后面、全屏、没有原位置", ins["kind"] == "video" and ins["clip"]["mode"] == "fullscreen"
      and ins["clip"]["rect"] is None and ins["title"] == "讲解", (ins["kind"], ins["clip"]["mode"], ins["title"]))
bad = c.patch(f"/api/projects/{pid}/steps/{ins['id']}/video", json={"mode": "inset"}, headers=H)
check("没有原位置的视频不能选「原位置播放」", bad.status_code == 400, bad.json().get("detail"))

print("\n== 4. 视频里的讲话转成字幕 ==")
r = wait(c.post(f"/api/projects/{pid}/steps/{ins['id']}/video/transcribe", headers=H).json(), timeout=900)
st = next(s for s in c.get(f"/api/projects/{pid}").json()["steps"] if s["id"] == ins["id"])
check("识别出字幕、有逐字时间（存在视频里，不占解说配音的时间）",
      r["status"] == "done" and len(st["caption"]) > 3 and st["clip"]["words"]
      and st["clip"]["transcript"] == st["caption"] and not st["boundaries"],
      r.get("error") or st["caption"])
check("解说词没被改动（播原声时解说不念）", st["narration"] == "")

print("\n== 5. 渲染：各段时长 ==")
r = wait(c.post(f"/api/projects/{pid}/render", json={"width": 960, "height": 540, "fps": 15}, headers=H).json())
check("渲染成功", r["status"] == "done", r.get("error"))
p = c.get(f"/api/projects/{pid}").json()
by_id = {s["id"]: s for s in p["steps"]}
check("剪辑后的视频 5 秒", abs(by_id[v_trim["id"]]["duration"] - 5) < 0.05, by_id[v_trim["id"]]["duration"])
check("补传的视频 10 秒", abs(by_id[v_link["id"]]["duration"] - 10) < 0.05)
check("在线视频（没文件）没进成片", not by_id[v_online["id"]]["include"])
mp4 = DATA / "projects" / pid / "output" / p["output"]
d = float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                          str(mp4)], capture_output=True, text=True).stdout or 0)
steps_total = sum(s["duration"] for s in p["steps"] if s["include"])
check("成片时长 = 各步合计 + 片头片尾", steps_total < d < steps_total + 7, f"{d:.1f}s vs 步骤 {steps_total:.1f}s")

print("\n== 6. 渲染中途停止：解码视频的 ffmpeg 不能留下 ==")
job = c.post(f"/api/projects/{pid}/render", json={"width": 1920, "height": 1080, "fps": 30}, headers=H).json()
for _ in range(100):
    j = c.get(f"/api/jobs/{job['id']}").json()
    if j["progress"] > 0.15 or j["status"] != "running":
        break
    time.sleep(0.2)
c.post(f"/api/jobs/{job['id']}/cancel", headers=H)
j = wait(job)
time.sleep(1.5)
left = ffmpeg_procs_on(str(DATA))
check("停下来了", j["status"] == "cancelled", j["status"])
check("没有残留的 ffmpeg 进程", not left, left[:2])

print("\n== 7. 删除视频步骤：视频文件一起删 ==")
media = DATA / "projects" / pid / "media"
f_ins = media / ins["clip"]["file"]
check("删除前文件在", f_ins.exists())
c.delete(f"/api/projects/{pid}/steps/{ins['id']}", headers=H)
check("删除后视频文件没了", not f_ins.exists())
check("别的视频文件还在", any(media.iterdir()))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
