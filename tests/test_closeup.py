"""Close-ups of recorded steps: close enough to read the page, sharp (cut from the full-size screenshot, not enlarged from the frame-sized
picture), redaction kept, and recordings of an ordinary window size zoom as before. Synthetic screenshots, no network."""
import os
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SP = Path(sys.argv[1])
DATA = SP / "closeup_data"
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
from backend.models import Rect, Redaction, Step, Target  # noqa: E402
from backend.services import video  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def page(w, h, dpr):
    """A page with fine 'text': rows of thin dark strokes on white (what small text looks like to a sharpness measure)."""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    step = max(2, int(round(3 * dpr)))
    for y in range(int(40 * dpr), h - int(40 * dpr), int(20 * dpr)):
        for x in range(int(20 * dpr), w - int(20 * dpr), step * 2):
            d.line((x, y, x, y + int(10 * dpr)), fill=(30, 30, 30), width=1)
    return im


def theme(w, h, **extra):
    return video.Theme.from_config({**config.DEFAULTS, "video_width": w, "video_height": h, **extra})


def renderer(img_px, css, rect, th, redactions=()):
    name = f"shot_{img_px[0]}x{img_px[1]}_{len(redactions)}.png"
    page(img_px[0], img_px[1], img_px[0] / css[0]).save(DATA / name)
    st = Step(kind="click", screenshot=name, img_w=img_px[0], img_h=img_px[1], viewport_w=css[0], viewport_h=css[1],
              target=Target(tag="button", text="Add", rect=Rect(**rect)), point={"x": rect["x"] + rect["w"] / 2, "y": rect["y"] + rect["h"] / 2},
              redactions=list(redactions), title="t")
    return video.StepRenderer(step=st, screenshot_path=DATA / name, theme=th, duration=6.0)


def sharpness(im, box):
    """Variance of the Laplacian: high for crisp fine detail, low for a blur."""
    a = np.asarray(im.convert("L").crop(tuple(int(v) for v in box)), np.float32)
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


HOLD = 3.0          # fully zoomed in (after the cursor has moved and the zoom ramp)

print("== 1. 带鱼屏的超宽窗口（3440×1279 截图，网页 2752 像素宽），视频 720p ==")
rect = {"x": 1300.0, "y": 300.0, "w": 80.0, "h": 32.0}
r = renderer((3440, 1279), (2752, 1023), rect, theme(1280, 720))
css = r._css_scale()
check("整页显示时网页 1 像素只剩约 0.43 个视频像素（字会小得看不清）", 0.38 < css < 0.48, round(css, 3))
check("推近到看得清：至少 0.75 个视频像素，超过「放大倍数」1.35", r.zoom_max >= 0.75 / css - 1e-6 and r.zoom_max > 1.35, round(r.zoom_max, 2))
s, ox, oy = r._zoom_at(HOLD)
check("推近时在用这个倍数", abs(s - r.zoom_max) < 1e-6, (s, r.zoom_max))
check("推近用的是按推近倍数画的高清底图", r.stage_hi is not None and abs(r.stage_z - r.zoom_max) < 1e-6
      and r.stage_hi.size == (round(1280 * r.stage_z), round(720 * r.stage_z)), (r.stage_z, r.stage_hi and r.stage_hi.size))
new = r.frame(HOLD)
x0, y0, x1, y1 = r.target
area = ((x0 - ox) * s - 120, (y0 - oy) * s - 60, (x1 - ox) * s + 120, (y1 - oy) * s + 60)
hi = sharpness(new, area)
r.stage_hi = None                                   # the old way: the frame-sized picture enlarged
old = r.frame(HOLD)
lo = sharpness(old, area)
check("推近的画面比放大缩小图清楚得多（细节没有糊掉）", hi > lo * 1.8, f"清晰度 {hi:.0f} 对 {lo:.0f}")
check("高亮框、鼠标照样画在推近后的画面上", new.size == (1280, 720))

print("\n== 2. 打码的地方推近后也看不见 ==")
red = Redaction(x=1250.0, y=280.0, w=40.0, h=80.0)      # CSS pixels, right next to the target
rr = renderer((3440, 1279), (2752, 1023), rect, theme(1280, 720), redactions=[red])
s, ox, oy = rr._zoom_at(HOLD)
f = rr.frame(HOLD)


def box_out(x, y, w, h):
    cx0, cy0 = rr._to_canvas(x, y)
    cx1, cy1 = rr._to_canvas(x + w, y + h)
    return ((cx0 - ox) * s + 4, (cy0 - oy) * s + 4, (cx1 - ox) * s - 4, (cy1 - oy) * s - 4)


inside = sharpness(f, box_out(red.x, red.y, red.w, red.h))
beside = sharpness(f, box_out(red.x - 60, red.y, red.w, red.h))
check("打码区域在推近画面里仍是糊的（高清底图也是用打过码的截图画的）", inside < beside * 0.1, f"打码处 {inside:.0f}，旁边 {beside:.0f}")

print("\n== 3. 普通窗口（1600×900 网页）、1080p：推近和以前一样 ==")
rn = renderer((1600, 900), (1600, 900), {"x": 700.0, "y": 400.0, "w": 120.0, "h": 36.0}, theme(1920, 1080))
check("网页本来就看得清：倍数还是「放大倍数」1.35", abs(rn.zoom_max - 1.35) < 1e-6, round(rn.zoom_max, 3))
check("截图不比画面大：不另画高清底图（照旧）", rn.stage_hi is None, rn.stage_z)
big = renderer((1600, 900), (1600, 900), {"x": 100.0, "y": 100.0, "w": 1400.0, "h": 700.0}, theme(1280, 720))
check("目标几乎是整页：不推近（要看清也不能把目标挤出画面）", big.zoom_max <= 1.02 or big.zoom_max < 1.3, round(big.zoom_max, 3))
off = renderer((3440, 1279), (2752, 1023), rect, theme(1280, 720, zoom_enabled=False))
check("关掉「自动放大」：不推近", off.zoom_max == 1.0 and off.stage_hi is None, off.zoom_max)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
