"""Speech synthesis.

Main engine: edge-tts (Microsoft Edge neural voices; free, no key, many languages)
Fallback: pyttsx3 (Windows SAPI5, offline)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from .. import config, i18n
from ..models import Project, Step
from . import ffmpeg_util

_voices_cache: Dict[str, Any] = {"ts": 0.0, "data": []}

# Recommended voice per language (UI defaults)
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
    """Run an asyncio coroutine in the current (worker) thread."""
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
    """List of available edge-tts voices (cached for 5 minutes)."""
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
            # English usually gets WordBoundary, Chinese SentenceBoundary; collect both
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                boundaries.append({
                    "t": chunk["offset"] / 10_000_000.0,       # 100 ns -> seconds
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
    """Synthesise speech; returns (duration in seconds, word boundaries). Voices with a service prefix (doubao: / minimax: / qwen: / gemini: / elevenlabs: / azure:) go to the paid services."""
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
        # fallback: SAPI5 (wav only, no word boundaries)
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
    """Intro / outro voice-over, returned only if it was synthesised from the current text (and, when voice is given, with that voice)."""
    import json
    mp3 = audio_dir / f"__{kind}__.mp3"
    side = _card_sidecar(audio_dir, kind)
    if not (text or "").strip() or not mp3.exists() or mp3.stat().st_size < 512:
        return None
    try:
        raw = side.read_text(encoding="utf-8")
    except OSError:
        return None          # no record (made by an old version) also counts as outdated
    try:
        meta = json.loads(raw)
        meta = meta if isinstance(meta, dict) else {"text": raw}
    except ValueError:
        meta = {"text": raw}  # the previous version stored only the text
    if meta.get("text") != text.strip():
        return None
    if voice and meta.get("voice") and meta["voice"] != voice:
        return None          # the voice changed (e.g. a new host in Q&A mode): synthesise again
    return mp3


def synth_card(audio_dir: Path, kind: str, text: str, voice: str,
               rate: str = "", volume: str = "") -> float:
    import json
    mp3 = audio_dir / f"__{kind}__.mp3"
    dur, _ = synth(text, voice, mp3, rate, volume)
    _card_sidecar(audio_dir, kind).write_text(json.dumps({"text": text.strip(), "voice": voice}, ensure_ascii=False),
                                              encoding="utf-8")
    return dur


class _Para(NamedTuple):
    pos: int                      # place in the whole list, from 1 (what the progress message shows)
    key: str
    step: Optional[Step]          # None = the intro or the outro
    text: str
    fname: str
    path: Path


def _tts_workers(voice: str, n: int) -> int:
    """How many paragraphs to voice at once. Mostly waiting for the service, so a few in parallel is several times faster; paid services
    limit parallel requests by plan, so they get fewer. `tts_workers` in the settings overrides (0 = automatic)."""
    want = int(config.load().get("tts_workers", 0) or 0)
    if want <= 0:
        from . import tts_cloud
        want = 2 if tts_cloud.is_cloud(voice) else 3
    return max(1, min(want, n))


def synth_project(
    proj: Project,
    voice: str = "",
    rate: str = "",
    volume: str = "",
    only_missing: bool = True,
    audio_dir: Optional[Path] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """Synthesise the voice-over for the whole project (intro + every step + outro)."""
    from .. import storage
    audio_dir = audio_dir or storage.audio_dir(proj.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    dialogue_on = proj.is_dialogue()
    if dialogue_on:
        # two-person Q&A: lines use their speaker's voice; intro, outro and narration without lines are read by the host
        from . import dialogue
        voice = dialogue.speaker(proj, "host").voice
    voice = voice or proj.voice or default_voice(proj.language)
    proj.voice = voice

    # a disabled intro / outro isn't in the video, so don't spend time voicing it
    cfg = {**config.load(), **{k: v for k, v in (proj.settings or {}).items() if v is not None}}
    todo: List[Tuple[str, Optional[Step], str]] = []
    if proj.intro and cfg.get("intro_enabled", True):
        todo.append(("__intro__", None, proj.intro))
    for s in proj.steps:
        # the user's own recordings are never overwritten by the AI; switching to AI requires an explicit "Switch to AI voice"
        # steps playing the video's own sound get no voice-over (the narration is only a subtitle)
        if s.include and s.voice_source != "own" and (s.narration or "").strip() and not s.plays_clip_audio():
            todo.append((s.id, s, s.narration))
    if proj.outro and cfg.get("outro_enabled", True):
        todo.append(("__outro__", None, proj.outro))

    made = 0
    errors: List[str] = []
    pending: List[_Para] = []
    for pos, (key, step, text) in enumerate(todo, 1):
        fname = f"{key}.mp3"
        if step is not None:
            if only_missing and step.audio and (audio_dir / step.audio).exists() and step.audio_duration > 0:
                continue
        else:
            if only_missing and card_audio(audio_dir, key.strip("_"), text, voice):
                continue
        pending.append(_Para(pos, key, step, text, fname, audio_dir / fname))

    def synth_para(para: _Para) -> Tuple[float, List[Dict[str, float]], List[List[float]], Optional[Path]]:
        """Runs in a worker thread. A step's voice-over is made in a file of its own; it only replaces the real audio file in take(), together with the
        step's record (duration, subtitle times), so a paragraph that is dropped (Stop) never leaves new audio next to old times."""
        if para.step is None:
            return synth_card(audio_dir, para.key.strip("_"), para.text, voice, rate, volume), [], [], None
        out = para.path.with_name(f"{para.path.stem}.{uuid.uuid4().hex[:6]}.new.mp3")
        try:
            if dialogue_on and para.step.lines:
                return (*dialogue.synth_lines(proj, para.step, out, rate, volume), out)
            dur, bounds = synth(para.text, voice, out, rate, volume)
            return dur, bounds, [], out
        except BaseException:
            out.unlink(missing_ok=True)
            raise

    def take(para: _Para, res: Tuple[float, List[Dict[str, float]], List[List[float]], Optional[Path]]) -> None:
        """Back in the calling thread: the new audio file and the step's record change together."""
        nonlocal made
        dur, bounds, times, new = res
        if new is not None:
            try:
                storage._replace_with_retry(new, para.path)
            except OSError as e:
                new.unlink(missing_ok=True)
                errors.append(f"[{para.key}] {e}")
                return
        made += 1
        if para.step is not None:
            para.step.audio = para.fname
            para.step.voice_source = "tts"
            para.step.audio_duration = dur
            para.step.boundaries = bounds
            para.step.line_times = times

    def note_progress(para: _Para) -> None:
        """Says which paragraph is next; this is also where a Stop from the user is noticed (the progress callback raises)."""
        if progress:
            progress(para.pos / max(1, len(todo)), i18n.t("合成语音 {i}/{n}：{text}…", i=para.pos, n=len(todo), text=para.text[:18]))

    # Voiced a few at a time: each paragraph is mostly waiting for the service, so this is several times faster. At most `workers` are in flight;
    # a finished one is saved first, then Stop is checked, then the next one starts: no new one starts after a Stop (with one worker this is exactly
    # the old one-by-one order). The ones already in flight when Stop comes are waited for and saved too, the way the single one used to be.
    # Results are applied in this thread only. Q&A steps already voice their lines in parallel, so those go one step at a time.
    workers = 1 if dialogue_on else _tts_workers(voice, len(pending))
    queue = list(pending)
    flying: List[Tuple[_Para, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts") as ex:
        def launch() -> None:
            if queue:
                para = queue.pop(0)
                flying.append((para, ex.submit(synth_para, para)))

        try:
            if queue:
                note_progress(queue[0])
            for _ in range(workers):
                launch()
            while flying:
                para, fut = flying.pop(0)
                try:
                    res = fut.result()
                except Exception as e:
                    errors.append(f"[{para.key}] {e}")
                else:
                    take(para, res)
                if flying or queue:
                    note_progress(flying[0][0] if flying else queue[0])
                launch()
        except BaseException:
            ex.shutdown(wait=True, cancel_futures=True)      # stopped (or an error): nothing new starts, the ones in flight finish
            for para, fut in flying:
                if not fut.cancelled() and fut.exception() is None:
                    take(para, fut.result())                 # finished work is kept, with its record
            raise
    if progress:
        progress(1.0, i18n.t("语音完成，共生成 {n} 段，{failed} 段失败", n=made, failed=len(errors)) if errors
                 else i18n.t("语音完成，共生成 {n} 段", n=made))
    return {"generated": made, "total": len(todo), "errors": errors, "voice": voice}


# A full stop ends a sentence only when nothing glues it to the next word: a space after it, or a capital letter / Chinese character directly
# after it ("chapter.Nova"). Not inside a number or a name ("5.7 kg", "v1.8.1", "example.com"), or a subtitle would turn the page at "5."
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|(?<=\.[\"'”’」』）)\]])\s+|(?<=\.)(?=[A-Z一-鿿])")


def _in_token(ch: str) -> bool:
    """Part of a number or a Latin word ("5.7", "3.5mm", "A4"), which a hard cut must not tear apart."""
    return ch.isascii() and (ch.isalnum() or ch in ".,/-%")


def split_sentences(text: str, max_len: int = 0) -> List[str]:
    """Split into sentences at punctuation, for subtitle lines."""
    text = (text or "").strip()
    if not text:
        return []
    # also break at line breaks: Q&A has one sentence per line, so a subtitle never spans two speakers
    parts = [p.strip() for para in text.split("\n") for p in _SENT_SPLIT.split(para) if p.strip()]
    if not max_len:
        return parts
    out: List[str] = []
    for p in parts:
        while len(p) > max_len:
            cut = -1
            for ch in ("，", ",", "、", "；", ";", " "):
                cut = max(cut, p.rfind(ch, 0, max_len + 1))
            if cut < max_len // 2:          # no good break point: hard cut, but not through the middle of a number or a Latin word
                cut = max_len - 1
                while cut > max_len // 2 and _in_token(p[cut]) and _in_token(p[cut + 1]):
                    cut -= 1
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    # merge a tail that is just punctuation or very short back into the previous line, so a subtitle never shows a lone full stop
    merged: List[str] = []
    for line in out:
        if merged and len(line) <= 2:
            merged[-1] += line
        else:
            merged.append(line)
    return merged
