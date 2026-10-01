"""StepCast 启动入口。

用法：
    python app.py                 # 启动服务并打开编辑器
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


def main() -> int:
    # 输出被重定向到文件、或控制台不是 UTF-8 时，打印 ✓ 这类字符不能让程序崩掉。
    # 重定向到文件时统一写 UTF-8、按行刷新：日志里的中文 / 德文不会变成问号，程序被强制结束也不丢日志
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

    # 已经在运行（比如又双击了一次 exe）：直接打开编辑器，不再启动第二份
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
    # 双击 exe 打开的窗口，出错时会一闪而过；停一下让人看清报错
    if code and config.FROZEN:
        try:
            print()
            input("Press Enter to exit / 按回车键退出 …")  # i18n: ignore
        except Exception:
            pass
    sys.exit(code)
