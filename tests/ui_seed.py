"""Create two projects for the UI test server: a slide project and a recorded project (writes ui_data only)."""
import os
import sys
from pathlib import Path

from PIL import Image

SP = Path(__file__).parent / "_work"   # data lives in tests/_work/ui_data (not committed)
os.environ["VT_DATA_DIR"] = str(SP / "ui_data")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = SP / "ui_data" / "config.json"
config._cache = None
from backend import storage  # noqa: E402
from backend.models import Step  # noqa: E402

out = {}
for name, src in (("PPT 换语言测试", "slides"), ("网页录制测试", "capture")):
    p = storage.create(name, "zh-CN")
    p.source = src
    Image.new("RGB", (1280, 720), (240, 240, 250)).save(storage.screenshots_dir(p.id) / "s0.png")
    p.steps = [Step(kind="slide" if src == "slides" else "click", screenshot="s0.png", img_w=1280, img_h=720,
                    viewport_w=1280, viewport_h=720, narration="中文解说", caption="中文解说")]
    storage.save(p)
    out[src] = p.id
print(out)
