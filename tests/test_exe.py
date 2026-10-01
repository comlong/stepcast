"""Test the packaged exe (not the source): run the main features from a folder with Chinese characters and spaces in its path."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

SP = Path(sys.argv[1])
SRC = Path(__file__).resolve().parents[1] / "dist" / "StepCast"   # the packaged exe folder
ROOT = SP / "exe 测试" / "StepCast"
PORT = 8766
B = f"http://127.0.0.1:{PORT}"
H = {"Origin": B}
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def wait_job(j, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(f"{B}/api/jobs/{j['id']}", timeout=10).json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.5)
    raise TimeoutError(j)


def exe_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VT_", "PYTHON", "VIRTUAL_ENV"))}
    # remove ffmpeg and Python from PATH to simulate a colleague's computer: only the bundled ones can be used
    keep = [p for p in env.get("PATH", "").split(os.pathsep)
            if p and not (Path(p) / "ffmpeg.exe").exists() and not (Path(p) / "python.exe").exists()]
    env["PATH"] = os.pathsep.join(keep)
    env["VT_DISABLE_POWERPOINT"] = "1"
    return env


print("\n== 准备：复制到带中文和空格的目录 ==")
shutil.rmtree(ROOT.parent, ignore_errors=True)
shutil.copytree(SRC, ROOT)
exe = ROOT / "StepCast.exe"
check("exe 存在", exe.exists())
check("扩展文件夹在 exe 旁边", (ROOT / "extension" / "manifest.json").exists())
check("没有带出 config.json / projects", not (ROOT / "config.json").exists() and not (ROOT / "projects").exists())

log = open(SP / "exe_run.log", "w", encoding="utf-8", errors="replace")
t0 = time.time()
proc = subprocess.Popen([str(exe), "--port", str(PORT), "--no-browser"], cwd=str(SP), env=exe_env(),
                        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
try:
    up = False
    for _ in range(120):
        try:
            if requests.get(f"{B}/api/health", timeout=2).ok:
                up = True
                break
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    check("exe 启动成功", up, f"{time.time() - t0:.1f} 秒")
    if not up:
        raise SystemExit(1)

    print("\n== 1. 基本信息 ==")
    h = requests.get(f"{B}/api/health").json()
    want = re.search(r'version="([^"]+)"', (Path(__file__).resolve().parents[1] / "backend" / "main.py").read_text(encoding="utf-8"))[1]
    check("版本和源码一致", h["version"] == want, f"{h['version']} / 源码 {want}")
    check("测试时不会调用本机 PowerPoint", h["powerpoint"] is False)
    if h["powerpoint"] is not False:
        raise SystemExit("PowerPoint 开关没生效，停止测试，免得动到正在打开的 PowerPoint")
    check("用的是包里自带的 ffmpeg", h["ffmpeg"]["ok"] and "_internal" in (h["ffmpeg"].get("path") or ""), h["ffmpeg"].get("path"))
    check("项目目录在 exe 旁边", Path(h["data_dir"]) == ROOT / "projects", h["data_dir"])
    r = requests.get(f"{B}/", headers={"accept-language": "de-DE,de;q=0.9"})
    check("全新安装默认英文界面（不按浏览器语言猜）", r.ok and "⚙ Settings" in r.text and '<html lang="en">' in r.text)
    st = requests.get(f"{B}/api/settings").json()
    check("默认解说语言 / 音色是英语", (st["language"], st["voice"]) == ("en-US", "en-US-AriaNeural"),
          (st["language"], st["voice"]))
    check("静态文件", requests.get(f"{B}/static/app.js").ok and requests.get(f"{B}/static/i18n.js").ok)
    check("能换成中文", requests.post(f"{B}/api/settings", json={"ui_language": "zh", "language": "zh-CN",
                                                                "voice": "zh-CN-XiaoxiaoNeural"}, headers=H).ok)
    check("config.json 写在 exe 旁边", (ROOT / "config.json").exists())

    print("\n== 2. 导入 PDF / PPTX ==")
    with open(SP / "pptspike" / "export_slides.pdf", "rb") as f:
        r = wait_job(requests.post(f"{B}/api/import/slides", files={"file": ("导出.pdf", f)}, headers=H).json())
    check("PDF 解析（pymupdf）", r["status"] == "done" and r["result"]["count"] == 3, r.get("error") or r["result"].get("count"))
    with open(SP / "notes_deck.pptx", "rb") as f:
        r = wait_job(requests.post(f"{B}/api/import/slides", files={"file": ("销售 对话.pptx", f)}, headers=H).json())
    man = r.get("result") or {}
    check("PPTX 解析（python-pptx，读到备注）", r["status"] == "done" and man.get("with_notes") == 3, r.get("error") or man.get("with_notes"))
    upload = next((ROOT / "projects" / "_imports").glob("imp_*/source.pptx"), None)
    check("上传的文件存在 exe 旁边的 projects/_imports", upload is not None, upload)

    print("\n== 3. 生成项目 + 在线配音（edge-tts）==")
    r = wait_job(requests.post(f"{B}/api/import/slides/{man['id']}/create",
                               json={"notes_mode": "verbatim", "missing": "empty", "language": "zh-CN", "auto_voice": True},
                               headers=H).json())
    check("创建项目并配音", r["status"] == "done", r.get("error") or r.get("message"))
    pid = r["result"]["project_id"]
    p = requests.get(f"{B}/api/projects/{pid}").json()
    voiced = [bool(s["audio"]) for s in p["steps"]]
    check("有备注的页都配好音了", voiced == [True, True, False, True], voiced)

    print("\n== 4. 渲染视频（自带 ffmpeg）+ 下载 ==")
    r = wait_job(requests.post(f"{B}/api/projects/{pid}/render", json={"width": 960, "height": 540, "fps": 15}, headers=H).json())
    check("渲染成功", r["status"] == "done", r.get("error"))
    p = requests.get(f"{B}/api/projects/{pid}").json()
    mp4 = ROOT / "projects" / pid / "output" / p["output"]
    check("视频在 exe 旁边的 projects/<id>/output", mp4.exists() and mp4.stat().st_size > 50000, mp4)
    d = requests.get(f"{B}/api/projects/{pid}/file/output/{urllib.parse.quote(p['output'])}?download=true")
    check("下载 MP4", d.ok and len(d.content) == mp4.stat().st_size, d.status_code)
    st2 = requests.get(f"{B}/api/projects/{pid}/subtitles2").json()
    check("第二语言字幕：主字幕记下了、留了位置", st2.get("has_primary") and st2.get("space") and not st2.get("legacy"), st2)
    pk = requests.get(f"{B}/api/projects/{pid}/export/player")
    import io as _io
    import zipfile as _zip
    names = _zip.ZipFile(_io.BytesIO(pk.content)).namelist() if pk.ok else []
    check("网页播放包能下载（播放页 + 视频）", "index.html" in names and p["output"] in names, (pk.status_code, names))
    # the video length must match the step durations: the bundled ffmpeg 7.1 doesn't honour -shortest,
    # which used to keep appending silence and turned a one-minute video into more than an hour
    plan = sum(s["duration"] for s in p["steps"] if s.get("include", True) and s.get("duration"))
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(mp4)], capture_output=True, text=True)
    real = float(r.stdout.strip() or 0)
    check("视频时长正常（不是几十分钟的静音）", plan - 1 < real < plan + 12, f"{real:.1f}s（各步合计 {plan:.1f}s + 片头片尾）")

    print("\n== 5. 语音识别（faster-whisper）==")
    clip = SP / "line0.mp3"
    with open(clip, "rb") as f:
        j = requests.post(f"{B}/api/transcribe", files={"file": ("a.mp3", f)}, data={"language": "zh-CN"}, headers=H).json()
    r = wait_job(j, timeout=900)
    check("识别出文字", r["status"] == "done" and len((r.get("result") or {}).get("text", "")) > 2,
          r.get("error") or (r.get("result") or {}).get("text"))

    print("\n== 6. AI 服务商（只验证 HTTPS 和 SDK 能用，用假 Key，不花钱）==")
    r = requests.post(f"{B}/api/settings/llm-models", json={"provider": "openai", "api_key": "sk-fake-for-test"}, headers=H).json()
    check("OpenAI 兼容（requests + 证书）", not r["ok"] and "401" in r["message"], r["message"][:100])
    r = requests.post(f"{B}/api/settings/llm-models", json={"provider": "anthropic", "api_key": "sk-ant-fake-for-test"}, headers=H).json()
    check("Claude（anthropic SDK）", not r["ok"] and ("401" in r["message"] or "鉴权" in r["message"]), r["message"][:100])

    print("\n== 7. 再双击一次 ==")
    t1 = time.time()
    second = subprocess.run([str(exe), "--port", str(PORT), "--no-browser"], cwd=str(SP), env=exe_env(),
                            capture_output=True, timeout=120)
    out = second.stdout.decode("utf-8", "replace")
    check("第二份直接退出，不重复启动", second.returncode == 0 and ("已经在运行" in out or "already running" in out),
          f"code={second.returncode} {time.time() - t1:.1f}s {out.strip()[-80:]}")
finally:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    log.close()

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
