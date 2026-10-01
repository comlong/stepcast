"""Create a two-person Q&A slide project for the UI test server (writes ui_data only)."""
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SP = Path(__file__).parent / "_work"   # data lives in tests/_work/ui_data (not committed)
os.environ["VT_DATA_DIR"] = str(SP / "ui_data")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = SP / "ui_data" / "config.json"
config._cache = None
from backend import storage  # noqa: E402
from backend.models import DialogueLine, Step, join_lines  # noqa: E402
from backend.services import dialogue  # noqa: E402

p = storage.create("两人问答测试", "zh-CN")
p.source = "slides"
p.settings = {"dialogue": True, "slides_notes_mode": "reference", "zoom_enabled": False, "show_cursor": False,
              "browser_frame": False, "show_step_badge": False}
p.speakers = dialogue.default_speakers("zh-CN")
p.voice = p.speakers[0].voice
img = Image.new("RGB", (1600, 900), (245, 247, 252))
d = ImageDraw.Draw(img)
d.rectangle((80, 60, 1100, 150), fill=(30, 40, 60))
for k in range(3):
    d.rounded_rectangle((80 + k * 500, 300, 520 + k * 500, 700), 24, fill=[(255, 120, 80), (80, 160, 255), (60, 190, 120)][k])
img.save(storage.screenshots_dir(p.id) / "s0.png")
lines = [DialogueLine(who="host", text="这一页讲的三件事，分别是什么？"),
         DialogueLine(who="expert", text="第一是选定共性案例，第二是确定重点部门，第三是发布红线清单。"),
         DialogueLine(who="host", text="听起来是先试点、再推广？"),
         DialogueLine(who="expert", text="对，先把样板做出来，再逐步复制。")]
st = Step(kind="slide", screenshot="s0.png", img_w=1600, img_h=900, viewport_w=1600, viewport_h=900, zoom=False,
          highlight=False, page_title="三件事", slide_text="选定共性案例 / 确定重点部门 / 发布红线清单",
          slide_notes="讲清楚三件事的顺序。", lines=lines, title="三件事")
st.narration = st.caption = join_lines(lines)
p.steps = [st]
storage.save(p)
print(p.id)
