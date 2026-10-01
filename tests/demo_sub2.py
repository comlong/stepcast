"""Look at second-language subtitles by hand: render a video from the fake recorded project, generate English (italic) and Traditional Chinese (upright) second subtitles
(hand-written translations, no AI credits) and unpack the web player package. Data goes to tests/_work/ui_data (the ui-test server opens it directly),
the package to tests/_work/pkg (pkg in .claude/launch.json).

    .venv\\Scripts\\python.exe -X utf8 tests\\demo_sub2.py
"""
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "_work"
DATA = WORK / "ui_data"
os.environ["VT_DATA_DIR"] = str(DATA)
os.environ["VT_DISABLE_POWERPOINT"] = "1"
sys.path.insert(0, str(HERE.parent))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
from backend import storage  # noqa: E402
from backend.services import second_subs, video  # noqa: E402
from fixtures_build import make_capture_project  # noqa: E402

EN = {
    "这段演示": "This demo shows you how to create a new organization in the sample admin panel in just a few steps.",
    "首先打开": "First open the admin page and click Platform Management in the menu on the left.",
    "展开以后": "Once it expands, find Organization Management below and click it.",
    "这里列出": "All organizations are listed here. Let's open the details of the first row.",
    "看完以后": "Then click the Add button in the top right corner to create a new organization.",
    "在弹出": "In the form that opens, first click the Organization Code field.",
    "输入机构编码": "Enter the organization code. It must be unique across the whole platform.",
    "接着点击": "Next click the Organization Name field to fill in the name.",
    "最后检查": "Finally check everything and click Save. The new organization is ready.",
    "好了": "That's it, the new organization has been created. Thanks for watching!",
}
TW = {"这段演示": "這段示範帶你在示例後台裡新建一個機構，只需要幾步就能完成。", "首先打开": "首先打開後台管理頁面，在左側選單裡點擊「平台管理」。",
      "展开以后": "展開以後，在下面找到「機構管理」，點擊進入。", "这里列出": "這裡列出了所有機構，我們先點開第一列的詳情看看。",
      "看完以后": "看完以後，點擊右上角的「新增」按鈕，開始建立一個新的機構。", "在弹出": "在彈出的表單裡，先點一下「機構編碼」這一欄。",
      "输入机构编码": "輸入機構編碼，編碼在整個平台裡不能重複。", "接着点击": "接著點擊「機構名稱」這一欄，準備填寫名稱。",
      "最后检查": "最後檢查一遍，確認無誤後點擊「儲存」，新機構就建好了。", "好了": "好了，新的機構已經建立完成，感謝觀看。"}


class Hand:
    """Fake translation: look up the hand-written translation by the start of the sentence."""

    def chat_json(self, messages, **k):
        u = messages[-1]["content"]
        table = TW if "繁體" in u.split("\n", 1)[0] else EN
        items = json.loads(u[u.index("\n\n") + 2:])
        return {"items": [{"i": it["i"], "t": next((v for key, v in table.items() if it["t"].startswith(key)), it["t"])}
                          for it in items]}


pid = make_capture_project(DATA)
res = video.render_project(storage.load(pid), overrides={"video_width": 1920, "video_height": 1080, "video_fps": 30})
proj = storage.load(pid)
proj.output = res["file"]
storage.save(proj)
tracks = second_subs.generate(storage.load(pid), ["en-US", "zh-TW"], client=Hand())
proj = storage.load(pid)
second_subs.merge_tracks(proj, tracks)
storage.save(proj)
z = second_subs.export_package(storage.load(pid))
out = WORK / "pkg"
shutil.rmtree(out, ignore_errors=True)
zipfile.ZipFile(z).extractall(out)
z.unlink()
print("项目", pid, "播放包", out, sorted(os.listdir(out)))
