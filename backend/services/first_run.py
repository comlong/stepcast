"""Settings of a new installation by the assistant skill (`stepcast.ps1 setup`): interface and narration language follow the
computer's display language, and StepCast's own AI is off because the assistant writes the texts.

    python app.py --init-settings zh-CN       (VT_HOME = the work folder; does nothing when config.json already exists)
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .. import config, i18n


def narration_language(locale: str) -> str:
    """Windows display language (zh-CN, de-AT, sr-Latn-RS …) -> a narration language of the table; English when there is none."""
    from .script_gen import LANG_NAMES
    parts = [p for p in str(locale or "").replace("_", "-").split("-") if p]
    if not parts:
        return "en-US"
    base = parts[0].lower()
    region = parts[-1].upper() if len(parts) > 1 else ""
    if f"{base}-{region}" in LANG_NAMES:
        return f"{base}-{region}"
    if base == "zh":           # Taiwan, Hong Kong, Macao and "Hant" write traditional characters
        return "zh-TW" if region in ("TW", "HK", "MO") or "hant" in str(locale).lower() else "zh-CN"
    for code in LANG_NAMES:     # same language, another country: the first one in the table
        if code.split("-")[0].lower() == base:
            return code
    return "en-US"


def defaults(locale: str) -> Dict[str, Any]:
    from .tts import DEFAULT_VOICES
    language = narration_language(locale)
    return {"ai_disabled": True, "ui_language": i18n.normalize(locale) or i18n.FALLBACK,
            "language": language, "voice": DEFAULT_VOICES[language]}


def write_defaults(locale: str) -> Dict[str, Any]:
    """Write the defaults for a new work folder; existing settings are never touched (an update keeps them). Returns what was written."""
    if config.CONFIG_PATH.exists():
        return {}
    d = defaults(locale)
    # only these keys (config.save would write every default): defaults changed by a later version still apply
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    config._cache = None
    return d
