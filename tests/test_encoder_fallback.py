"""显卡编码中途挂掉时：这一段退回 CPU，最后合并要重编，成品仍然正常。"""
import os, shutil, subprocess, sys, time
from pathlib import Path
SP = Path(sys.argv[1]); DATA = SP / "fallback_projects"
PROBE, FF = shutil.which("ffprobe"), shutil.which("ffmpeg")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
from fixtures_build import make_capture_project  # 假的网页录制项目，不用真实项目
PID = make_capture_project(DATA)
from backend import storage
from backend.services import ffmpeg_util, video as V

os.environ["PATH"] = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                                     if p and not (Path(p) / "ffmpeg.exe").exists())
for f in (ffmpeg_util.ffmpeg_bin, ffmpeg_util._listed_encoders, ffmpeg_util.pick_encoder, ffmpeg_util._probe, ffmpeg_util._bundled_ffmpeg):
    f.cache_clear()
enc = ffmpeg_util.pick_encoder("auto")
print("自动挑到:", enc)
if enc == "libx264":
    print("这台电脑没有可用的显卡编码器，跳过（结果: 通过）")
    sys.exit(0)

# 让第 2 段的显卡编码失败
real_args, calls = ffmpeg_util.encoder_args, {"n": 0}
def flaky(name):
    if name == enc:
        calls["n"] += 1
        if calls["n"] == 2:
            return ["-preset", "这是个坏参数"]
    return real_args(name)
ffmpeg_util.encoder_args = flaky

proj = storage.load(PID)
t0 = time.time()
r = V.render_project(proj, overrides=dict(video_width=960, video_height=540, video_fps=15,
                                          render_workers=1, video_encoder="auto"))
out = storage.output_dir(PID) / r["file"]
d = float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(out)], capture_output=True, text=True).stdout.strip() or 0)
print(f"渲染完成 {time.time()-t0:.0f}s  编码器字段={r['encoder']}  时长={d:.1f}s  大小={out.stat().st_size/1e6:.2f}MB")
ok = r["encoder"] == "mixed" and abs(d - r["duration"]) < 1.5
# 成品能正常解码到最后一帧
p = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-v", "error", "-i", str(out), "-f", "null", "-"],
                   capture_output=True, text=True)
print("完整解码检查:", "没有报错" if p.returncode == 0 and not p.stderr.strip() else f"有问题: {p.stderr[:200]}")
print("结果:", "通过" if ok and p.returncode == 0 and not p.stderr.strip() else "失败")
