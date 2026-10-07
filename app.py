"""StepCast entry point.

Usage:
    python app.py                 # start the service and open the editor
    python app.py --port 9000
    python app.py --no-browser
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import threading
import time
import urllib.request
import webbrowser

from backend import config


def _quiet_connection_resets() -> None:
    """On Windows, a browser that closes a connection in the middle of a transfer (seeking or stopping a video, closing the tab) makes asyncio print
    a long traceback for a harmless ConnectionResetError. Swallow exactly that error when the transport is torn down."""
    if sys.platform != "win32":
        return
    try:
        from asyncio import proactor_events
        transport = proactor_events._ProactorBasePipeTransport
        original = transport._call_connection_lost

        def _call_connection_lost(self, exc):
            try:
                original(self, exc)
            except (ConnectionResetError, ConnectionAbortedError):
                pass
        transport._call_connection_lost = _call_connection_lost
    except Exception:
        pass


def main() -> int:
    # when output is redirected to a file or the console isn't UTF-8, printing characters like ✓ must not crash the program.
    # when redirected to a file, always write UTF-8 and flush per line: Chinese / German in the log doesn't turn into question marks, and nothing is lost if the program is killed
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
    from backend import i18n
    parser = argparse.ArgumentParser(description=i18n.t("StepCast - 浏览器操作转教学视频"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true", help=i18n.t("不自动打开浏览器"))
    parser.add_argument("--reload", action="store_true", help=i18n.t("开发模式：改代码自动重启"))
    args = parser.parse_args()

    port = args.port or int(config.get("server_port", 8756))
    url = f"http://{args.host}:{port}/"

    # already running (e.g. the exe was double-clicked again): just open the editor, don't start a second instance
    if _already_running(args.host, port):
        print(i18n.t("StepCast 已经在运行：{url}", url=url))
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    config.ensure_dirs()

    try:
        import uvicorn
    except ImportError:
        print(i18n.t("缺少依赖，请先执行：pip install -r requirements.txt"))
        return 1

    from backend.services import ffmpeg_util
    ff = ffmpeg_util.available()

    print("=" * 62)
    print("  StepCast  —  " + i18n.t("浏览器操作 → 带解说的教学视频"))
    print("=" * 62)
    print(f"  {i18n.t('编辑器')}: {url}")
    print(f"  {i18n.t('项目目录')}: {config.DATA_DIR}")
    print(f"  ffmpeg: {'✓ ' + ff.get('version', '')[:40] if ff['ok'] else i18n.t('✗ 未找到（无法导出视频）')}")
    from backend.services import llm
    ai = llm.public_state()
    print(f"  {i18n.t('AI 模型')}: {ai['name']} / {ai['model'] or i18n.t('未选模型')}  "
          f"{i18n.t('✓ 已配置') if ai['configured'] else i18n.t('✗ 未配置（界面右上角设置里填）')}")
    print("-" * 62)
    print("  " + i18n.t("Chrome 扩展：打开 chrome://extensions → 开发者模式 → 加载已解压的扩展程序"))
    print("  " + i18n.t("选择 {path}", path=config.INSTALL_DIR / "extension"))
    print("  " + i18n.t("按 Ctrl+C 停止服务"))
    print("=" * 62)

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    _quiet_connection_resets()
    uvicorn.run(
        "backend.main:app" if args.reload else _get_app(),
        host=args.host, port=port, reload=args.reload, log_level="warning",
    )
    return 0


def _get_app():
    from backend.main import app
    return app


def _already_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if config.FROZEN and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("StepCast")
        except Exception:
            pass
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    # a window opened by double-clicking the exe would vanish instantly on errors; pause so the error can be read
    if code and config.FROZEN:
        try:
            print()
            input("Press Enter to exit / 按回车键退出 …")  # i18n: ignore
        except Exception:
            pass
    sys.exit(code)
