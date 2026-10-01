"""全局配置：从 config.json 读取，支持环境变量覆盖。"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

# 打包成 exe（PyInstaller）后：代码和静态资源在 exe 旁边的 _internal 里，
# 配置、项目、上传的文件放在 exe 所在的文件夹 —— 整个文件夹拷走就是一份完整的软件和数据。
FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR   # extension 文件夹在这里


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".write_test_{uuid.uuid4().hex[:6]}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def pick_home(install_dir: Path, frozen: bool) -> Path:
    """放配置和项目的目录。exe 放在没有写权限的地方（如 C:\\Program Files）时，改存到用户目录。"""
    if not frozen or _writable(install_dir):
        return install_dir
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    # 1.3 之前软件叫 VideoTutorial：旧目录里已经有数据、新目录还没建时接着用旧的，升级不丢项目
    legacy, home = base / "VideoTutorial", base / "StepCast"
    return legacy if legacy.exists() and not home.exists() else home


BASE_DIR = pick_home(INSTALL_DIR, FROZEN)
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = Path(os.environ.get("VT_DATA_DIR", BASE_DIR / "projects"))
STATIC_DIR = RESOURCE_DIR / "static"

DEFAULTS: Dict[str, Any] = {
    # 大模型：llm_provider 选哪家，llm_providers[家] = {api_key, base_url, model}
    "llm_provider": "deepseek",
    "llm_providers": {},
    # 商用配音服务：tts_services[doubao | minimax | qwen] = {api_key, voices（自己的音色 ID）, model}
    "tts_services": {},
    # 老版本只支持 DeepSeek 时的字段，仍然有效
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "ui_language": "en",          # 界面（工作）语言 zh/en/de/fr/pl/it/es/nl；没设置过一律英语
    "language": "en-US",          # 解说语言
    "voice": "en-US-AriaNeural",
    "tts_rate": "+0%",
    "tts_volume": "+0%",
    "video_width": 1920,
    "video_height": 1080,
    "video_fps": 30,
    # 渲染速度：video_encoder = auto(有显卡就用显卡编码) | cpu | nvenc | qsv | amf
    # render_workers = 同时渲染几段，0 = 按 CPU 核数自动
    "video_encoder": "auto",
    "render_workers": 0,
    "accent_color": "#FF5C39",
    "background_color": "#0E1116",
    "zoom_enabled": True,
    "zoom_factor": 1.35,
    "dim_background": True,
    "burn_subtitles": True,
    "second_sub_space": True,       # 主字幕往上挪一点，在画面最底下给第二语言字幕（外挂）留位置
    "show_cursor": True,
    "show_step_badge": True,
    "browser_frame": True,
    "min_step_duration": 2.5,
    "step_padding": 0.7,
    "intro_enabled": True,
    "outro_enabled": True,
    "server_port": 8756,
    "font_path": "",
    # 语音识别（本地 faster-whisper）
    "asr_model": "small",          # tiny | base | small | medium | large-v3
    "asr_device": "auto",          # auto | cpu | cuda
    "hf_endpoint": "",             # HuggingFace 镜像，如 https://hf-mirror.com
    "asr_remove_fillers": True,    # 去掉「嗯/啊/呃」等口头禅
}

_lock = threading.Lock()
_cache: Dict[str, Any] | None = None


def _read_file() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load() -> Dict[str, Any]:
    """返回完整配置（默认值 + 文件 + 环境变量）。"""
    global _cache
    with _lock:
        if _cache is None:
            cfg = dict(DEFAULTS)
            file_cfg = _read_file()
            cfg.update(file_cfg)
            env_key = os.environ.get("DEEPSEEK_API_KEY")
            if env_key:
                cfg["deepseek_api_key"] = env_key
            _cache = cfg
        return dict(_cache)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并写入 config.json（只保留已知字段）。"""
    global _cache
    with _lock:
        cfg = dict(DEFAULTS)
        cfg.update(_read_file())
        for k, v in patch.items():
            if k in DEFAULTS:
                cfg[k] = v
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _cache = None
    return load()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---- 字体 ----------------------------------------------------------------
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font_path(bold: bool = False) -> str:
    """找一个能显示中文的 TTF/TTC 字体。"""
    custom = get("font_path")
    if custom and Path(custom).exists():
        return custom
    order = list(_FONT_CANDIDATES)
    if bold:
        order = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"] + order
    for p in order:
        if Path(p).exists():
            return p
    return ""
