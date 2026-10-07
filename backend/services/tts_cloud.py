"""Paid voice services: Doubao Speech (Volcengine), MiniMax Speech, Qwen-TTS (Alibaba Cloud Model Studio),
Google Gemini TTS, ElevenLabs and Azure AI Speech (the last three are available in Europe, with EU regions / data residency).

Voice IDs carry a service prefix, e.g. doubao:zh_female_vv_uranus_bigtts, minimax:English_Graceful_Lady, qwen:Cherry,
gemini:Kore, elevenlabs:21m00Tcm4TlvDq8ikWAM, azure:en-US-AvaMultilingualNeural;
IDs without a prefix are the default free Edge voices. tts.synth dispatches by prefix, so the project voice and the two Q&A speakers can each use a different service.

- Long text is split into sentence chunks that are synthesised separately and joined (each service limits the length per call; each chunk's duration also serves as timing for subtitles and slide reveals)
- Speed and volume: passed to the API where supported (Doubao, MiniMax, Azure); otherwise adjusted with ffmpeg after synthesis
- Errors are reported directly (unlike Edge, there is no fallback to Windows offline voices: silently replacing a paid voice with a robotic one would be wrong)
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import requests

from .. import config, i18n
from ..i18n import N_
from . import ffmpeg_util

GAP = 0.12                   # pause between the chunks of a paragraph synthesised in several parts (seconds)


class CloudTTSError(RuntimeError):
    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry            # rate limits, temporary server errors: worth retrying; wrong key, no balance, bad parameters: retrying won't help


@dataclass(frozen=True)
class Field:
    """An extra setting of a service, shown under its key in the settings page: a choice (when `choices` is given) or free text."""
    key: str
    label: str
    default: str = ""
    choices: Tuple[Tuple[str, str], ...] = ()       # (value, label)


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    hint: str
    key_url: str
    env: Tuple[str, ...]
    llm: str = ""                     # which AI-model provider shares the same key (borrowed when this one is empty)
    max_chars: int = 150              # maximum characters per chunk (split by sentences, each chunk synthesised separately)
    model: str = ""
    fields: Tuple[Field, ...] = ()


SERVICES: Dict[str, Service] = {s.id: s for s in (
    Service("doubao", N_("豆包语音（火山引擎）"),
            N_("字节跳动，中文最自然的一档。在火山引擎「豆包语音」控制台开通语音合成大模型 2.0，"
               "然后在「API Key 管理」里创建 Key（新版控制台）。"),
            "https://console.volcengine.com/speech/new/setting/apikeys", ("MODEL_SPEECH_API_KEY", "VOLC_SPEECH_API_KEY")),
    Service("minimax", N_("MiniMax 语音"),
            N_("中英文都很自然，音色多。Key 和「AI 模型」里的 MiniMax 是同一个，那边填过这里可以不填。"),
            "https://platform.minimaxi.com/", ("MINIMAX_API_KEY",), llm="minimax", max_chars=400,
            model="speech-2.8-hd"),
    Service("qwen", N_("通义语音（阿里云百炼）"),
            N_("阿里云 Qwen-TTS，一个音色能说中文、英文等十种语言。Key 和「AI 模型」里的通义千问是同一个，那边填过这里可以不填。"),
            "https://bailian.console.aliyun.com/", ("DASHSCOPE_API_KEY",), llm="qwen", max_chars=150,
            model="qwen3-tts-flash"),
    Service("gemini", N_("Google Gemini 语音"),
            N_("Google Gemini TTS：一个音色能说一百多种语言，还可以用文字描述语气。Key 和「AI 模型」里的 Google Gemini 是同一个，"
               "那边填过这里可以不填。欧洲使用需要付费档（在 Google AI Studio 里绑定结算账号）。"),
            "https://aistudio.google.com/apikey", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), llm="gemini", max_chars=500,
            model="gemini-3.8-flash-tts",
            fields=(Field("model", N_("模型"), "gemini-3.8-flash-tts",
                          (("gemini-3.8-flash-tts", N_("Gemini 3.8 Flash TTS（效果最好）")),
                           ("gemini-3.8-flash-lite-tts", N_("Gemini 3.8 Flash-Lite TTS（更便宜）")))),
                    Field("style", N_("语气提示（可选，比如：平静、清晰的讲解语气）"), ""))),
    Service("elevenlabs", N_("ElevenLabs"),
            N_("公认最自然、最有表现力的语音，音色库很大，英语和欧洲语言尤其好。到网站的 API Keys 页面创建 Key；"
               "免费档每月 1 万字符。需要数据留在欧盟时，在下面的「数据区域」里选欧盟（要企业档）。"),
            "https://elevenlabs.io/app/settings/api-keys", ("ELEVENLABS_API_KEY", "XI_API_KEY"), max_chars=500,
            model="eleven_multilingual_v2",
            fields=(Field("model", N_("模型"), "eleven_multilingual_v2",
                          (("eleven_multilingual_v2", N_("Multilingual v2（稳定，推荐）")),
                           ("eleven_v3", N_("v3（最有表现力）")),
                           ("eleven_flash_v2_5", N_("Flash v2.5（便宜、快）")))),
                    Field("region", N_("数据区域"), "",
                          (("", N_("默认")), ("eu", N_("欧盟（数据驻留）")), ("us", N_("美国")))))),
    Service("azure", N_("微软 Azure 语音"),
            N_("和默认的 Edge 免费语音是同一套声音，另有更自然的 HD 语音，有欧洲区域，每月有免费额度。"
               "在 Azure 门户创建「语音服务」资源，复制它的 Key，并在下面填资源所在的区域（欧洲常用 westeurope、northeurope）。"),
            "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices",
            ("AZURE_SPEECH_KEY", "SPEECH_KEY"), max_chars=300,
            fields=(Field("region", N_("区域"), "westeurope"),)),
)}

# ---- voices -----------------------------------------------------------------------

MULTI = ["zh", "en", "fr", "de", "ru", "it", "es", "pt", "ja", "ko"]

# Common official voices of Doubao Speech Synthesis 2.0 (hundreds more in the Volcengine docs; your own cloned voice IDs can be added in the settings)
DOUBAO_VOICES = [  # i18n: ignore
    ("zh_female_vv_uranus_bigtts", "Vivi", "Female", "zh-CN"),
    ("zh_female_xiaohe_uranus_bigtts", "小何", "Female", "zh-CN"),  # i18n: ignore
    ("zh_female_cancan_uranus_bigtts", "知性灿灿", "Female", "zh-CN"),  # i18n: ignore
    ("zh_female_shuangkuaisisi_uranus_bigtts", "爽快思思", "Female", "zh-CN"),  # i18n: ignore
    ("zh_female_qingxinnvsheng_uranus_bigtts", "清新女声", "Female", "zh-CN"),  # i18n: ignore
    ("zh_male_m191_uranus_bigtts", "云舟", "Male", "zh-CN"),  # i18n: ignore
    ("zh_male_liufei_uranus_bigtts", "刘飞", "Male", "zh-CN"),  # i18n: ignore
    ("zh_male_taocheng_uranus_bigtts", "小天", "Male", "zh-CN"),  # i18n: ignore
    ("en_female_dacey_uranus_bigtts", "Dacey", "Female", "en-US"),
    ("en_male_tim_uranus_bigtts", "Tim", "Male", "en-US"),
]
# Qwen-TTS: a few voices suited to narration (each speaks ten languages; dialect, child and character voices left out)
QWEN_VOICES = [
    ("Cherry", "Cherry", "Female"), ("Serena", "Serena", "Female"), ("Maia", "Maia", "Female"),
    ("Jennifer", "Jennifer", "Female"), ("Katerina", "Katerina", "Female"),
    ("Ethan", "Ethan", "Male"), ("Neil", "Neil", "Male"), ("Andre", "Andre", "Male"),
    ("Kai", "Kai", "Male"), ("Aiden", "Aiden", "Male"), ("Ryan", "Ryan", "Male"),
]
# MiniMax: with a key, the full list of system voices is fetched from the API; otherwise these are used
MINIMAX_FALLBACK = [
    ("Chinese (Mandarin)_Reliable_Executive", "Reliable Executive", "Male", "zh-CN"),
    ("Chinese (Mandarin)_Lyrical_Voice", "Lyrical Voice", "Female", "zh-CN"),
    ("presenter_male", "Presenter (male)", "Male", "zh-CN"),
    ("presenter_female", "Presenter (female)", "Female", "zh-CN"),
    ("English_Graceful_Lady", "Graceful Lady", "Female", "en-US"),
    ("English_Insightful_Speaker", "Insightful Speaker", "Male", "en-US"),
]
_MINIMAX_LANG = {"chinese (mandarin)": "zh-CN", "cantonese": "zh-HK", "english": "en-US", "japanese": "ja-JP",
                 "korean": "ko-KR", "spanish": "es-ES", "portuguese": "pt-BR", "french": "fr-FR", "german": "de-DE",
                 "italian": "it-IT", "russian": "ru-RU", "arabic": "ar-SA", "dutch": "nl-NL", "polish": "pl-PL",
                 "vietnamese": "vi-VN", "thai": "th-TH", "indonesian": "id-ID", "turkish": "tr-TR", "hindi": "hi-IN",
                 "ukrainian": "uk-UA", "romanian": "ro-RO", "greek": "el-GR", "czech": "cs-CZ", "finnish": "fi-FI",
                 "bulgarian": "bg-BG", "danish": "da-DK", "slovak": "sk-SK", "swedish": "sv-SE", "croatian": "hr-HR",
                 "hungarian": "hu-HU", "norwegian": "nb-NO", "slovenian": "sl-SI", "catalan": "ca-ES"}
_voice_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def _entry(svc: str, vid: str, name: str, gender: str, locale: str, langs: Optional[List[str]] = None, note: str = "") -> Dict[str, Any]:
    return {"name": f"{svc}:{vid}", "locale": locale, "gender": gender, "langs": langs or [locale.split("-")[0]],
            "friendly": f"{i18n.t(SERVICES[svc].name)} · {name}" + (f"（{note}）" if note else ""), "short": name, "service": svc}


def _every_language() -> List[str]:
    """Language codes of all narration languages: for voices that speak whatever language the text is in."""
    from .script_gen import LANG_NAMES
    return sorted({code.split("-")[0].lower() for code in LANG_NAMES})


# Gemini TTS: the 30 prebuilt voices (each speaks every supported language; the word is Google's description of its character)
GEMINI_VOICES = [
    ("Zephyr", "Bright", "Female"), ("Puck", "Upbeat", "Male"), ("Charon", "Informative", "Male"), ("Kore", "Firm", "Female"),
    ("Fenrir", "Excitable", "Male"), ("Leda", "Youthful", "Female"), ("Orus", "Firm", "Male"), ("Aoede", "Breezy", "Female"),
    ("Callirrhoe", "Easy-going", "Female"), ("Autonoe", "Bright", "Female"), ("Enceladus", "Breathy", "Male"),
    ("Iapetus", "Clear", "Male"), ("Umbriel", "Easy-going", "Male"), ("Algieba", "Smooth", "Male"), ("Despina", "Smooth", "Female"),
    ("Erinome", "Clear", "Female"), ("Algenib", "Gravelly", "Male"), ("Rasalgethi", "Informative", "Male"),
    ("Laomedeia", "Upbeat", "Female"), ("Achernar", "Soft", "Female"), ("Alnilam", "Firm", "Male"), ("Schedar", "Even", "Male"),
    ("Gacrux", "Mature", "Female"), ("Pulcherrima", "Forward", "Female"), ("Achird", "Friendly", "Male"),
    ("Zubenelgenubi", "Casual", "Male"), ("Vindemiatrix", "Gentle", "Female"), ("Sadachbia", "Lively", "Male"),
    ("Sadaltager", "Knowledgeable", "Male"), ("Sulafat", "Warm", "Female"),
]
# ElevenLabs: with a key, the voices of the account (the default ones and the ones you added) are fetched; otherwise a few of the default ones
ELEVEN_FALLBACK = [
    ("21m00Tcm4TlvDq8ikWAM", "Rachel", "Female"), ("EXAVITQu4vr4xnSDxMaL", "Sarah", "Female"), ("Xb7hH8MSUJpSbSDYk0k2", "Alice", "Female"),
    ("pNInz6obpgDQGcFmaJgB", "Adam", "Male"), ("JBFqnCBsd6RMkjVDRZzb", "George", "Male"), ("nPczCjzI2devNBz1zQrb", "Brian", "Male"),
]
# Azure: with a key and region, the voices of that region are fetched; otherwise these multilingual ones (each speaks many languages)
AZURE_FALLBACK = [
    ("en-US-AvaMultilingualNeural", "Ava", "Female"), ("en-US-EmmaMultilingualNeural", "Emma", "Female"),
    ("en-US-AndrewMultilingualNeural", "Andrew", "Male"), ("en-US-BrianMultilingualNeural", "Brian", "Male"),
    ("de-DE-SeraphinaMultilingualNeural", "Seraphina", "Female"), ("de-DE-FlorianMultilingualNeural", "Florian", "Male"),
    ("fr-FR-VivienneMultilingualNeural", "Vivienne", "Female"), ("fr-FR-RemyMultilingualNeural", "Remy", "Male"),
]


def _guess_gender(*texts: str) -> str:
    t = " ".join(texts).lower()
    if re.search(r"\b(female|woman|lady|girl|she)\b|女", t):  # i18n: ignore
        return "Female"
    if re.search(r"\b(male|man|boy|gentleman|he)\b|男", t):  # i18n: ignore
        return "Male"
    return ""


def _minimax_voices(key: str) -> List[Dict[str, Any]]:
    try:
        r = requests.post(_minimax_base() + "/get_voice", headers={"Authorization": f"Bearer {key}"},
                          json={"voice_type": "all"}, timeout=8)
        data = r.json() if r.status_code < 400 else {}
    except Exception:
        data = {}
    out = []
    for group in ("system_voice", "voice_cloning", "voice_generation"):
        for v in (data.get(group) or []) if isinstance(data, dict) else []:
            vid = str(v.get("voice_id") or "")
            if not vid:
                continue
            desc = " ".join(v.get("description") or []) if isinstance(v.get("description"), list) else str(v.get("description") or "")
            name = str(v.get("voice_name") or vid)
            prefix = vid.split("_")[0].lower()
            locale = _MINIMAX_LANG.get(prefix, "zh-CN")
            out.append(_entry("minimax", vid, name, _guess_gender(name, desc, vid), locale))
    return out


def _eleven_voices(key: str, base: str) -> List[Dict[str, Any]]:
    try:
        r = requests.get(base + "/v1/voices", headers={"xi-api-key": key}, timeout=8)
        data = r.json() if r.status_code < 400 else {}
    except Exception:
        data = {}
    out = []
    for v in (data.get("voices") or []) if isinstance(data, dict) else []:
        vid = str(v.get("voice_id") or "")
        if not vid:
            continue
        labels = v.get("labels") or {}
        gender = {"female": "Female", "male": "Male"}.get(str(labels.get("gender") or "").lower(), "")
        note = ", ".join(str(labels[k]) for k in ("accent", "description") if labels.get(k))
        out.append(_entry("elevenlabs", vid, str(v.get("name") or vid), gender, "en-US", _every_language(), note))
    return out


def _azure_voices(key: str, region: str) -> List[Dict[str, Any]]:
    try:
        r = requests.get(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                         headers={"Ocp-Apim-Subscription-Key": key}, timeout=10)
        data = r.json() if r.status_code < 400 else []
    except Exception:
        data = []
    out = []
    for v in data if isinstance(data, list) else []:
        sn, loc = str(v.get("ShortName") or ""), str(v.get("Locale") or "")
        vtype = str(v.get("VoiceType") or "Neural")
        if not sn or not loc or "Neural" not in vtype or vtype.startswith("Custom"):
            continue                                  # (standard and custom voices aren't offered)
        langs = [loc.split("-")[0].lower()] + [x.split("-")[0].lower() for x in v.get("SecondaryLocaleList") or []]
        name = str(v.get("LocalName") or v.get("DisplayName") or sn)
        out.append(_entry("azure", sn, name, str(v.get("Gender") or ""), loc, list(dict.fromkeys(langs)),
                          "HD" if "DragonHD" in sn or "HD" in vtype else ""))
    return out


def _cached_voices(cache_key: str, load: Callable[[], List[Dict[str, Any]]], fallback: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Fetched online and cached for an hour; if that fails (offline, wrong key) the built-in few are used and the fetch is retried after 5 minutes."""
    hit = _voice_cache.get(cache_key)
    if hit is None or time.time() > hit[0]:
        got = load()
        hit = (time.time() + (3600 if got else 300), got or fallback())
        _voice_cache[cache_key] = hit
    return hit[1]


def voices(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Voices of the services that have a key (others aren't listed). MiniMax's full list is fetched online and cached for an hour."""
    cfg = cfg if cfg is not None else config.load()
    out: List[Dict[str, Any]] = []
    if api_key("doubao", cfg):
        out += [_entry("doubao", v, n, g, loc) for v, n, g, loc in DOUBAO_VOICES]
        for vid in extra_voices("doubao", cfg):
            out.append(_entry("doubao", vid, vid, "", "zh-CN", MULTI))
    key = api_key("minimax", cfg)
    if key:
        hit = _voice_cache.get(key)
        if hit is None or time.time() > hit[0]:
            got = _minimax_voices(key)
            # cached for an hour when fetched; if not (offline, wrong key) use the built-in few and retry after 5 minutes
            hit = (time.time() + (3600 if got else 300),
                   got or [_entry("minimax", v, n, g, loc) for v, n, g, loc in MINIMAX_FALLBACK])
            _voice_cache[key] = hit
        out += hit[1]
    if api_key("qwen", cfg):
        out += [_entry("qwen", v, n, g, "zh-CN", MULTI) for v, n, g in QWEN_VOICES]
    if api_key("gemini", cfg):
        every = _every_language()
        out += [_entry("gemini", v, v, g, "en-US", every, note) for v, note, g in GEMINI_VOICES]
    key = api_key("elevenlabs", cfg)
    if key:
        base = _eleven_base(cfg)
        out += _cached_voices(f"elevenlabs|{key}|{base}", lambda: _eleven_voices(key, base),
                              lambda: [_entry("elevenlabs", v, n, g, "en-US", _every_language()) for v, n, g in ELEVEN_FALLBACK])
        for vid in extra_voices("elevenlabs", cfg):
            out.append(_entry("elevenlabs", vid, vid, "", "en-US", _every_language()))
    key = api_key("azure", cfg)
    if key:
        region = _azure_region(cfg)
        out += _cached_voices(f"azure|{key}|{region}", lambda: _azure_voices(key, region),
                              lambda: [_entry("azure", v, n, g, "-".join(v.split("-")[:2]), MULTI + ["pl", "nl", "sv"])
                                       for v, n, g in AZURE_FALLBACK])
    return out


def is_cloud(voice: str) -> bool:
    return ":" in (voice or "") and voice.split(":", 1)[0] in SERVICES


def voice_info(voice: str) -> Dict[str, Any]:
    """Name, language and gender of a voice (offline: MiniMax uses the cache or guesses from the ID)."""
    svc, _, vid = (voice or "").partition(":")
    for v in [x for _, (_, lst) in _voice_cache.items() for x in lst]:
        if v["name"] == voice:
            return v
    if svc == "doubao":
        for v, n, g, loc in DOUBAO_VOICES:
            if v == vid:
                return _entry(svc, v, n, g, loc)
        return _entry(svc, vid, vid, "", "zh-CN", MULTI)
    if svc == "qwen":
        for v, n, g in QWEN_VOICES:
            if v == vid:
                return _entry(svc, v, n, g, "zh-CN", MULTI)
        return _entry(svc, vid, vid, "", "zh-CN", MULTI)
    if svc == "minimax":
        name = vid.split("_", 1)[-1].replace("_", " ") if "_" in vid else vid
        return _entry(svc, vid, name, _guess_gender(vid), _MINIMAX_LANG.get(vid.split("_")[0].lower(), "zh-CN"))
    if svc == "gemini":
        for v, note, g in GEMINI_VOICES:
            if v == vid:
                return _entry(svc, v, v, g, "en-US", _every_language(), note)
        return _entry(svc, vid, vid, "", "en-US", _every_language())
    if svc == "elevenlabs":
        for v, n, g in ELEVEN_FALLBACK:
            if v == vid:
                return _entry(svc, v, n, g, "en-US", _every_language())
        return _entry(svc, vid, vid, "", "en-US", _every_language())
    if svc == "azure":
        loc = "-".join(vid.split("-")[:2]) or "en-US"
        name = vid.split("-", 2)[-1].replace("MultilingualNeural", "").replace("Neural", "")
        return _entry(svc, vid, name or vid, "", loc, [loc.split("-")[0].lower()] + (MULTI if "Multilingual" in vid else []))
    return {}


# ---- settings -----------------------------------------------------------------------

def _saved(svc: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = (cfg.get("tts_services") or {}).get(svc)
    return v if isinstance(v, dict) else {}


def key_source(svc: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(key, where it comes from: config | env:XXX | llm)."""
    cfg = cfg if cfg is not None else config.load()
    s = SERVICES[svc]
    for e in s.env:
        if os.environ.get(e, "").strip():
            return os.environ[e].strip(), f"env:{e}"
    k = str(_saved(svc, cfg).get("api_key") or "").strip()
    if k:
        return k, "config"
    if s.llm:
        k = str(((cfg.get("llm_providers") or {}).get(s.llm) or {}).get("api_key") or "").strip()
        if k:
            return k, "llm"
    return "", ""


def api_key(svc: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    return key_source(svc, cfg)[0]


def setting(svc: str, key: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """An extra setting of a service (model, region, …): what was saved, else the field's default."""
    cfg = cfg if cfg is not None else config.load()
    v = str(_saved(svc, cfg).get(key) or "").strip()
    if v:
        return v
    return next((f.default for f in SERVICES[svc].fields if f.key == key), "")


def extra_voices(svc: str, cfg: Dict[str, Any]) -> List[str]:
    raw = str(_saved(svc, cfg).get("voices") or "")
    return [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]


def public_state(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """For the settings page: each service's description, masked key and where the key comes from."""
    from .llm import _mask
    cfg = cfg if cfg is not None else config.load()
    out = []
    for s in SERVICES.values():
        key, src = key_source(s.id, cfg)
        out.append({"id": s.id, "name": i18n.t(s.name), "hint": i18n.t(s.hint), "key_url": s.key_url,
                    "key_masked": _mask(key), "key_from": src, "configured": bool(key),
                    "voices": str(_saved(s.id, cfg).get("voices") or ""),
                    "fields": [{"key": f.key, "label": i18n.t(f.label), "value": setting(s.id, f.key, cfg), "default": f.default,
                                "choices": [{"value": v, "label": i18n.t(lab)} for v, lab in f.choices]} for f in s.fields]})
    return out


def merge_settings_patch(patch: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge tts_services submitted by the UI into the saved values: an empty key keeps the existing one."""
    incoming = patch.get("tts_services")
    if incoming is None:
        return patch
    cfg = cfg if cfg is not None else config.load()
    merged = {k: dict(v) for k, v in (cfg.get("tts_services") or {}).items() if k in SERVICES and isinstance(v, dict)}
    if isinstance(incoming, dict):
        for sid, vals in incoming.items():
            if sid not in SERVICES or not isinstance(vals, dict):
                continue
            e = merged.setdefault(sid, {})
            if str(vals.get("api_key") or "").strip():
                e["api_key"] = str(vals["api_key"]).strip()
            if vals.get("clear_key"):
                e.pop("api_key", None)
            if "voices" in vals:
                e["voices"] = str(vals["voices"] or "").strip()
            for f in SERVICES[sid].fields:
                if f.key in vals:
                    v = str(vals[f.key] or "").strip()
                    if f.choices and v not in {c for c, _ in f.choices}:
                        continue                          # only the offered choices are accepted
                    e[f.key] = v
    patch["tts_services"] = merged
    _voice_cache.clear()                    # the key may have changed: fetch MiniMax's voice list again
    return patch


# ---- synthesis -----------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n+")


def split_chunks(text: str, max_chars: int) -> List[str]:
    """Split into chunks of at most max_chars by sentences (an overly long sentence is split at commas, otherwise hard-cut).
    Text in Latin letters takes more characters for the same meaning, so the limit is doubled for it."""
    text = (text or "").strip()
    latin = sum(1 for ch in text if ch.isascii()) > len(text) * 0.8
    max_chars = max_chars * 2 if latin else max_chars
    sents = [x.strip() for x in _SENT_SPLIT.split(text) if x and x.strip()]
    out: List[str] = []
    for s in sents:
        while len(s) > max_chars:
            cut = max(s.rfind(ch, 0, max_chars) for ch in "，,、 ")
            cut = cut if cut >= max_chars // 3 else max_chars - 1
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if not s:
            continue
        if out and len(out[-1]) + len(s) + 1 <= max_chars:
            out[-1] = out[-1] + (" " if latin else "") + s
        else:
            out.append(s)
    return out


def _rate_factor(rate: str) -> float:
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*%\s*$", rate or "")
    return 1.0 + float(m.group(1)) / 100 if m else 1.0


def _check(svc: str, r: requests.Response) -> None:
    name = i18n.t(SERVICES[svc].name)
    if r.status_code in (401, 403):
        raise CloudTTSError(i18n.t("{name} 鉴权失败（{status}），请检查 API Key。", name=name, status=r.status_code)
                            + " " + r.text[:200])
    if r.status_code >= 400:
        raise CloudTTSError(i18n.t("{name} 返回 {status}", name=name, status=r.status_code) + ": " + r.text[:300],
                            retry=r.status_code == 429 or r.status_code >= 500)


def _json(svc: str, r: requests.Response) -> Dict[str, Any]:
    try:
        d = r.json()
    except ValueError:
        d = None
    if not isinstance(d, dict):
        raise CloudTTSError(i18n.t("{name} 返回了看不懂的内容：{body}", name=i18n.t(SERVICES[svc].name), body=r.text[:200]),
                            retry=True)
    return d


def _doubao(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    if vid.startswith("S_"):
        resource = "seed-icl-2.0"            # your own cloned voice
    elif vid.endswith("_uranus_bigtts"):
        resource = "seed-tts-2.0"
    else:
        resource = "seed-tts-1.0"
    headers = {"Content-Type": "application/json", "X-Api-Key": key, "X-Api-Resource-Id": resource,
               "X-Api-Request-Id": str(uuid.uuid4())}
    body = {"user": {"uid": "stepcast"},
            "req_params": {"text": text, "speaker": vid,
                           "audio_params": {"format": "mp3", "bit_rate": 128000,
                                            "speech_rate": max(-50, min(100, int(round((speed - 1) * 100))))}}}
    r = requests.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse", headers=headers,
                      json=body, timeout=90, stream=True)
    _check("doubao", r)
    chunks: List[bytes] = []
    for raw in r.iter_lines():
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")).strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        code = d.get("code", 0)
        if code not in (0, 20000000):
            raise CloudTTSError(i18n.t("{name} 合成失败：{error}", name=i18n.t(SERVICES["doubao"].name),
                                       error=f"{code} {d.get('message', '')}"),
                                retry=str(code).startswith(("5", "429")))
        if d.get("data"):
            chunks.append(base64.b64decode(d["data"]))
    if not chunks:
        raise CloudTTSError(i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES["doubao"].name)))
    out.write_bytes(b"".join(chunks))


def _minimax_base() -> str:
    from .llm import resolve
    try:
        base = resolve("minimax").base_url or ""
    except Exception:
        base = ""
    return (base or "https://api.minimax.cn/v1").rstrip("/")


def _minimax(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    body = {"model": str(_saved("minimax", cfg).get("model") or SERVICES["minimax"].model), "text": text,
            "stream": False, "output_format": "hex", "language_boost": "auto",
            "voice_setting": {"voice_id": vid, "speed": round(max(0.5, min(2.0, speed)), 2), "vol": 1, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1}}
    r = requests.post(_minimax_base() + "/t2a_v2", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=120)
    _check("minimax", r)
    d = _json("minimax", r)
    base = d.get("base_resp") or {}
    if base.get("status_code", 0) != 0:
        raise CloudTTSError(i18n.t("{name} 合成失败：{error}", name=i18n.t(SERVICES["minimax"].name),
                                   error=f"{base.get('status_code')} {base.get('status_msg', '')}"))
    audio = (d.get("data") or {}).get("audio") or ""
    if not audio:
        raise CloudTTSError(i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES["minimax"].name)))
    out.write_bytes(bytes.fromhex(audio))


def _qwen(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    body = {"model": str(_saved("qwen", cfg).get("model") or SERVICES["qwen"].model),
            "input": {"text": text, "voice": vid}}
    r = requests.post("https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=120)
    _check("qwen", r)
    d = _json("qwen", r)
    if d.get("code"):
        raise CloudTTSError(i18n.t("{name} 合成失败：{error}", name=i18n.t(SERVICES["qwen"].name),
                                   error=f"{d.get('code')} {d.get('message', '')}"))
    audio = ((d.get("output") or {}).get("audio") or {})
    if audio.get("data"):
        out.write_bytes(base64.b64decode(audio["data"]))
        return
    url = audio.get("url")
    if not url:
        raise CloudTTSError(i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES["qwen"].name)))
    a = requests.get(url, timeout=120)
    _check("qwen", a)
    out.write_bytes(a.content)


def _gemini(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    name = i18n.t(SERVICES["gemini"].name)
    model = setting("gemini", "model", cfg)
    style = setting("gemini", "style", cfg)
    m = re.search(r"gemini-(\d+)\.(\d+)", model)
    if m and (int(m.group(1)), int(m.group(2))) < (3, 8):
        # models before 3.8 take the voice differently and have no separate style field: the style goes in front of the text
        body = {"contents": [{"parts": [{"text": f"{style}: {text}" if style else text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": vid}}}}}
    else:
        part: Dict[str, Any] = {"text": text}
        if style:
            part["speech_metadata"] = {"style": style}
        body = {"contents": [{"role": "user", "parts": [part]}],
                "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"voice": vid}}}}
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='.-_')}:generateContent",
                      headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=body, timeout=120)
    _check("gemini", r)
    d = _json("gemini", r)
    cand = (d.get("candidates") or [{}])[0] if isinstance(d.get("candidates"), list) else {}
    parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand, dict) else []
    inline = next((p.get("inlineData") or p.get("inline_data") for p in parts if isinstance(p, dict)
                   and (p.get("inlineData") or p.get("inline_data"))), None)
    if not inline or not inline.get("data"):
        # the model sometimes answers with text instead of speech: worth another try
        reason = str(cand.get("finishReason") or (d.get("promptFeedback") or {}).get("blockReason") or "")
        raise CloudTTSError(i18n.t("{name} 没有返回音频", name=name) + (f"（{reason}）" if reason else ""), retry=True)
    raw = base64.b64decode(inline["data"])
    if raw[:4] == b"RIFF":
        out.write_bytes(raw)                           # a complete WAV file
        return
    rate = re.search(r"rate=(\d+)", str(inline.get("mimeType") or inline.get("mime_type") or ""))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:                    # raw 16-bit mono PCM: give it a WAV header
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate.group(1)) if rate else 24000)
        w.writeframes(raw)
    out.write_bytes(buf.getvalue())


_ELEVEN_BASE = {"": "https://api.elevenlabs.io", "eu": "https://api.eu.residency.elevenlabs.io", "us": "https://api.us.elevenlabs.io"}


def _eleven_base(cfg: Optional[Dict[str, Any]] = None) -> str:
    return _ELEVEN_BASE.get(setting("elevenlabs", "region", cfg), _ELEVEN_BASE[""])


def _elevenlabs(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    r = requests.post(f"{_eleven_base(cfg)}/v1/text-to-speech/{quote(vid, safe='')}", params={"output_format": "mp3_44100_128"},
                      headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                      json={"text": text, "model_id": setting("elevenlabs", "model", cfg)}, timeout=120)
    _check("elevenlabs", r)
    if not r.content or "json" in str((getattr(r, "headers", None) or {}).get("content-type", "")):
        raise CloudTTSError(i18n.t("{name} 返回了看不懂的内容：{body}", name=i18n.t(SERVICES["elevenlabs"].name),
                                   body=(r.text or "")[:200]))
    out.write_bytes(r.content)


_AZURE_REGION = re.compile(r"^[a-z0-9]{3,30}$")


def _azure_region(cfg: Optional[Dict[str, Any]] = None) -> str:
    """The region of the Speech resource (it becomes part of the address, so only letters and digits are accepted)."""
    region = setting("azure", "region", cfg).lower().replace(" ", "")
    return region if _AZURE_REGION.match(region) else "westeurope"


def _azure(text: str, vid: str, key: str, out: Path, speed: float, cfg: Dict[str, Any]) -> None:
    lang = "-".join(vid.split("-")[:2]) or "en-US"
    inner = xml_escape(text)
    if abs(speed - 1) > 0.01:
        inner = f'<prosody rate="{(speed - 1) * 100:+.0f}%">{inner}</prosody>'
    ssml = (f"<speak version='1.0' xml:lang='{xml_escape(lang)}'><voice name='{xml_escape(vid, {chr(39): '&apos;'})}'>"
            f"{inner}</voice></speak>")
    r = requests.post(f"https://{_azure_region(cfg)}.tts.speech.microsoft.com/cognitiveservices/v1",
                      headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                               "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3", "User-Agent": "StepCast"},
                      data=ssml.encode("utf-8"), timeout=120)
    if r.status_code in (401, 403):
        raise CloudTTSError(i18n.t("{name} 鉴权失败（{status}），请检查 API Key 和区域（要和 Azure 资源所在的区域一致）。",
                                   name=i18n.t(SERVICES["azure"].name), status=r.status_code))
    _check("azure", r)
    if not r.content:
        raise CloudTTSError(i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES["azure"].name)))
    out.write_bytes(r.content)


_ENGINES: Dict[str, Callable[..., None]] = {"doubao": _doubao, "minimax": _minimax, "qwen": _qwen, "gemini": _gemini,
                                            "elevenlabs": _elevenlabs, "azure": _azure}
NATIVE_SPEED = {"doubao", "minimax", "azure"}      # the API adjusts speed itself; the others are adjusted with ffmpeg after synthesis


def synth(text: str, voice: str, out_path: Path, rate: str = "", volume: str = "") -> Tuple[float, List[Dict[str, Any]]]:
    """Synthesise a paragraph with a paid service and save it as mp3. Returns (duration, time of each chunk). Chunk times serve as word boundaries, so subtitles and slide reveals stay in sync."""
    cfg = config.load()
    svc, _, vid = voice.partition(":")
    if svc not in SERVICES:
        raise CloudTTSError(i18n.t("不认识的配音服务：{name}", name=svc))
    key = api_key(svc, cfg)
    if not key:
        raise CloudTTSError(i18n.t("没有配置 {name} 的 API Key（设置 → 配音）。", name=i18n.t(SERVICES[svc].name)))
    rate = rate or cfg.get("tts_rate", "+0%")
    volume = volume or cfg.get("tts_volume", "+0%")
    speed = _rate_factor(rate)
    parts_text = split_chunks(text, SERVICES[svc].max_chars)
    if not parts_text:
        raise CloudTTSError(i18n.t("解说文本为空。"))
    tmp = out_path.parent / f"{out_path.stem}.cloud_{uuid.uuid4().hex[:6]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        files: List[Path] = []
        for k, piece in enumerate(parts_text):
            f = tmp / f"{k:03d}.bin"
            last: Optional[Exception] = None
            for attempt in range(3):                 # network hiccups, rate limits, temporary server errors: retry twice
                try:
                    _ENGINES[svc](piece, vid, key, f, speed if svc in NATIVE_SPEED else 1.0, cfg)
                    last = None
                    break
                except CloudTTSError as e:
                    last = e
                except requests.RequestException as e:
                    last = CloudTTSError(i18n.t("{name} 连接失败：{error}", name=i18n.t(SERVICES[svc].name), error=e),
                                         retry=True)
                except (ValueError, KeyError, TypeError) as e:      # the returned data has an unexpected format
                    last = CloudTTSError(i18n.t("{name} 返回了看不懂的内容：{body}",
                                                name=i18n.t(SERVICES[svc].name), body=e))
                if not last.retry or attempt == 2:
                    break
                time.sleep(1.5 * (attempt + 1))
            if last is not None:
                raise last
            if not f.exists() or f.stat().st_size < 256:
                raise CloudTTSError(i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES[svc].name)))
            files.append(f)
        filt = []
        if svc not in NATIVE_SPEED and abs(speed - 1) > 0.01:
            filt.append(f"atempo={max(0.5, min(2.0, speed)):.3f}")
        vol = _rate_factor(volume)
        if abs(vol - 1) > 0.01:
            filt.append(f"volume={vol:.3f}")
        args: List[str] = []
        chains = []
        for k, f in enumerate(files):
            args += ["-i", str(f)]
            pad = f",apad=pad_dur={GAP}" if k < len(files) - 1 else ""
            chains.append(f"[{k}:a]aformat=sample_rates=24000:channel_layouts=mono{pad}[a{k}]")
        graph = ";".join(chains) + ";" + "".join(f"[a{k}]" for k in range(len(files))) \
            + f"concat=n={len(files)}:v=0:a=1" + ("," + ",".join(filt) if filt else "") + "[out]"
        part = tmp / "all.mp3"
        ffmpeg_util.run(args + ["-filter_complex", graph, "-map", "[out]", "-codec:a", "libmp3lame",
                                "-b:a", "128k", str(part)])
        os.replace(part, out_path)
        # duration of each chunk (scaled for speed changes), used as word boundaries
        bounds: List[Dict[str, Any]] = []
        t = 0.0
        scale = 1 / max(0.5, min(2.0, speed)) if svc not in NATIVE_SPEED and abs(speed - 1) > 0.01 else 1.0
        for f, piece in zip(files, parts_text):
            d = ffmpeg_util.probe_duration(f) * scale
            bounds.append({"t": round(t, 3), "d": round(d, 3), "text": piece})
            t += d + GAP * scale
        dur = ffmpeg_util.probe_duration(out_path) or t
        return dur, bounds
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test(svc: str, api_key_override: str = "") -> Dict[str, Any]:
    """Test whether a service works: synthesise a short sentence with its first voice."""
    cfg = dict(config.load())
    if api_key_override.strip():
        cfg["tts_services"] = {**(cfg.get("tts_services") or {}),
                               svc: {**_saved(svc, cfg), "api_key": api_key_override.strip()}}
    if svc not in SERVICES:
        return {"ok": False, "message": i18n.t("不认识的配音服务：{name}", name=svc)}
    key = api_key(svc, cfg)
    if not key:
        return {"ok": False, "message": i18n.t("没有配置 {name} 的 API Key（设置 → 配音）。", name=i18n.t(SERVICES[svc].name))}
    first = lambda lst: lst[0][0]                                                      # noqa: E731
    vid = {"doubao": first(DOUBAO_VOICES), "minimax": first(MINIMAX_FALLBACK), "qwen": first(QWEN_VOICES),
           "gemini": first(GEMINI_VOICES), "elevenlabs": first(ELEVEN_FALLBACK), "azure": first(AZURE_FALLBACK)}[svc]
    text = "你好，这是一段测试。" if svc in ("doubao", "minimax", "qwen") else "Hello, this is a short test."  # i18n: ignore
    tmp = config.DATA_DIR / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"ttstest_{uuid.uuid4().hex[:6]}.bin"
    try:
        _ENGINES[svc](text, vid, key, f, 1.0, cfg)
        dur = ffmpeg_util.probe_duration(f)
        if dur <= 0:
            return {"ok": False, "message": i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES[svc].name))}
        return {"ok": True, "message": i18n.t("连接成功，合成了 {sec} 秒测试音频", sec=f"{dur:.1f}")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300]}
    finally:
        f.unlink(missing_ok=True)
