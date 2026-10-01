"""并行渲染出来的画面必须和串行一模一样（光标起点是我改过的地方）。"""
import os, shutil, subprocess, sys, time
from pathlib import Path
SP = Path(sys.argv[1]); DATA = SP / "verify_projects"
PROBE = shutil.which("ffprobe"); FF = shutil.which("ffmpeg")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
from fixtures_build import make_capture_project  # 假的网页录制项目，不用真实项目
PID = make_capture_project(DATA)
from backend import storage
from backend.services import video as V

proj = storage.load(PID)
outs = {}
for tag, workers in (("串行", 1), ("并行", 4)):
    t0 = time.time()
    r = V.render_project(proj, overrides=dict(video_width=960, video_height=540, video_fps=15,
                                              render_workers=workers, video_encoder="cpu"))
    src = storage.output_dir(PID) / r["file"]
    dst = SP / f"verify_{tag}.mp4"; shutil.copyfile(src, dst); outs[tag] = dst
    print(f"{tag}（{workers} 线程）: {time.time()-t0:5.1f}s  成品 {dst.stat().st_size/1e6:.2f}MB")

# 抽帧逐像素比对
bad = 0
for t in [x for x in (1.0, 4.0, 9.0, 18.0, 30.0, 45.0, 60.0, 66.0) if x < r["duration"] - 0.5]:
    pngs = []
    for tag, f in outs.items():
        png = SP / f"vf_{tag}_{t}.png"
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(f),
                        "-frames:v", "1", str(png)], check=True)
        pngs.append(png)
    from PIL import Image, ImageChops
    a, b = Image.open(pngs[0]).convert("RGB"), Image.open(pngs[1]).convert("RGB")
    diff = ImageChops.difference(a, b).getbbox()
    same = diff is None
    if not same:
        stat = ImageChops.difference(a, b).convert("L").getextrema()
        bad += 1
        print(f"  t={t:5.1f}s  不一致！最大像素差 {stat[1]}  区域 {diff}")
    else:
        print(f"  t={t:5.1f}s  完全一致")
    for p in pngs: p.unlink(missing_ok=True)
print("\n结论：" + ("并行与串行画面完全一致" if not bad else f"有 {bad} 处不一致，要查"))
