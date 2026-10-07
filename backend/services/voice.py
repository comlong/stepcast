"""The user's own voice: per-step recording / uploaded audio / back to AI voice / dictation / narrate while recording."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import i18n, storage
from ..models import Project, Step, drop_stale_lines
from . import asr, ffmpeg_util

Progress = Optional[Callable[[float, str], None]]

MAX_STEP_BYTES = 200 * 1024 ** 2       # largest upload for one step's voice-over / a dictation
MAX_SESSION_BYTES = 2 * 1024 ** 3      # largest recording of a whole session (narrate while recording)
MAX_STEP_SECONDS = 120        # longest voice-over per step (recorded or uploaded), in seconds; the editor's recorder limits voice-over recordings to 60 seconds


def to_mp3(src: Path, dst: Path, start: float = 0.0, end: float = 0.0) -> float:
    """Any audio -> mp3 (optionally a time range); returns the duration."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    args: List[str] = ["-i", str(src)]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    if end > start:
        args += ["-to", f"{end:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame", "-q:a", "3", str(dst)]
    ffmpeg_util.run(args)
    return ffmpeg_util.probe_duration(dst)


# ---- dictation ---------------------------------------------------------------

def dictate(src: Path, language: str = "", progress: Progress = None) -> Dict[str, Any]:
    """Text only; the audio isn't kept."""
    work = src.with_suffix(".dictate.mp3")
    try:
        to_mp3(src, work)
        r = asr.transcribe(work, language, progress, words=False)
        return {"text": r["text"], "language": r["language"], "duration": r["duration"]}
    finally:
        work.unlink(missing_ok=True)


# ---- per step: your own recording as voice-over --------------------------------------------

def set_step_voice(proj: Project, step: Step, src: Path, transcribe: bool = True,
                   replace_text: bool = True, language: str = "",
                   progress: Progress = None) -> Dict[str, Any]:
    """Use a recording as this step's voice-over. The recognised text goes into the narration, for subtitles and for switching to an AI voice later."""
    audio_dir = storage.audio_dir(proj.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{step.id}_own.mp3"
    dst = audio_dir / fname
    tmp = audio_dir / f"{step.id}_own.{uuid.uuid4().hex[:6]}.part.mp3"
    if progress:
        progress(0.03, i18n.t("转换音频格式…"))
    try:
        try:
            dur = to_mp3(src, tmp)
        except ffmpeg_util.FFmpegError:
            raise ValueError(i18n.t("读不了这个音频文件：可能已损坏，或不是音频格式（支持 mp3 / wav / m4a / webm / ogg）"))
        if dur <= 0.2:
            raise ValueError(i18n.t("录音太短或没有声音"))
        if dur > MAX_STEP_SECONDS:
            raise ValueError(i18n.t("单步录音不能超过 {max} 秒（当前 {sec} 秒）", max=MAX_STEP_SECONDS, sec=f"{dur:.0f}"))

        text, bounds = "", []
        if transcribe:
            r = asr.transcribe(tmp, language or proj.language,
                               progress=lambda f, m: progress(0.05 + f * 0.9, m) if progress else None)
            text = r["text"]
            bounds = asr.words_to_boundaries(r["segments"], 0.0, r["language"])
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)

    old = step.audio
    step.audio = fname
    step.audio_duration = dur
    step.voice_source = "own"
    step.boundaries = bounds
    if text and (replace_text or not (step.narration or "").strip()):
        step.narration = text
        drop_stale_lines(step)
        step.caption = text
    if old and old != fname:
        (audio_dir / old).unlink(missing_ok=True)
    if progress:
        progress(1.0, i18n.t("已设为原声配音（{sec} 秒）", sec=f"{dur:.1f}"))
    return {"audio": fname, "duration": dur, "text": text, "transcribed": bool(text)}


def switch_to_ai(proj: Project, step: Step) -> None:
    """Keep the text, discard the recording, let the AI read it."""
    if step.voice_source == "own" and step.audio:
        (storage.audio_dir(proj.id) / step.audio).unlink(missing_ok=True)
    step.voice_source = "tts"
    step.audio = ""
    step.audio_duration = 0.0
    step.boundaries = []


def remove_voice(proj: Project, step: Step) -> None:
    if step.audio:
        (storage.audio_dir(proj.id) / step.audio).unlink(missing_ok=True)
    step.voice_source = "tts"
    step.audio = ""
    step.audio_duration = 0.0
    step.boundaries = []


# ---- narrate while recording: one long recording -> split across the steps by action time -------------------------------

LEAD_TOLERANCE = 0.8   # a sentence whose midpoint is up to this many seconds after a click still belongs to that step (talking while clicking)


def assign_segments(segments: List[Dict[str, Any]], click_times: List[float]) -> List[List[int]]:
    """Assign each recognised sentence to a step.

    People usually "say it, then click": a sentence belongs to the first action after it.
    Talking after the last action belongs to the last step.
    """
    buckets: List[List[int]] = [[] for _ in click_times]
    if not click_times:
        return buckets
    for si, seg in enumerate(segments):
        mid = (seg["start"] + seg["end"]) / 2
        idx = next((i for i, t in enumerate(click_times) if t >= mid - LEAD_TOLERANCE),
                   len(click_times) - 1)
        buckets[idx].append(si)
    return buckets


def import_session(proj: Project, src: Path, rec_start_ms: float, mode: str = "ai",
                   language: str = "", progress: Progress = None) -> Dict[str, Any]:
    """mode: ai = turn it into text for the AI voice (default) | own = keep the original voice, cut per step."""
    audio_dir = storage.audio_dir(proj.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    session = audio_dir / f"_session_{uuid.uuid4().hex[:6]}.mp3"
    try:
        return _import_session(proj, src, session, rec_start_ms, mode, language, progress)
    finally:
        session.unlink(missing_ok=True)


def _import_session(proj: Project, src: Path, session: Path, rec_start_ms: float, mode: str,
                    language: str, progress: Progress) -> Dict[str, Any]:
    audio_dir = session.parent
    if progress:
        progress(0.02, i18n.t("整理录音…"))
    total = to_mp3(src, session)
    if total < 0.5:
        raise ValueError(i18n.t("录音为空，可能麦克风没有声音"))

    lang = language or proj.language
    r = asr.transcribe(session, lang,
                       progress=lambda f, m: progress(0.04 + f * 0.8, m) if progress else None)
    segs = r["segments"]
    if not segs:
        return {"segments": 0, "steps": 0, "duration": total, "note": i18n.t("没有识别到说话内容")}

    steps = [s for s in proj.steps if s.include]
    rec_start = rec_start_ms / 1000.0
    times = [max(0.0, s.ts - rec_start) for s in steps]
    order = sorted(range(len(steps)), key=lambda i: times[i])
    steps = [steps[i] for i in order]
    times = [times[i] for i in order]
    buckets = assign_segments(segs, times)

    sep = "" if r["language"] in ("zh", "ja") else " "
    touched = 0
    for bi, (step, idxs) in enumerate(zip(steps, buckets)):
        if not idxs:
            continue
        chunk = [segs[i] for i in idxs]
        text = asr.clean_text(sep.join(c["text"] for c in chunk), r["language"])
        if not text:
            continue
        touched += 1
        step.narration = text
        drop_stale_lines(step)
        step.caption = text

        if mode == "own":
            first, last = min(idxs), max(idxs)
            prev_end = max((segs[i]["end"] for i in range(first)), default=0.0)
            next_start = min((segs[i]["start"] for i in range(last + 1, len(segs))), default=total)
            a = max(prev_end, chunk[0]["start"] - 0.15)
            b = min(next_start, chunk[-1]["end"] + 0.3)
            fname = f"{step.id}_own.mp3"
            dur = to_mp3(session, audio_dir / fname, a, b)
            step.audio = fname
            step.audio_duration = dur
            step.voice_source = "own"
            step.boundaries = asr.words_to_boundaries(chunk, a, r["language"])
        else:
            if step.voice_source == "own" and step.audio:
                (audio_dir / step.audio).unlink(missing_ok=True)
            step.voice_source = "tts"
            step.audio = ""
            step.audio_duration = 0.0
            step.boundaries = []
        if progress:
            progress(0.85 + 0.14 * (bi + 1) / len(steps), i18n.t("分配到第 {n} 步", n=bi + 1))

    if progress:
        progress(1.0, i18n.t("识别 {segments} 句，写入 {steps} 个步骤", segments=len(segs), steps=touched))
    return {"segments": len(segs), "steps": touched, "duration": total,
            "mode": mode, "language": r["language"]}


def save_upload(upload, dst_dir: Path, prefix: str = "upload", max_bytes: int = MAX_STEP_BYTES) -> Path:
    """Write a FastAPI UploadFile to disk (storage.UploadTooLarge if it is bigger than max_bytes)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix.lower() or ".webm"
    if suffix not in (".webm", ".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus", ".mp4"):
        suffix = ".webm"
    dst = dst_dir / f"{prefix}{suffix}"
    storage.copy_limited(upload.file, dst, max_bytes)
    return dst
