"""The real server process for the stability test: separate data folder and settings, never touches the user's projects or config.json, never calls PowerPoint."""
import os
import sys
from pathlib import Path

SP = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "_work"   # data lives in <work folder>/stab_data
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
