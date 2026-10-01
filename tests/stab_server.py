"""稳定性测试用的真实服务进程：独立数据目录和配置，不碰用户的项目和 config.json，不调用 PowerPoint。"""
import os
import sys
from pathlib import Path

SP = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "_work"   # 数据放在 <工作目录>/stab_data
os.environ["VT_DATA_DIR"] = str(SP / "stab_data" / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = SP / "stab_data" / "config.json"
config._cache = None
import uvicorn  # noqa: E402

from backend.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8771, log_level="warning")
