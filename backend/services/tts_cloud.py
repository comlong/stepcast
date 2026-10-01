"""付费的商用配音服务：豆包语音（火山引擎）、MiniMax 语音、通义语音（阿里云百炼 Qwen-TTS）。

音色 ID 带服务前缀，比如 doubao:zh_female_vv_uranus_bigtts、minimax:English_Graceful_Lady、qwen:Cherry；
没有前缀的是默认的 Edge 免费音色。tts.synth 按前缀分过来，所以项目音色、两人问答的两位讲者可以各用各的服务。

- 长文字按句子切成小段分别合成再拼起来（各家单次字数有上限；每段的时长也正好当作字幕和逐条出现的时间）
- 语速、音量：接口支持的直接传，不支持的（通义）合成后用 ffmpeg 调
- 出错直接报出来（不像 Edge 那样退回 Windows 离线语音：花钱买的声音悄悄换成机器音不合适）
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .. import config, i18n
from ..i18n import N_
from . import ffmpeg_util

GAP = 0.12                   # 一段话切成几段合成时，段与段之间的停顿（秒）


class CloudTTSError(RuntimeError):
    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry            # 限流、服务端临时出错：值得再试；Key 不对、余额不足、参数不对：再试也没用


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    hint: str
    key_url: str
    env: Tuple[str, ...]
    llm: str = ""                     # 和「AI 模型」里哪一家是同一个 Key（留空时借用）
    max_chars: int = 150              # 每段最多多少字（按句子切，切出来的每段单独合成）
    model: str = ""


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
)}

# ---- 音色 -----------------------------------------------------------------------

MULTI = ["zh", "en", "fr", "de", "ru", "it", "es", "pt", "ja", "ko"]

# 豆包语音合成大模型 2.0 的常用官方音色（完整的几百个见火山引擎文档；自己复刻的音色 ID 可以在设置里加）
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
# 通义 Qwen-TTS：挑了适合讲解的几个（每个都能说十种语言；方言、童声、角色音没放进来）
QWEN_VOICES = [
    ("Cherry", "Cherry", "Female"), ("Serena", "Serena", "Female"), ("Maia", "Maia", "Female"),
    ("Jennifer", "Jennifer", "Female"), ("Katerina", "Katerina", "Female"),
    ("Ethan", "Ethan", "Male"), ("Neil", "Neil", "Male"), ("Andre", "Andre", "Male"),
    ("Kai", "Kai", "Male"), ("Aiden", "Aiden", "Male"), ("Ryan", "Ryan", "Male"),
]
# MiniMax：有 Key 时从接口取完整的系统音色；取不到时用这几个
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


def _entry(svc: str, vid: str, name: str, gender: str, locale: str, langs: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"name": f"{svc}:{vid}", "locale": locale, "gender": gender, "langs": langs or [locale.split("-")[0]],
            "friendly": f"{i18n.t(SERVICES[svc].name)} · {name}", "short": name, "service": svc}


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


def voices(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """已经配好 Key 的服务的音色（没配的不列）。MiniMax 的完整音色要联网取，缓存一小时。"""
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
            # 取到了缓存一小时；没取到（断网、Key 不对）先用内置的几个，5 分钟后再试
            hit = (time.time() + (3600 if got else 300),
                   got or [_entry("minimax", v, n, g, loc) for v, n, g, loc in MINIMAX_FALLBACK])
            _voice_cache[key] = hit
        out += hit[1]
    if api_key("qwen", cfg):
        out += [_entry("qwen", v, n, g, "zh-CN", MULTI) for v, n, g in QWEN_VOICES]
    return out


def is_cloud(voice: str) -> bool:
    return ":" in (voice or "") and voice.split(":", 1)[0] in SERVICES


def voice_info(voice: str) -> Dict[str, Any]:
    """一个音色的名字、语言、性别（不联网：MiniMax 用缓存或从 ID 猜）。"""
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
    return {}


# ---- 配置 -----------------------------------------------------------------------

def _saved(svc: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = (cfg.get("tts_services") or {}).get(svc)
    return v if isinstance(v, dict) else {}


def key_source(svc: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(Key, 从哪来：config | env:XXX | llm)。"""
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


def extra_voices(svc: str, cfg: Dict[str, Any]) -> List[str]:
    raw = str(_saved(svc, cfg).get("voices") or "")
    return [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]


def public_state(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """设置页用：每个服务的说明、Key 掩码、Key 从哪来。"""
    from .llm import _mask
    cfg = cfg if cfg is not None else config.load()
    out = []
    for s in SERVICES.values():
        key, src = key_source(s.id, cfg)
        out.append({"id": s.id, "name": i18n.t(s.name), "hint": i18n.t(s.hint), "key_url": s.key_url,
                    "key_masked": _mask(key), "key_from": src, "configured": bool(key),
                    "voices": str(_saved(s.id, cfg).get("voices") or "")})
    return out


def merge_settings_patch(patch: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """界面提交的 tts_services 合并进已保存的值：没填 Key 就保留原来的。"""
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
    patch["tts_services"] = merged
    _voice_cache.clear()                    # Key 可能换了：MiniMax 的音色表重新取
    return patch


# ---- 合成 -----------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n+")


def split_chunks(text: str, max_chars: int) -> List[str]:
    """按句子切成每段不超过 max_chars 的几段（一句本身太长就在逗号处断，再不行硬切）。
    西文字母占的多（同样的意思字数多），上限放宽一倍。"""
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
        resource = "seed-icl-2.0"            # 自己复刻的声音
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


_ENGINES: Dict[str, Callable[..., None]] = {"doubao": _doubao, "minimax": _minimax, "qwen": _qwen}
NATIVE_SPEED = {"doubao", "minimax"}      # 接口自己能调语速；其余合成后用 ffmpeg 调


def synth(text: str, voice: str, out_path: Path, rate: str = "", volume: str = "") -> Tuple[float, List[Dict[str, Any]]]:
    """用商用服务合成一段话，存成 mp3。返回 (时长, 每段的时间)。每段的时间当作词边界用，字幕、逐条出现照样对得上。"""
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
            for attempt in range(3):                 # 网络抖动、限流、服务端临时出错：重试两次
                try:
                    _ENGINES[svc](piece, vid, key, f, speed if svc in NATIVE_SPEED else 1.0, cfg)
                    last = None
                    break
                except CloudTTSError as e:
                    last = e
                except requests.RequestException as e:
                    last = CloudTTSError(i18n.t("{name} 连接失败：{error}", name=i18n.t(SERVICES[svc].name), error=e),
                                         retry=True)
                except (ValueError, KeyError, TypeError) as e:      # 返回的数据格式不对
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
        # 每段的时长（调过语速的按比例换算），当作词边界
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
    """测试一个服务能不能用：用它的第一个音色合成一小句。"""
    cfg = dict(config.load())
    if api_key_override.strip():
        cfg["tts_services"] = {**(cfg.get("tts_services") or {}),
                               svc: {**_saved(svc, cfg), "api_key": api_key_override.strip()}}
    if svc not in SERVICES:
        return {"ok": False, "message": i18n.t("不认识的配音服务：{name}", name=svc)}
    key = api_key(svc, cfg)
    if not key:
        return {"ok": False, "message": i18n.t("没有配置 {name} 的 API Key（设置 → 配音）。", name=i18n.t(SERVICES[svc].name))}
    vid = {"doubao": DOUBAO_VOICES[0][0], "minimax": MINIMAX_FALLBACK[0][0], "qwen": QWEN_VOICES[0][0]}[svc]
    tmp = config.DATA_DIR / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"ttstest_{uuid.uuid4().hex[:6]}.bin"
    try:
        _ENGINES[svc]("你好，这是一段测试。", vid, key, f, 1.0, cfg)  # i18n: ignore
        dur = ffmpeg_util.probe_duration(f)
        if dur <= 0:
            return {"ok": False, "message": i18n.t("{name} 没有返回音频", name=i18n.t(SERVICES[svc].name))}
        return {"ok": True, "message": i18n.t("连接成功，合成了 {sec} 秒测试音频", sec=f"{dur:.1f}")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300]}
    finally:
        f.unlink(missing_ok=True)
