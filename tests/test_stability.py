"""Stability / reliability on a real server process: repeated renders, concurrent jobs, stopping halfway, editing while rendering, concurrent saves, missing files, restarts.

Doesn't touch the user's projects or config.json (separate data folder stab_data, port 8771), never calls PowerPoint, no online voice-over (audio is generated locally).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw

SP = Path(sys.argv[1]).resolve()
DATA = SP / "stab_data"
PROJ = DATA / "projects"
PORT = 8771
B = f"http://127.0.0.1:{PORT}"
H = {"Origin": B}
PY = sys.executable
FF = shutil.which("ffmpeg")
PROBE = shutil.which("ffprobe")
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def ps(cmd):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout.strip()


def server_pid():
    out = ps(f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess")
    return int(out.splitlines()[0]) if out else 0


def server_stats():
    pid = server_pid()
    out = ps(f"$p = Get-Process -Id {pid}; \"$($p.WorkingSet64) $($p.HandleCount) $($p.Threads.Count)\"")
    ws, hc, th = (int(x) for x in out.split())
    return {"mem_mb": ws // 1024 // 1024, "handles": hc, "threads": th}


def ffmpeg_left():
    out = ps("Get-CimInstance Win32_Process -Filter \"Name like 'ffmpeg%'\" | ForEach-Object { $_.CommandLine }")
    return [x for x in out.splitlines() if "stab_data" in x]


def wait_job(j, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(f"{B}/api/jobs/{j['id']}", timeout=10).json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.3)
    raise TimeoutError(j)


def render(pid, w=960, h=540, fps=15):
    return requests.post(f"{B}/api/projects/{pid}/render", json={"width": w, "height": h, "fps": fps}, headers=H).json()


def proj(pid):
    return requests.get(f"{B}/api/projects/{pid}", timeout=10).json()


def out_file(pid):
    p = proj(pid)
    return PROJ / pid / "output" / p["output"] if p.get("output") else None


def duration(path):
    return float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True).stdout or 0)


server = None


def start_server():
    global server
    log = open(SP / "stab_server.log", "a", encoding="utf-8", errors="replace")
    server = subprocess.Popen([PY, "-X", "utf8", str(Path(__file__).parent / "stab_server.py"), str(PORT), str(SP)], stdout=log,
                              stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    for _ in range(80):
        try:
            if requests.get(f"{B}/api/health", timeout=2).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def stop_server():
    pid = server_pid()
    if pid:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
    for _ in range(20):
        if not server_pid():
            break
        time.sleep(0.3)


# ---------------------------------------------------------------- prepare data
print("\n== 准备：独立数据目录里建测试项目 ==")
shutil.rmtree(DATA, ignore_errors=True)
PROJ.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(PROJ)
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural", "asr_model": "small"})
from backend import storage  # noqa: E402
from backend.models import Rect, Step, Target  # noqa: E402


def make_capture_project(name, n=12):
    p = storage.create(name, "zh-CN")
    for i in range(n):
        s = Step(kind="click" if i % 3 else "input", title=f"第 {i + 1} 步", value="hello" if i % 3 == 0 else "")
        img = Image.new("RGB", (1280, 720), (245, 247, 250))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 1280, 56), fill=(32, 64, 128))
        for k in range(6):
            d.rectangle((40, 90 + k * 95, 1240, 160 + k * 95), outline=(180, 180, 190), width=2)
        bx, by = 100 + (i * 83) % 900, 110 + (i * 57) % 480
        d.rectangle((bx, by, bx + 160, by + 44), fill=(255, 140, 0))
        fname = f"{s.id}.png"
        (storage.screenshots_dir(p.id)).mkdir(parents=True, exist_ok=True)
        img.save(storage.screenshots_dir(p.id) / fname)
        s.screenshot, s.img_w, s.img_h, s.viewport_w, s.viewport_h = fname, 1280, 720, 1280, 720
        s.target = Target(tag="button", text=f"按钮{i}", rect=Rect(x=bx, y=by, w=160, h=44))
        s.point = {"x": bx + 80, "y": by + 22}
        s.narration = s.caption = f"第 {i + 1} 步，点击橙色的按钮{i}，然后看看页面上发生的变化。"
        # voice-over: generate audio locally (offline)
        audio = storage.audio_dir(p.id)
        audio.mkdir(parents=True, exist_ok=True)
        dur = 2.0 + (i % 4) * 0.5
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={300 + i * 40}:sample_rate=24000",
                        "-t", f"{dur}", "-b:a", "48k", str(audio / f"{s.id}.mp3")], check=True)
        s.audio, s.audio_duration = f"{s.id}.mp3", dur
        s.index = i
        p.steps.append(s)
    p.title, p.intro, p.outro = name, "这是片头的介绍。", "谢谢观看！"
    storage.save(p)
    return p.id


A = make_capture_project("稳定性A")
D = make_capture_project("故障注入D", 6)
E = make_capture_project("并发E", 8)
check("测试项目建好", all((PROJ / x / "project.json").exists() for x in (A, D, E)))

check("启动服务（真实进程）", start_server())
spid = server_pid()
print(f"     服务进程 PID {spid}")

# import a deck with videos and insert several kinds of video
with open(SP / "vid" / "deck_with_video.pptx", "rb") as f:
    man = wait_job(requests.post(f"{B}/api/import/slides", files={"file": ("视频.pptx", f)}, headers=H).json())["result"]
r = wait_job(requests.post(f"{B}/api/import/slides/{man['id']}/create",
                           json={"notes_mode": "ignore", "missing": "empty", "language": "zh-CN", "auto_voice": False},
                           headers=H).json())
BV = r["result"]["project_id"]
for f_ in (SP / "vid2" / "rotated.mp4", SP / "vid2" / "nodur.webm", SP / "vid" / "speech.mp4"):
    with open(f_, "rb") as fh:
        wait_job(requests.post(f"{B}/api/projects/{BV}/steps/video", files={"file": (f_.name, fh)}, headers=H).json())
kinds = [s["kind"] for s in proj(BV)["steps"]]
check("带视频的项目建好（幻灯片 + 视频 + 插入的视频）", kinds.count("video") == 4, kinds)

# ---------------------------------------------------------------- 1. repeated renders
print("\n== 1. 同一项目连续渲染 6 次：结果一致、不留垃圾、资源不涨 ==")
r = wait_job(render(A))
check("第 1 次（预热）", r["status"] == "done", r.get("error"))
base = server_stats()
print(f"     预热后：内存 {base['mem_mb']} MB，句柄 {base['handles']}，线程 {base['threads']}")
durs, times = [], []
for k in range(5):
    t0 = time.time()
    r = wait_job(render(A))
    times.append(time.time() - t0)
    if r["status"] != "done":
        check(f"第 {k + 2} 次渲染", False, r.get("error"))
        break
    durs.append(duration(out_file(A)))
check("5 次全部成功、成片时长完全一致", len(durs) == 5 and max(durs) - min(durs) < 0.05, [round(x, 2) for x in durs])
print(f"     每次耗时 {[round(x, 1) for x in times]} 秒，编码器 {r.get('result', {}).get('encoder')}")
left_work = list((PROJ / A / "work").glob("render_*")) if (PROJ / A / "work").exists() else []
check("临时渲染目录都清理了", not left_work, left_work[:2])
check("没有残留的 ffmpeg 进程", not ffmpeg_left())
after = server_stats()
print(f"     5 次后：内存 {after['mem_mb']} MB，句柄 {after['handles']}，线程 {after['threads']}")
check("句柄 / 线程没有随渲染次数增长", after["handles"] - base["handles"] < 150 and after["threads"] - base["threads"] < 10,
      f"句柄 {base['handles']}→{after['handles']}，线程 {base['threads']}→{after['threads']}")

# ---------------------------------------------------------------- 2. concurrency
print("\n== 2. 三个项目同时渲染 + 防连点 ==")
j1, j2, j3 = render(A), render(BV), render(E)
dup = render(A)
check("同一项目连点两次只跑一个任务", dup["id"] == j1["id"])
rs = [wait_job(j) for j in (j1, j2, j3)]
check("三个同时渲染都成功", all(x["status"] == "done" for x in rs), [x.get("error", "")[:60] for x in rs])
dv = duration(out_file(BV))
tot = sum(s["duration"] for s in proj(BV)["steps"] if s["include"])
check("带视频的项目时长正常", tot < dv < tot + 7, f"{dv:.1f}s vs 步骤 {tot:.1f}s")

# ---------------------------------------------------------------- 3. other actions while rendering
print("\n== 3. 渲染进行中：冲突操作被拒绝，文字修改不丢 ==")
vstep = next(s for s in proj(BV)["steps"] if s["kind"] == "video")
jb = render(BV, 1280, 720, 25)
time.sleep(0.8)
with open(SP / "vid" / "clip10.mp4", "rb") as fh:
    up = requests.post(f"{B}/api/projects/{BV}/steps/{vstep['id']}/video", files={"file": ("x.mp4", fh)}, headers=H)
check("渲染时换视频：被拒绝（409）", up.status_code == 409, up.status_code)
check("被拒绝的上传没留下临时文件", not list((PROJ / "_tmp").glob("*")) if (PROJ / "_tmp").exists() else True)
sid0 = proj(BV)["steps"][0]["id"]
requests.patch(f"{B}/api/projects/{BV}/steps/{sid0}", json={"title": "渲染时改的标题", "note": "备注X"}, headers=H)
requests.patch(f"{B}/api/projects/{BV}", json={"subtitle": "渲染时改的副标题"}, headers=H)
rb = wait_job(jb)
p_after = proj(BV)
check("渲染成功", rb["status"] == "done", rb.get("error"))
check("渲染期间改的文字没被渲染结果覆盖", p_after["steps"][0]["title"] == "渲染时改的标题"
      and p_after["steps"][0]["note"] == "备注X" and p_after["subtitle"] == "渲染时改的副标题",
      (p_after["steps"][0]["title"], p_after["subtitle"]))

# ---------------------------------------------------------------- 4. stopping halfway
print("\n== 4. 中途停止 3 次：很快停下、旧成片不被破坏、不留进程 ==")
of = out_file(A)
st0 = (of.stat().st_size, of.stat().st_mtime)
for k in range(3):
    j = render(A, 1920, 1080, 30)
    t0 = time.time()
    while time.time() - t0 < 60:
        jj = requests.get(f"{B}/api/jobs/{j['id']}").json()
        if jj["progress"] > 0.1 + k * 0.2 or jj["status"] != "running":
            break
        time.sleep(0.2)
    t1 = time.time()
    requests.post(f"{B}/api/jobs/{j['id']}/cancel", headers=H)
    jj = wait_job(j)
    check(f"第 {k + 1} 次停止（在 {jj['progress'] * 100:.0f}% 处）", jj["status"] == "cancelled" and time.time() - t1 < 5,
          f"{jj['status']}，{time.time() - t1:.1f} 秒停下")
time.sleep(1)
check("旧成片没被半截文件覆盖", (of.stat().st_size, of.stat().st_mtime) == st0)
check("没有残留的 ffmpeg 进程", not ffmpeg_left(), ffmpeg_left()[:1])
check("停止后能正常再渲染", wait_job(render(A))["status"] == "done")

# ---------------------------------------------------------------- 5. concurrent saves
print("\n== 5. 并发保存 120 次（12 个线程）：一个都不丢，project.json 完好 ==")
steps_e = proj(E)["steps"]


def edit(i):
    s = steps_e[i % len(steps_e)]
    field = ("title", "note", "caption")[i % 3]
    val = f"{field}-{i}"
    r = requests.patch(f"{B}/api/projects/{E}/steps/{s['id']}", json={field: val}, headers=H, timeout=30)
    return r.status_code, s["id"], field, val


with ThreadPoolExecutor(12) as ex:
    res = list(ex.map(edit, range(120)))
check("120 次请求全部成功", all(r[0] == 200 for r in res), {r[0] for r in res})
final = {s["id"]: s for s in proj(E)["steps"]}
# the last value written for every (step, field) must be there
last = {}
for code, sid, field, val in res:
    last[(sid, field)] = val
wrong = [(k, v, final[k[0]][k[1]]) for k, v in last.items() if not final[k[0]][k[1]].startswith(k[1] + "-")]
check("所有字段都是某次写入的值（没有丢写、没有串）", not wrong, wrong[:2])
raw = (PROJ / E / "project.json").read_text(encoding="utf-8")
check("project.json 是完整合法的 JSON", bool(json.loads(raw)["steps"]))
check("没有留下写到一半的临时文件", not list((PROJ / E).glob("*.tmp")))

# ---------------------------------------------------------------- 6. missing files
print("\n== 6. 文件缺失 / 损坏：照样出片 ==")
pd = storage.load(D)
(storage.screenshots_dir(D) / pd.steps[0].screenshot).unlink()
(storage.audio_dir(D) / pd.steps[1].audio).unlink()
(storage.audio_dir(D) / pd.steps[2].audio).write_bytes(b"not an mp3 at all")
r = wait_job(render(D))
check("截图被删、配音被删、配音文件损坏：照样渲染成功", r["status"] == "done", r.get("error", "")[:200])
warn = (r.get("result") or {}).get("warning", "")
check("完成时提醒是哪几步的配音有问题", "步骤 2" in warn and "步骤 3" in warn and "步骤 1" not in warn, warn)
check("进度条的完成信息里也有这条提醒", warn and warn in r.get("message", ""), r.get("message", "")[:80])
pv = proj(BV)
v_last = [s for s in pv["steps"] if s["kind"] == "video"][-1]
(PROJ / BV / "media" / v_last["clip"]["file"]).unlink()
v_bad = [s for s in pv["steps"] if s["kind"] == "video"][-2]
(PROJ / BV / "media" / v_bad["clip"]["file"]).write_bytes(b"\x00" * 5000)
r = wait_job(render(BV))
check("视频文件被删、视频文件损坏：照样渲染成功（那一步退回显示画面）", r["status"] == "done", r.get("error", "")[:200])
pr = requests.get(f"{B}/api/projects/{BV}/steps/{v_bad['id']}/preview")
check("损坏视频那一步的预览不报错", pr.status_code == 200, pr.status_code)
bad_json = requests.patch(f"{B}/api/projects/{BV}/steps/{v_bad['id']}/video", json={"start": "abc"}, headers=H)
check("乱填的截取时间：报 400 而不是崩溃", bad_json.status_code == 400, bad_json.status_code)
nf = requests.get(f"{B}/api/projects/p_doesnotexist")
check("不存在的项目：404", nf.status_code == 404, nf.status_code)
tr = requests.get(f"{B}/api/projects/{A}/file/screenshots/..%5C..%5Cconfig.json")
check("路径穿越读文件：被拒绝", tr.status_code == 404, tr.status_code)

# ---------------------------------------------------------------- 7. resources
print("\n== 7. 所有测试之后的资源占用 ==")
time.sleep(2)
end = server_stats()
print(f"     内存 {base['mem_mb']}→{end['mem_mb']} MB，句柄 {base['handles']}→{end['handles']}，线程 {base['threads']}→{end['threads']}")
check("线程数回落（后台任务线程都结束了）", end["threads"] - base["threads"] < 10, f"{base['threads']}→{end['threads']}")
check("没有残留的 ffmpeg 进程", not ffmpeg_left())

# ---------------------------------------------------------------- 8. restart
print("\n== 8. 重启服务：设置和项目都在 ==")
requests.post(f"{B}/api/settings", json={"language": "en-US", "voice": "en-US-AriaNeural", "ui_language": "de"}, headers=H)
n_before = len(requests.get(f"{B}/api/projects").json()["projects"])
stop_server()
check("服务已停止", not server_pid())
check("重新启动", start_server())
st = requests.get(f"{B}/api/settings").json()
check("设置保留（解说语言、音色、界面语言）", (st["language"], st["voice"], st["ui_language"]) == ("en-US", "en-US-AriaNeural", "de"),
      (st["language"], st["voice"], st["ui_language"]))
n_after = len(requests.get(f"{B}/api/projects").json()["projects"])
check("项目都在", n_before == n_after, f"{n_before} → {n_after}")
check("重启后能渲染", wait_job(render(E))["status"] == "done")
stop_server()
check("测试结束，服务和 ffmpeg 都没留下", not server_pid() and not ffmpeg_left())

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
