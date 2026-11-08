"""The skill with StepCast inside (packaging/build_skill.py): size limit, nothing private in it, and the first use with
stepcast.ps1 — notice accepted, work folder, Python environment, settings with the built-in AI off, start / stop, and an update
that replaces only the program. Offline: the environment reuses this .venv's packages (-NoInstall); own port, own folders."""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import imageio_ffmpeg

SP = Path(sys.argv[1]).resolve()
if os.name != "nt" or not shutil.which("powershell"):
    print("== 跳过：需要 Windows PowerShell")
    print("\n结果: 全部通过")
    sys.exit(0)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
import build_skill  # noqa: E402

os.environ["VT_DATA_DIR"] = str(SP / "skill_setup_unused")       # importing the backend below must not point at real projects
from backend.services import first_run  # noqa: E402

DATA = SP / "skill_setup_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
WORK = DATA / "工作 文件夹"            # Chinese and a space: cmd and PowerShell must cope with both
POINTER = DATA / "pointer" / "home.json"
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


print("\n== 1. 打包技能 ==")
skill = build_skill.build(DATA / "skill")
size = sum(f.stat().st_size for f in skill.rglob("*") if f.is_file())
zsize = build_skill.make_zip(skill, DATA / "skill.zip")
check("技能文件夹和压缩包都远小于 100 MB", size < build_skill.MAX_BYTES and zsize < build_skill.MAX_BYTES,
      f"{size / 1048576:.1f} MB / {zsize / 1048576:.1f} MB")
check("没有 config.json、projects、模型", not [p for p in skill.rglob("*") if p.name in ("config.json", "projects", "models")])
req = (skill / "app" / "requirements.txt").read_text(encoding="utf-8")
check("依赖里没有语音识别，ffmpeg 单独按哈希装", "faster-whisper" not in req and "imageio-ffmpeg" not in req
      and "--hash=sha256:" in (skill / "app" / "ffmpeg-requirements.txt").read_text(encoding="utf-8"))
check("依赖里有 numpy（PPT 动画图层要用），pymupdf 不上 2.0", "numpy" in req and "pymupdf>=1.24,<2" in req)
VERSION = build_skill.version()
check("程序版本写在 app/VERSION", (skill / "app" / "VERSION").read_text().strip() == VERSION)
check("技能里有程序、扩展和说明", all((skill / p).exists() for p in
                                     ("SKILL.md", "scripts/stepcast.ps1", "app/app.py", "app/backend/main.py",
                                      "app/static/index.html", "extension/manifest.json")))
LOCALES = {"zh-CN": ("zh", "zh-CN"), "zh-HK": ("zh", "zh-TW"), "zh-Hant-TW": ("zh", "zh-TW"), "de-AT": ("de", "de-DE"),
           "fr-CA": ("fr", "fr-FR"), "pt-BR": ("en", "pt-BR"), "sr-Latn-RS": ("en", "sr-RS"), "ja-JP": ("en", "ja-JP"),
           "xx-YY": ("en", "en-US"), "": ("en", "en-US")}
got = {loc: (first_run.defaults(loc)["ui_language"], first_run.defaults(loc)["language"]) for loc in LOCALES}
check("本机显示语言 → 界面语言和解说语言（表里没有的用英文）", got == LOCALES, {k: v for k, v in got.items() if v != LOCALES[k]})
check("每种都有默认音色，内置 AI 都关", all(first_run.defaults(loc)["voice"] and first_run.defaults(loc)["ai_disabled"]
                                          for loc in LOCALES))
SCRIPT = skill / "scripts" / "stepcast.ps1"
env = dict(os.environ, STEPCAST_POINTER=str(POINTER))
env.pop("VT_HOME", None)
env.pop("VT_DATA_DIR", None)


def ps(*args, env=env):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, cwd=DATA, env=env, timeout=600)
    out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    try:
        js = json.loads(out)
    except ValueError:
        js = None
    return r.returncode, js, err + ("" if js is not None else out)


PORT = free_port()


def build_skill_mirrors():
    return ["https://mirrors.aliyun.com/pypi/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple"]


NOBODY = f"http://127.0.0.1:{free_port()}"       # never the user's own StepCast on 8756

print("\n== 2. 第一次使用：先确认提示、选文件夹 ==")
code, st, err = ps("status", "-Base", NOBODY)
check("没装过：next = first-use", code == 0 and st and st["next"] == "first-use" and st["skill_version"] == VERSION,
      st or err[:200])
code, _, err = ps("setup", "-Base", NOBODY)
check("没给文件夹：让助手去问用户", code == 1 and "-Folder" in err, err[:200])
code, _, err = ps("setup", "-Folder", WORK, "-Base", NOBODY)
check("用户还没同意提示：不装", code == 1 and "-AcceptRisk" in err and not WORK.exists(), err[:200])
code, _, err = ps("setup", "-Folder", skill / "data", "-AcceptRisk", "-Base", NOBODY)
check("工作文件夹不能在技能文件夹里", code == 1 and "inside the skill" in err, err[:200])
code, _, err = ps("setup", "-Folder", WORK, "-AcceptRisk", "-Base", NOBODY)
check("没说网络环境：让助手去问用户是不是在中国大陆", code == 1 and "-Network cn" in err and "-Network global" in err
      and not WORK.exists(), err[:200])
code, _, err = ps("setup", "-Folder", WORK, "-AcceptRisk", "-Network", "moon", "-Base", NOBODY)
check("网络环境只认 cn / global", code == 1 and "-Network" in err and not WORK.exists(), err[:200])

print("\n== 3. 安装到工作文件夹（离线，借用本机 .venv 的组件）==")
code, r, err = ps("setup", "-Folder", WORK, "-AcceptRisk", "-Network", "cn", "-Locale", "de-AT",
                  "-Python", sys.executable, "-NoInstall", "-Base", NOBODY)
check("setup 成功", code == 0 and r and Path(r["work_folder"]) == WORK, err[-500:])
idx = (r or {}).get("package_indexes") or []
check("中国大陆：先阿里云、再清华，官方 PyPI 最后", r and r["network"] == "cn" and idx[:2] == build_skill_mirrors()
      and idx[-1] == "https://pypi.org/simple", idx)
check("ffmpeg 可用", bool(r) and Path(r["ffmpeg"]).exists(), (r or {}).get("ffmpeg"))
rt = WORK / "runtime"
check("程序、Python 环境、扩展都在工作文件夹里", (rt / "app" / "app.py").exists() and (rt / "venv" / "Scripts" / "python.exe").exists()
      and (WORK / "extension" / "manifest.json").exists())
cfg = json.loads((WORK / "config.json").read_text(encoding="utf-8"))
check("设置按指定的语言（de-AT → 德语界面、德语解说），内置 AI 关闭",
      cfg == {"ai_disabled": True, "ui_language": "de", "language": "de-DE", "voice": "de-DE-KatjaNeural"}
      and r["new_settings"] == cfg, cfg)
launcher = (WORK / "StepCast.cmd").read_bytes()
check("StepCast.cmd 只有 ASCII，按自己的位置找程序", launcher.isascii() and b"%~dp0runtime\\venv\\Scripts\\python.exe" in launcher
      and b"VT_HOME=%~dp0." in launcher)
state = json.loads((WORK / "setup.json").read_text(encoding="utf-8"))
check("记下版本、网络环境和同意提示的时间", state["version"] == VERSION and state["accepted_at"] and state["network"] == "cn", state)
check("记住工作文件夹", json.loads(POINTER.read_text(encoding="utf-8"))["home"] == str(WORK))
cfg["server_port"] = PORT
(WORK / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
code, st, err = ps("status")
check("之后不用再给文件夹和地址：next = start，端口取自设置", code == 0 and st and st["next"] == "start"
      and st["base"] == f"http://127.0.0.1:{PORT}" and Path(st["work_folder"]) == WORK and st["network"] == "cn", st or err[:200])
UI_CULTURE = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-UICulture).Name"], capture_output=True,
                            text=True).stdout.strip()
check("status 报告本机显示语言", st and st["computer_language"] == UI_CULTURE, (st or {}).get("computer_language"))

print("\n== 4. 启动 / 停止 ==")
# what an assistant's sandbox does: programs get out only through its proxy, named in HTTPS_PROXY; StepCast, started outside the
# sandbox's commands, must still use it for the online voice. A local listening socket stands in for that proxy
sandbox_proxy = socket.socket()
sandbox_proxy.bind(("127.0.0.1", 0))
sandbox_proxy.listen(4)
SANDBOX = f"http://127.0.0.1:{sandbox_proxy.getsockname()[1]}"
env_sandbox = dict(env, HTTPS_PROXY=SANDBOX)
code, st, err = ps("status", env=env_sandbox)
afile = WORK / "runtime" / "assistant-proxy.txt"
check("每次调用都把助手的代理告诉 StepCast", code == 0 and afile.exists() and afile.read_text(encoding="utf-8") == SANDBOX,
      afile.read_text(encoding="utf-8") if afile.exists() else err[:200])
code, r, err = ps("start")
h = health(PORT)
check("start 起来了，数据在工作文件夹里", code == 0 and h and Path(h["data_dir"]) == WORK / "projects", err[-400:] or h["data_dir"])
check("内置 AI 是关的", bool(h) and h["ai_disabled"] is True)
check("命令结束后 StepCast 还在", health(PORT) is not None, (r or {}).get("started_with"))
code, st, err = ps("status")
check("next = ready", code == 0 and st and st["next"] == "ready", st)
check("没在沙箱环境里启动的 StepCast 也用上了助手的代理（在线语音）", bool(st) and st["server"].get("voice_proxy") == SANDBOX,
      (st or {}).get("server"))
code, r2, err = ps("start")
check("再 start 一次：已经在运行", code == 0 and r2 and r2.get("already") is True, err[:200])
code, _, err = ps("setup")
check("运行中不替换程序", code == 1 and "stop" in err, err[:200])
sandbox_proxy.close()
code, r, err = ps("stop")
check("stop 之后服务没了", code == 0 and r and r["running"] is False and health(PORT) is None, err[:300] or r)
code, r, err = ps("stop")
check("没在运行时 stop 也不报错", code == 0 and r and r["running"] is False, err[:200])

print("\n== 5. 技能更新：只换程序，设置和项目不动 ==")
cfg["accent_color"] = "#123456"
(WORK / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
(WORK / "projects" / "p_keepme").mkdir(parents=True, exist_ok=True)
(rt / "app" / "old_file.py").write_text("# from the old version\n", encoding="utf-8")
state["version"] = "0.0.1"
(WORK / "setup.json").write_text(json.dumps(state), encoding="utf-8")
code, st, err = ps("status")
check("next = update", code == 0 and st and st["next"] == "update" and st["installed_version"] == "0.0.1", st)
t0 = time.time()
code, r, err = ps("setup")
check("更新不用再给文件夹、网络环境，也不用再确认提示", code == 0 and r and r["network"] == "cn", err[-400:])
check("设置没重写", bool(r) and r["new_settings"] is None, (r or {}).get("new_settings"))
check("Python 环境没变就不重装", time.time() - t0 < 60 and "creating the Python environment" not in err, f"{time.time() - t0:.0f}s")
check("程序换成新的", not (rt / "app" / "old_file.py").exists() and (rt / "app" / "app.py").exists())
cfg2 = json.loads((WORK / "config.json").read_text(encoding="utf-8"))
check("设置原样保留", cfg2.get("accent_color") == "#123456" and cfg2.get("server_port") == PORT, cfg2)
check("项目原样保留", (WORK / "projects" / "p_keepme").is_dir())
check("版本更新了", json.loads((WORK / "setup.json").read_text(encoding="utf-8"))["version"] == VERSION)

print("\n== 6. 不在中国大陆、不指定语言：官方 PyPI 优先，按本机显示语言 ==")
WORK2 = DATA / "work2"
code, r, err = ps("setup", "-Folder", WORK2, "-AcceptRisk", "-Network", "global", "-Python", sys.executable, "-NoInstall",
                  "-Base", NOBODY)
idx = (r or {}).get("package_indexes") or []
check("先官方 PyPI，国内镜像兜底", code == 0 and idx[:1] == ["https://pypi.org/simple"] and idx[1:] == build_skill_mirrors(),
      err[-300:] or idx)
cfg3 = json.loads((WORK2 / "config.json").read_text(encoding="utf-8"))
check(f"按本机显示语言（{UI_CULTURE}）", cfg3 == first_run.defaults(UI_CULTURE), cfg3)

print("\n== 7. ffmpeg 下载不了：用本机已有的，或用户下载 / 手动放的 ==")
REAL_FF = Path(imageio_ffmpeg.get_ffmpeg_exe())
no_ff_path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                             if d and not (Path(d) / "ffmpeg.exe").exists())
env_nodl = dict(env, STEPCAST_TEST_NO_DOWNLOAD="1", PATH=no_ff_path)
WORK3 = DATA / "work3"
FF3 = WORK3 / "ffmpeg"
setup3 = ("setup", "-Folder", WORK3, "-AcceptRisk", "-Network", "cn", "-Python", sys.executable, "-NoInstall", "-Base", NOBODY)
code, r, err = ps(*setup3, env=env_nodl)
help_ = (r or {}).get("ffmpeg_help") or {}
check("都下载不了、本机也没有：其余照装，报错并给出下载地址和存放位置",
      code == 1 and r and r["ffmpeg"] == "" and "ffmpeg_help" in err and Path(help_.get("put_into", "")) == FF3
      and (WORK3 / "runtime" / "app" / "app.py").exists() and (WORK3 / "config.json").exists(), err[-300:])
links = help_.get("download") or []
check("中国大陆：给阿里云 / 清华镜像和 npm 镜像的地址", any("mirrors.aliyun.com" in u for u in links)
      and any("tuna.tsinghua" in u for u in links) and any("npmmirror" in u for u in links), links)
code, st, err = ps("status", env=env_nodl)
check("status：next = ffmpeg，也带下载说明", code == 0 and st and st["next"] == "ffmpeg" and st.get("ffmpeg_help"), st or err[:200])

pathbin = DATA / "pathbin"
pathbin.mkdir(exist_ok=True)
shutil.copyfile(REAL_FF, pathbin / "ffmpeg.exe")
env_path = dict(env_nodl, PATH=str(pathbin) + os.pathsep + no_ff_path)
code, r, err = ps(*setup3, env=env_path)
check("本机 PATH 里有 ffmpeg：复制进工作文件夹来用", code == 0 and r and Path(r["ffmpeg"]) == FF3 / "ffmpeg.exe"
      and r["ffmpeg_source"].startswith("this computer"), err[-300:] or r)

shutil.rmtree(FF3)
gz = DATA / "ffmpeg-win32-x64.gz"
with open(REAL_FF, "rb") as src, gzip.open(gz, "wb", compresslevel=1) as dst:
    shutil.copyfileobj(src, dst)
code, r, err = ps(*setup3, "-FfmpegFile", gz, env=env_nodl)
check("用户下载的 .gz（npm 镜像）：解压进工作文件夹", code == 0 and r and Path(r["ffmpeg"]) == FF3 / "ffmpeg.exe"
      and r["ffmpeg_source"] == "work folder", err[-300:] or r)

shutil.rmtree(FF3)
zp = DATA / "ffmpeg-release-essentials.zip"
with zipfile.ZipFile(zp, "w") as z:
    z.write(REAL_FF, "ffmpeg-9.0-essentials_build/bin/ffmpeg.exe")
    z.writestr("ffmpeg-9.0-essentials_build/README.txt", "x")
code, r, err = ps(*setup3, "-FfmpegFile", zp, env=env_nodl)
check("用户下载的 .zip 版本：取出 bin 里的 ffmpeg.exe", code == 0 and r and Path(r["ffmpeg"]) == FF3 / "ffmpeg.exe", err[-300:] or r)

shutil.rmtree(FF3)
fake = DATA / "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"
with zipfile.ZipFile(fake, "w") as z:
    z.write(REAL_FF, "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe")
code, r, err = ps(*setup3, "-FfmpegFile", fake, env=env_nodl)
check("校验值不对的 .whl：拒收", code == 1 and "SHA-256" in err and not (FF3 / "ffmpeg.exe").exists(), err[-300:])
notff = DATA / "not-ffmpeg.exe"
notff.write_bytes(b"MZ not a program")
code, r, err = ps(*setup3, "-FfmpegFile", notff, env=env_nodl)
check("不是能用的 ffmpeg：拒收", code == 1 and "working ffmpeg" in err and not (FF3 / "ffmpeg.exe").exists(), err[-300:])

FF3.mkdir(exist_ok=True)
shutil.copyfile(REAL_FF, FF3 / "ffmpeg.exe")
code, r, err = ps(*setup3, env=env_nodl)
check("用户手动放进 ffmpeg 文件夹：setup 直接用", code == 0 and r and Path(r["ffmpeg"]) == FF3 / "ffmpeg.exe"
      and r["ffmpeg_source"] == "work folder", err[-300:] or r)
PORT3 = free_port()
cfg4 = json.loads((WORK3 / "config.json").read_text(encoding="utf-8"))
cfg4["server_port"] = PORT3
(WORK3 / "config.json").write_text(json.dumps(cfg4), encoding="utf-8")
code, r, err = ps("start")
h = health(PORT3)
check("StepCast 运行时也优先用工作文件夹里的 ffmpeg", code == 0 and h and Path(h["ffmpeg"]["path"]) == FF3 / "ffmpeg.exe",
      err[-300:] or (h or {}).get("ffmpeg"))
code, r, err = ps("stop")
check("停掉", code == 0 and health(PORT3) is None, err[-200:])

print(f"\n结果: {'全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'}")
