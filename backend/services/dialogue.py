"""Two-person Q&A: the host asks, the expert explains.

- Default voices for the two speakers (female host, male expert) and their names (names only tell them apart in the editor; never spoken or shown in subtitles)
- Line-by-line voice-over: each line is synthesised with its speaker's voice, joined in order into the step's voice-over, with each line's start and end recorded
- Clean up AI-written lines: remove speaker prefixes such as "Xiaoxiao:" / "Host:" and names used to address the other speaker
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .. import i18n
from ..models import Project, Speaker, Step
from . import ffmpeg_util, tts

ROLES = ("host", "expert")
GAP = 0.22                   # pause between two lines (seconds): replies come quickly; long pauses don't sound like a conversation
SYNTH_WORKERS = 4            # lines of one step synthesised at the same time

# Male voice per language. The host uses the female voice from tts.DEFAULT_VOICES by default, the expert the male voice here
MALE_VOICES = {
    "zh-CN": "zh-CN-YunxiNeural", "zh-TW": "zh-TW-YunJheNeural",
    "en-US": "en-US-GuyNeural", "en-GB": "en-GB-RyanNeural",
    "ja-JP": "ja-JP-KeitaNeural", "ko-KR": "ko-KR-InJoonNeural",
    "fr-FR": "fr-FR-HenriNeural", "de-DE": "de-DE-ConradNeural",
    "pl-PL": "pl-PL-MarekNeural", "nl-NL": "nl-NL-MaartenNeural",
    "es-ES": "es-ES-AlvaroNeural", "pt-BR": "pt-BR-AntonioNeural",
    "ru-RU": "ru-RU-DmitryNeural", "it-IT": "it-IT-DiegoNeural",
    "th-TH": "th-TH-NiwatNeural", "vi-VN": "vi-VN-NamMinhNeural",
    "ar-SA": "ar-SA-HamedNeural", "hi-IN": "hi-IN-MadhurNeural",
    "pt-PT": "pt-PT-DuarteNeural", "bs-BA": "bs-BA-GoranNeural", "ca-ES": "ca-ES-EnricNeural",
    "cs-CZ": "cs-CZ-AntoninNeural", "cy-GB": "cy-GB-AledNeural", "da-DK": "da-DK-JeppeNeural",
    "et-EE": "et-EE-KertNeural", "ga-IE": "ga-IE-ColmNeural", "gl-ES": "gl-ES-RoiNeural",
    "hr-HR": "hr-HR-SreckoNeural", "is-IS": "is-IS-GunnarNeural", "lv-LV": "lv-LV-NilsNeural",
    "lt-LT": "lt-LT-LeonasNeural", "hu-HU": "hu-HU-TamasNeural", "mt-MT": "mt-MT-JosephNeural",
    "nb-NO": "nb-NO-FinnNeural", "ro-RO": "ro-RO-EmilNeural", "sq-AL": "sq-AL-IlirNeural",
    "sk-SK": "sk-SK-LukasNeural", "sl-SI": "sl-SI-RokNeural", "fi-FI": "fi-FI-HarriNeural",
    "sv-SE": "sv-SE-MattiasNeural", "tr-TR": "tr-TR-AhmetNeural", "el-GR": "el-GR-NestorasNeural",
    "bg-BG": "bg-BG-BorislavNeural", "mk-MK": "mk-MK-AleksandarNeural", "sr-RS": "sr-RS-NicholasNeural",
    "uk-UA": "uk-UA-OstapNeural",
}
# Chinese names of the Chinese voices (to tell speakers apart in the editor)
ZH_NAMES = {  # i18n: ignore
    "Xiaoxiao": "晓晓", "Xiaoyi": "晓伊", "Yunxi": "云希", "Yunjian": "云健", "Yunyang": "云扬",  # i18n: ignore
    "Yunxia": "云夏", "Xiaobei": "晓北", "Xiaoni": "晓妮", "HsiaoChen": "曉臻", "HsiaoYu": "曉雨",  # i18n: ignore
    "YunJhe": "雲哲", "HiuGaai": "曉佳", "HiuMaan": "曉曼", "WanLung": "雲龍",  # i18n: ignore
}
# speaker labels the AI may put in front of a line
ROLE_LABELS = ("主持人", "讲师", "专家", "嘉宾", "老师", "问", "答",  # i18n: ignore
               "host", "expert", "moderator", "presenter", "teacher", "speaker a", "speaker b", "q", "a")


# ---- speakers -----------------------------------------------------------------

def male_voice(lang: str) -> str:
    if lang in MALE_VOICES:
        return MALE_VOICES[lang]
    base = (lang or "").split("-")[0]
    return next((v for k, v in MALE_VOICES.items() if k.split("-")[0] == base), MALE_VOICES["en-US"])


def voice_name(voice: str) -> str:
    """The person name of a voice: zh-CN-XiaoxiaoNeural → 晓晓 (Xiaoxiao), en-US-GuyNeural → Guy, qwen:Cherry → Cherry."""
    from . import tts_cloud
    if tts_cloud.is_cloud(voice):
        return tts_cloud.voice_info(voice).get("short") or voice.split(":", 1)[1]
    base = (voice or "").split("-")[-1].replace("Multilingual", "").replace("Neural", "")
    return ZH_NAMES.get(base, base or voice)


def _speaks(voice: str, lang: str) -> bool:
    """Whether this voice can speak this language (many paid-service voices speak several)."""
    from . import tts_cloud
    base = lang.split("-")[0]
    if tts_cloud.is_cloud(voice):
        return base in (tts_cloud.voice_info(voice).get("langs") or [])
    return (voice or "").split("-")[0] == base


def default_speakers(lang: str, host_voice: str = "", expert_voice: str = "") -> List[Speaker]:
    hv = host_voice or tts.default_voice(lang)
    ev = expert_voice or male_voice(lang)
    return [Speaker(role="host", name=voice_name(hv), voice=hv),
            Speaker(role="expert", name=voice_name(ev), voice=ev)]


def ensure_speakers(proj: Project) -> List[Speaker]:
    """Make sure both speakers exist (old data or hand-edited JSON missing them gets the defaults for the language)."""
    have = {sp.role: sp for sp in proj.speakers if sp.role in ROLES}
    if len(have) == 2 and all(sp.voice and sp.name for sp in have.values()):
        return [have["host"], have["expert"]]
    defaults = {sp.role: sp for sp in default_speakers(proj.language, "" if "host" in have else proj.voice)}
    out = []
    for r in ROLES:
        sp = have.get(r) or defaults[r]
        sp.voice = sp.voice or defaults[r].voice
        sp.name = sp.name or voice_name(sp.voice)
        out.append(sp)
    proj.speakers = out
    return out


def speaker(proj: Project, role: str) -> Speaker:
    sps = ensure_speakers(proj)
    return next((sp for sp in sps if sp.role == role), sps[0])


def retarget(proj: Project, lang: str, host_voice: str = "") -> None:
    """Switching language: voices not in that language become its default voices; default names change along (names you chose are kept)."""
    for sp in ensure_speakers(proj):
        if sp.role == "host" and host_voice:
            want = host_voice
        elif not _speaks(sp.voice, lang):
            want = tts.default_voice(lang) if sp.role == "host" else male_voice(lang)
        else:
            continue
        if not sp.name or sp.name == voice_name(sp.voice):
            sp.name = voice_name(want)
        sp.voice = want
    proj.voice = speaker(proj, "host").voice


def speaker_names(proj: Project) -> List[str]:
    """Names of the two speakers (including the voices' own names): forms of address the AI writes into lines are removed."""
    out: List[str] = []
    for sp in proj.speakers:
        for n in (sp.name, voice_name(sp.voice)):
            if n and n not in out:
                out.append(n)
    return out


# ---- line-by-line voice-over -------------------------------------------------------

def synth_lines(proj: Project, step: Step, out_path: Path, rate: str = "",
                volume: str = "") -> Tuple[float, List[Dict[str, Any]], List[List[float]]]:
    """Synthesise a step's lines one by one (each with its speaker's voice) and join them into one voice-over.
    Returns (duration, word boundaries, [start, end] of each line). Boundaries are already on the joined voice-over's timeline, so subtitles stay in sync."""
    lines = [ln for ln in step.lines if (ln.text or "").strip()]
    if not lines:
        raise tts.TTSError(i18n.t("解说文本为空。"))
    voices = {sp.role: sp.voice for sp in ensure_speakers(proj)}
    tmp = out_path.parent / f"{out_path.stem}.lines_{uuid.uuid4().hex[:6]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        def one(k: int):
            p = tmp / f"{k:03d}.mp3"
            dur, bounds = tts.synth(lines[k].text, voices.get(lines[k].who) or voices["host"], p, rate, volume)
            return p, dur, bounds

        with ThreadPoolExecutor(max_workers=min(SYNTH_WORKERS, len(lines))) as ex:
            parts = list(ex.map(one, range(len(lines))))
        args: List[str] = []
        chains = []
        for k, (p, _, _) in enumerate(parts):
            args += ["-i", str(p)]
            pad = f",apad=pad_dur={GAP}" if k < len(parts) - 1 else ""
            chains.append(f"[{k}:a]aformat=sample_rates=24000:channel_layouts=mono{pad}[a{k}]")
        graph = ";".join(chains) + ";" + "".join(f"[a{k}]" for k in range(len(parts))) \
            + f"concat=n={len(parts)}:v=0:a=1[out]"
        part = tmp / "all.mp3"
        ffmpeg_util.run(args + ["-filter_complex", graph, "-map", "[out]", "-codec:a", "libmp3lame",
                                "-b:a", "128k", str(part)])
        os.replace(part, out_path)
        times: List[List[float]] = []
        bounds: List[Dict[str, Any]] = []
        t = 0.0
        for _, dur, b in parts:
            times.append([round(t, 3), round(t + dur, 3)])
            bounds += [{**x, "t": float(x.get("t", 0.0)) + t} for x in b]
            t += dur + GAP
        total = ffmpeg_util.probe_duration(out_path) or (t - GAP)
        return total, bounds, times
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- cleaning up lines ---------------------------------------------------------------

def clean_text(text: str, names: Iterable[str] = ()) -> str:
    """Remove speaker prefixes at the start ("Xiaoxiao:", "Host:", "[Expert]") and names used to address a speaker ("Yunxi, …", "…, Xiaoxiao?").
    Names only tell the speakers apart in the editor; they are never spoken or shown in subtitles."""
    names = [n for n in names if n]
    labels = sorted({*names, *ROLE_LABELS}, key=len, reverse=True)
    alt = "|".join(re.escape(x) for x in labels)
    text = re.sub(rf"^\s*[\[【(（]\s*(?:{alt})\s*[\]】)）]\s*[：:]?\s*", "", text, flags=re.I)    # [Host]
    text = re.sub(rf"^\s*(?:{alt})\s*[：:]\s*", "", text, flags=re.I)                           # Xiaoxiao:
    for n in sorted(names, key=len, reverse=True):
        e = re.escape(n)
        text = re.sub(rf"(^|[。！？!?.；;]\s*){e}\s*[，,、]\s*", r"\1", text)          # name at the start of a sentence
        text = re.sub(rf"\s*[，,]\s*{e}\s*(?=[。！？!?.]|$)", "", text)                # name at the end of a sentence
    return text.strip()


def normalize_lines(raw: Any, names: Iterable[str] = (), clean: bool = False) -> List[Dict[str, str]]:
    """Normalise lines from the AI or the editor to [{"who", "text"}]: who is only host / expert (Chinese and English aliases accepted); empty lines are dropped.
    With clean=True (AI-written lines) speaker prefixes and forms of address are removed as well."""
    alias = {"host": "host", "主持人": "host", "moderator": "host", "q": "host",  # i18n: ignore
             "expert": "expert", "讲师": "expert", "专家": "expert", "teacher": "expert", "a": "expert"}  # i18n: ignore
    names = list(names)
    out: List[Dict[str, str]] = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("t") or "").strip()
        if clean:
            text = clean_text(text, names)
        if not text:
            continue
        who = alias.get(str(it.get("who") or it.get("s") or "").strip().lower(), "")
        if not who:
            who = "expert" if out and out[-1]["who"] == "host" else "host"
        out.append({"who": who, "text": text})
    return out
