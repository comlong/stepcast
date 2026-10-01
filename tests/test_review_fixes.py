"""这轮检查修掉的问题：音色跟语言对不上、临时文件清理、健康检查缓存、截图文件句柄。全部在临时目录里做。"""
import os, shutil, sys, time
from pathlib import Path
SP = Path(sys.argv[1]); DATA = SP / "review_data"
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir()
os.environ["VT_DATA_DIR"] = str(DATA / "projects"); os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
from backend import storage
from backend.services import ffmpeg_util, slides
fails = []
def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}" + (f"  —— {d}" if d else "")); c or fails.append(n)

print("== 默认语言 ==")
cfg = config.load()
check("全新安装：界面英语、解说 en-US、音色 Aria", (cfg["ui_language"], cfg["language"], cfg["voice"]) == ("en", "en-US", "en-US-AriaNeural"))
p = storage.create("x")
check("新项目默认英语", (p.language, p.voice) == ("en-US", "en-US-AriaNeural"), (p.language, p.voice))

print("== 音色跟着解说语言走 ==")
config.save({"language": "en-US", "voice": "en-AU-NatashaNeural"})
p = storage.create("边录边讲-德语", "de-DE")
check("德语项目不会用英语音色", p.voice.startswith("de-DE-"), p.voice)
p = storage.create("英式英语", "en-GB")
check("同一种语言保留你选的音色", p.voice == "en-AU-NatashaNeural", p.voice)
p = storage.create("默认")
check("没指定语言用设置里的", (p.language, p.voice) == ("en-US", "en-AU-NatashaNeural"))

print("== 启动清理 ==")
work = storage.work_dir(p.id); stale = work / "render_deadbeef"; stale.mkdir(parents=True)
(stale / "clip_000.mp4").write_bytes(b"x" * 1000)
keep = work / "preview_card_intro.jpg"; keep.write_bytes(b"jpg")
tmp = config.DATA_DIR / "_tmp"; tmp.mkdir(exist_ok=True)
old = tmp / "old_upload.webm"; old.write_bytes(b"x"); os.utime(old, (time.time() - 3 * 86400,) * 2)
new = tmp / "new_upload.webm"; new.write_bytes(b"x")
n = storage.cleanup_temp()
check("渲染中断留下的目录被删", not stale.exists())
check("其他工作文件不动", keep.exists())
check("超过一天的临时上传被删、新的保留", not old.exists() and new.exists(), f"删了 {n} 个")
imp = slides.imports_dir() / "imp_old"; imp.mkdir(parents=True)
os.utime(imp, (time.time() - 3 * 86400,) * 2)
slides.cleanup_old_imports()
check("过期的 PPT 导入被清理", not imp.exists())

print("== 健康检查里的 ffmpeg 缓存 ==")
t = time.time(); a = ffmpeg_util.available(); first = time.time() - t
t = time.time(); b = ffmpeg_util.available(); second = time.time() - t
check("第二次直接用缓存", a == b and second < first / 5, f"{first*1000:.0f} ms -> {second*1000:.2f} ms")

print("== 截图文件句柄 ==")
from PIL import Image
from backend.models import Step
from backend.services.renderer import StepRenderer, Theme
shot = DATA / "shot.png"; Image.new("RGB", (800, 600), (200, 200, 200)).save(shot)
s = Step(kind="click", point={"x": 100, "y": 100}, viewport_w=800, img_w=800)
pt = StepRenderer.click_point(s, shot, Theme(width=1280, height=720))
shot.unlink()                                   # Windows 上文件还被占着的话这里会报错
check("算完点击点后截图文件没被占着（能删掉）", not shot.exists() and pt is not None, pt)
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
