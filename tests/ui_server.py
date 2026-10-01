"""Test server: separate data folder and settings file, never touches the user's projects or config.json and never calls the local PowerPoint.

With VT_SLOW_SETTINGS=1 the "read settings" endpoint is delayed by 0.8 s on purpose, to reproduce "language list first, settings later".
"""
import asyncio, os, sys
from pathlib import Path
SP = Path(__file__).parent / "_work"   # data lives in tests/_work/ui_data (not committed)
os.environ["VT_DATA_DIR"] = str(SP / "ui_data")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = SP / "ui_data" / "config.json"; config._cache = None
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from backend.main import app


class SlowSettings(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/api/settings" and request.method == "GET":
            await asyncio.sleep(0.8)
        return await call_next(request)


uvicorn.run(app, host="127.0.0.1", port=8770, log_level="warning")
