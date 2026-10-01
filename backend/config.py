"""Global settings: read from config.json, overridable by environment variables."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

# When packaged as an exe (PyInstaller): code and static files are in _internal next to the exe;
# settings, projects and uploads live in the exe's folder — copying that folder copies the complete software and data.
FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR   # the extension folder is here


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
    """Folder for settings and projects. If the exe sits somewhere without write access (e.g. C:\\Program Files), use the user folder instead."""
    if not frozen or _writable(install_dir):
        return install_dir
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    # before 1.3 the software was called VideoTutorial: if the old folder has data and the new one doesn't exist yet, keep using the old one so upgrades don't lose projects
    legacy, home = base / "VideoTutorial", base / "StepCast"
    return legacy if legacy.exists() and not home.exists() else home


BASE_DIR = pick_home(INSTALL_DIR, FROZEN)
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = Path(os.environ.get("VT_DATA_DIR", BASE_DIR / "projects"))
STATIC_DIR = RESOURCE_DIR / "static"

DEFAULTS: Dict[str, Any] = {
    # LLM: llm_provider picks the provider, llm_providers[provider] = {api_key, base_url, model}
    "llm_provider": "deepseek",
    "llm_providers": {},
    # paid voice services: tts_services[doubao | minimax | qwen] = {api_key, voices (your own voice IDs), model}
    "tts_services": {},
    # fields from old versions that only supported DeepSeek; still valid
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "ui_language": "en",          # interface (working) language zh/en/de/fr/pl/it/es/nl; English if never set
    "language": "en-US",          # narration language
    "voice": "en-US-AriaNeural",
    "tts_rate": "+0%",
    "tts_volume": "+0%",
    "video_width": 1920,
    "video_height": 1080,
    "video_fps": 30,
    # rendering speed: video_encoder = auto (GPU encoding when available) | cpu | nvenc | qsv | amf
    # render_workers = segments rendered at once, 0 = automatic by CPU count
    "video_encoder": "auto",
    "render_workers": 0,
    "accent_color": "#FF5C39",
    "background_color": "#0E1116",
    "zoom_enabled": True,
    "zoom_factor": 1.35,
    "dim_background": True,
    "burn_subtitles": True,
    "second_sub_space": True,       # move the main subtitle up a little and leave the very bottom for second-language subtitles (external)
    "show_cursor": True,
    "show_step_badge": True,
    "browser_frame": True,
    "min_step_duration": 2.5,
    "step_padding": 0.7,
    "intro_enabled": True,
    "outro_enabled": True,
    "server_port": 8756,
    "font_path": "",
    # speech recognition (local faster-whisper)
    "asr_model": "small",          # tiny | base | small | medium | large-v3
    "asr_device": "auto",          # auto | cpu | cuda
    "hf_endpoint": "",             # HuggingFace mirror, e.g. https://hf-mirror.com
    "asr_remove_fillers": True,    # remove filler words ("um", "uh" …)
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
    """The complete settings (defaults + file + environment variables)."""
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
    """Merge into config.json (only known fields are kept)."""
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


# ---- fonts -------------------------------------------------------------------
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # Microsoft YaHei
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font_path(bold: bool = False) -> str:
    """Find a TTF/TTC font that can display Chinese."""
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
