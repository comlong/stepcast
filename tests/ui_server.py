"""测试用服务：独立的数据目录和配置文件，不碰用户自己的项目和 config.json，也不调用本机 PowerPoint。

VT_SLOW_SETTINGS=1 时把「读设置」接口故意拖慢 0.8 秒，复现「语言列表先到、设置后到」的时序。
"""
import asyncio, os, sys
from pathlib import Path
SP = Path(__file__).parent / "_work"   # 数据放在 tests/_work/ui_data（不提交）
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
