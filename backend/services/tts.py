"""语音合成。

主引擎：edge-tts（微软 Edge 神经网络语音，免费、无需 Key、支持多语言）
兜底：pyttsx3（Windows SAPI5，离线）
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config, i18n
from ..models import Project, Step
from . import ffmpeg_util

_voices_cache: Dict[str, Any] = {"ts": 0.0, "data": []}

# 每种语言的推荐声音（界面默认值）
DEFAULT_VOICES = {
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh-TW": "zh-TW-HsiaoChenNeural",
    "en-US": "en-US-AriaNeural",
    "en-GB": "en-GB-SoniaNeural",
    "ja-JP": "ja-JP-NanamiNeural",
    "ko-KR": "ko-KR-SunHiNeural",
    "fr-FR": "fr-FR-DeniseNeural",
    "de-DE": "de-DE-KatjaNeural",
    "pl-PL": "pl-PL-ZofiaNeural",
    "nl-NL": "nl-NL-ColetteNeural",
    "es-ES": "es-ES-ElviraNeural",
    "pt-BR": "pt-BR-FranciscaNeural",
    "ru-RU": "ru-RU-SvetlanaNeural",
    "it-IT": "it-IT-ElsaNeural",
    "th-TH": "th-TH-PremwadeeNeural",
    "vi-VN": "vi-VN-HoaiMyNeural",
    "ar-SA": "ar-SA-ZariyahNeural",
    "hi-IN": "hi-IN-SwaraNeural",
    "pt-PT": "pt-PT-RaquelNeural",
    "bs-BA": "bs-BA-VesnaNeural",
    "ca-ES": "ca-ES-JoanaNeural",
    "cs-CZ": "cs-CZ-VlastaNeural",
    "cy-GB": "cy-GB-NiaNeural",
    "da-DK": "da-DK-ChristelNeural",
    "et-EE": "et-EE-AnuNeural",
    "ga-IE": "ga-IE-OrlaNeural",
    "gl-ES": "gl-ES-SabelaNeural",
    "hr-HR": "hr-HR-GabrijelaNeural",
    "is-IS": "is-IS-GudrunNeural",
    "lv-LV": "lv-LV-EveritaNeural",
    "lt-LT": "lt-LT-OnaNeural",
    "hu-HU": "hu-HU-NoemiNeural",
    "mt-MT": "mt-MT-GraceNeural",
    "nb-NO": "nb-NO-PernilleNeural",
    "ro-RO": "ro-RO-AlinaNeural",
    "sq-AL": "sq-AL-AnilaNeural",
    "sk-SK": "sk-SK-ViktoriaNeural",
    "sl-SI": "sl-SI-PetraNeural",
    "fi-FI": "fi-FI-NooraNeural",
    "sv-SE": "sv-SE-SofieNeural",
    "tr-TR": "tr-TR-EmelNeural",
    "el-GR": "el-GR-AthinaNeural",
    "bg-BG": "bg-BG-KalinaNeural",
    "mk-MK": "mk-MK-MarijaNeural",
    "sr-RS": "sr-RS-SophieNeural",
    "uk-UA": "uk-UA-PolinaNeural",
}


class TTSError(RuntimeError):
    pass


def default_voice(language: str) -> str:
    return DEFAULT_VOICES.get(language, DEFAULT_VOICES.get(language.split("-")[0], "en-US-AriaNeural"))


def _run_async(coro):
    """在当前（工作）线程里跑 asyncio 协程。"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)


def list_voices(force: bool = False) -> List[Dict[str, str]]:
    """返回 edge-tts 可用声音列表（带 5 分钟缓存）。"""
    now = time.time()
    if not force and _voices_cache["data"] and now - _voices_cache["ts"] < 300:
        return _voices_cache["data"]
    try:
        import edge_tts
        raw = _run_async(edge_tts.list_voices())
    except Exception as e:
        raise TTSError(i18n.t("获取语音列表失败（需要联网）：{error}", error=e))
    data = []
    for v in raw:
        data.append({
            "name": v.get("ShortName", ""),
            "locale": v.get("Locale", ""),
            "gender": v.get("Gender", ""),
            "friendly": (v.get("FriendlyName", "") or "")
                        .replace("Microsoft ", "").replace(" Online (Natural)", ""),
            "tags": ",".join((v.get("VoiceTag", {}) or {}).get("VoicePersonalities", []) or []),
        })
    data.sort(key=lambda x: (x["locale"], x["name"]))
    _voices_cache.update({"ts": now, "data": data})
    return data


async def _edge_synth(text: str, voice: str, out_path: Path,
                      rate: str, volume: str, pitch: str = "+0Hz"):
    import edge_tts
    kwargs: Dict[str, str] = {}
    if rate:
        kwargs["rate"] = rate
    if volume:
        kwargs["volume"] = volume
    if pitch and pitch != "+0Hz":
        kwargs["pitch"] = pitch
    comm = edge_tts.Communicate(text, voice, **kwargs)
    boundaries: List[Dict[str, float]] = []
    with open(out_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            # 英文一般给 WordBoundary，中文给 SentenceBoundary，两者都收
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                boundaries.append({
                    "t": chunk["offset"] / 10_000_000.0,       # 100ns -> 秒
                    "d": chunk["duration"] / 10_000_000.0,
                    "text": chunk.get("text", ""),
                })
    return boundaries


def _pyttsx3_synth(text: str, out_path: Path) -> None:
    import pyttsx3
    engine = pyttsx3.init()
    engine.save_to_file(text, str(out_path))
    engine.runAndWait()
    engine.stop()


def synth(text: str, voice: str, out_path: Path,
          rate: str = "", volume: str = "", pitch: str = "") -> Tuple[float, List[Dict[str, float]]]:
    """合成一段语音，返回 (时长秒, 词边界列表)。音色带服务前缀（doubao: / minimax: / qwen:）的交给商用服务。"""
    text = (text or "").strip()
    if not text:
        raise TTSError(i18n.t("解说文本为空。"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from . import tts_cloud
    if tts_cloud.is_cloud(voice):
        try:
            return tts_cloud.synth(text, voice, out_path, rate, volume)
        except tts_cloud.CloudTTSError as e:
            raise TTSError(str(e))
    cfg = config.load()
    rate = rate or cfg.get("tts_rate", "+0%")
    volume = volume or cfg.get("tts_volume", "+0%")

    boundaries: List[Dict[str, float]] = []
    part = out_path.with_name(f"{out_path.stem}.{uuid.uuid4().hex[:6]}.part.mp3")
    try:
        boundaries = _run_async(_edge_synth(text, voice, part, rate, volume, pitch or "+0Hz"))
        if not part.exists() or part.stat().st_size < 512:
            raise TTSError(i18n.t("edge-tts 返回空音频"))
    except Exception as e:
        # 兜底：SAPI5（只能生成 wav，且无词边界）
        wav = part.with_suffix(".wav")
        try:
            _pyttsx3_synth(text, wav)
            if wav.exists() and wav.stat().st_size > 512:
                ffmpeg_util.run(["-i", str(wav), "-codec:a", "libmp3lame",
                                 "-b:a", "192k", str(part)])
                boundaries = []
            else:
                raise TTSError(i18n.t("SAPI5 也没有生成音频"))
        except Exception as e2:
            part.unlink(missing_ok=True)
            raise TTSError(i18n.t("语音合成失败：{error}；离线兜底同样失败：{error2}", error=e, error2=e2))
        finally:
            wav.unlink(missing_ok=True)
    os.replace(part, out_path)

    dur = ffmpeg_util.probe_duration(out_path)
    if dur <= 0 and boundaries:
        last = boundaries[-1]
        dur = last["t"] + last["d"] + 0.3
    return dur, boundaries


def _card_sidecar(audio_dir: Path, kind: str) -> Path:
    return audio_dir / f"__{kind}__.txt"


def card_audio(audio_dir: Path, kind: str, text: str, voice: str = "") -> Optional[Path]:
    """片头 / 片尾的配音，仅当它正是用当前文案（给了 voice 时还要是这个音色）合成的才返回。"""
    import json
    mp3 = audio_dir / f"__{kind}__.mp3"
    side = _card_sidecar(audio_dir, kind)
    if not (text or "").strip() or not mp3.exists() or mp3.stat().st_size < 512:
        return None
    try:
        raw = side.read_text(encoding="utf-8")
    except OSError:
        return None          # 没有记录（老版本生成的）也当作过期
    try:
        meta = json.loads(raw)
        meta = meta if isinstance(meta, dict) else {"text": raw}
    except ValueError:
        meta = {"text": raw}  # 上个版本只记了文案
    if meta.get("text") != text.strip():
        return None
    if voice and meta.get("voice") and meta["voice"] != voice:
        return None          # 换了音色（比如两人问答换了主持人）：要重新合成
    return mp3


def synth_card(audio_dir: Path, kind: str, text: str, voice: str,
               rate: str = "", volume: str = "") -> float:
    import json
    mp3 = audio_dir / f"__{kind}__.mp3"
    dur, _ = synth(text, voice, mp3, rate, volume)
    _card_sidecar(audio_dir, kind).write_text(json.dumps({"text": text.strip(), "voice": voice}, ensure_ascii=False),
                                              encoding="utf-8")
    return dur


def synth_project(
    proj: Project,
    voice: str = "",
    rate: str = "",
    volume: str = "",
    only_missing: bool = True,
    audio_dir: Optional[Path] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """为整个项目（片头 + 每步 + 片尾）合成语音。"""
    from .. import storage
    audio_dir = audio_dir or storage.audio_dir(proj.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    dialogue_on = proj.is_dialogue()
    if dialogue_on:
        # 双人问答：台词按说话人各用各的音色；片头片尾和没有台词的解说由主持人念
        from . import dialogue
        voice = dialogue.speaker(proj, "host").voice
    voice = voice or proj.voice or default_voice(proj.language)
    proj.voice = voice

    # 关掉的片头 / 片尾不会进视频，也就不用花时间配音
    cfg = {**config.load(), **{k: v for k, v in (proj.settings or {}).items() if v is not None}}
    todo: List[Tuple[str, Optional[Step], str]] = []
    if proj.intro and cfg.get("intro_enabled", True):
        todo.append(("__intro__", None, proj.intro))
    for s in proj.steps:
        # 原声配音永远不被 AI 覆盖；要换成 AI 需要先显式「改用 AI 配音」
        # 播视频原声的步骤不配音（解说只当字幕）
        if s.include and s.voice_source != "own" and (s.narration or "").strip() and not s.plays_clip_audio():
            todo.append((s.id, s, s.narration))
    if proj.outro and cfg.get("outro_enabled", True):
        todo.append(("__outro__", None, proj.outro))

    done = 0
    made = 0
    errors: List[str] = []
    for key, step, text in todo:
        done += 1
        fname = f"{key}.mp3"
        path = audio_dir / fname
        if step is not None:
            if only_missing and step.audio and (audio_dir / step.audio).exists() and step.audio_duration > 0:
                continue
        else:
            if only_missing and card_audio(audio_dir, key.strip("_"), text, voice):
                continue
        if progress:
            progress(done / max(1, len(todo)), i18n.t("合成语音 {i}/{n}：{text}…", i=done, n=len(todo), text=text[:18]))
        times: List[List[float]] = []
        try:
            if step is None:
                dur, bounds = synth_card(audio_dir, key.strip("_"), text, voice, rate, volume), []
            elif dialogue_on and step.lines:
                dur, bounds, times = dialogue.synth_lines(proj, step, path, rate, volume)
            else:
                dur, bounds = synth(text, voice, path, rate, volume)
        except Exception as e:
            errors.append(f"[{key}] {e}")
            continue
        made += 1
        if step is not None:
            step.audio = fname
            step.voice_source = "tts"
            step.audio_duration = dur
            step.boundaries = bounds
            step.line_times = times
    if progress:
        progress(1.0, i18n.t("语音完成，共生成 {n} 段，{failed} 段失败", n=made, failed=len(errors)) if errors
                 else i18n.t("语音完成，共生成 {n} 段", n=made))
    return {"generated": made, "total": len(todo), "errors": errors, "voice": voice}


_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\.])\s*")


def split_sentences(text: str, max_len: int = 0) -> List[str]:
    """按标点切句，用于字幕分行。"""
    text = (text or "").strip()
    if not text:
        return []
    # 换行也断开：双人问答一句一行，一条字幕不会跨两个人
    parts = [p.strip() for para in text.split("\n") for p in _SENT_SPLIT.split(para) if p.strip()]
    if not max_len:
        return parts
    out: List[str] = []
    for p in parts:
        while len(p) > max_len:
            cut = -1
            for ch in ("，", ",", "、", "；", ";", " "):
                cut = max(cut, p.rfind(ch, 0, max_len + 1))
            if cut < max_len // 2:          # 没有合适断点就硬切
                cut = max_len - 1
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    # 把只剩标点或过短的尾巴并回上一行，避免出现「。」这样的单独字幕
    merged: List[str] = []
    for line in out:
        if merged and len(line) <= 2:
            merged[-1] += line
        else:
            merged.append(line)
    return merged
