"""两人问答：主持人提问、讲师讲解。

- 两位讲者的默认音色（主持人女声、讲师男声）和名字（名字只在编辑器里区分是谁，不念、不上字幕）
- 逐句配音：每句用说话人的音色合成，再按顺序拼成这一步的配音，记下每句的起止时间
- 整理 AI 写的台词：去掉「晓晓：」「主持人：」这类说话人前缀和对名字的称呼
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
GAP = 0.22                   # 两句台词之间的停顿（秒）：接话要快，停太久就不像聊天了
SYNTH_WORKERS = 4            # 一步里的几句台词同时合成

# 每种语言的男声。主持人默认用 tts.DEFAULT_VOICES 里的女声，讲师用这里的男声
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
# 中文音色的中文名（编辑器里区分是谁说的）
ZH_NAMES = {  # i18n: ignore
    "Xiaoxiao": "晓晓", "Xiaoyi": "晓伊", "Yunxi": "云希", "Yunjian": "云健", "Yunyang": "云扬",  # i18n: ignore
    "Yunxia": "云夏", "Xiaobei": "晓北", "Xiaoni": "晓妮", "HsiaoChen": "曉臻", "HsiaoYu": "曉雨",  # i18n: ignore
    "YunJhe": "雲哲", "HiuGaai": "曉佳", "HiuMaan": "曉曼", "WanLung": "雲龍",  # i18n: ignore
}
# AI 可能写在台词前面的说话人标签
ROLE_LABELS = ("主持人", "讲师", "专家", "嘉宾", "老师", "问", "答",  # i18n: ignore
               "host", "expert", "moderator", "presenter", "teacher", "speaker a", "speaker b", "q", "a")


# ---- 讲者 -----------------------------------------------------------------

def male_voice(lang: str) -> str:
    if lang in MALE_VOICES:
        return MALE_VOICES[lang]
    base = (lang or "").split("-")[0]
    return next((v for k, v in MALE_VOICES.items() if k.split("-")[0] == base), MALE_VOICES["en-US"])


def voice_name(voice: str) -> str:
    """音色的人名：zh-CN-XiaoxiaoNeural → 晓晓，en-US-GuyNeural → Guy，qwen:Cherry → Cherry。"""
    from . import tts_cloud
    if tts_cloud.is_cloud(voice):
        return tts_cloud.voice_info(voice).get("short") or voice.split(":", 1)[1]
    base = (voice or "").split("-")[-1].replace("Multilingual", "").replace("Neural", "")
    return ZH_NAMES.get(base, base or voice)


def _speaks(voice: str, lang: str) -> bool:
    """这个音色能不能说这种语言（商用服务的音色很多能说好几种语言）。"""
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
    """两位讲者都齐（老数据、手改过的 JSON 缺了就按语言补上默认值）。"""
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
    """换语言：音色不是这种语言的换成这种语言的默认音色；名字还是默认名的跟着换（自己起的名字保留）。"""
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
    """两位讲者的名字（包括音色本来的名字）：AI 写进台词里的称呼要去掉。"""
    out: List[str] = []
    for sp in proj.speakers:
        for n in (sp.name, voice_name(sp.voice)):
            if n and n not in out:
                out.append(n)
    return out


# ---- 逐句配音 ---------------------------------------------------------------

def synth_lines(proj: Project, step: Step, out_path: Path, rate: str = "",
                volume: str = "") -> Tuple[float, List[Dict[str, Any]], List[List[float]]]:
    """一步的台词逐句合成（每句用说话人的音色），拼成一段配音。
    返回 (时长, 词边界, 每句的 [开始, 结束])。词边界已经换算到整段配音的时间上，字幕照样对得上。"""
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


# ---- 整理台词 ---------------------------------------------------------------

def clean_text(text: str, names: Iterable[str] = ()) -> str:
    """去掉句首的说话人前缀（「晓晓：」「主持人:」「【讲师】」）和对讲者名字的称呼（「云希，……」「……，晓晓？」）。
    名字只用来在编辑器里区分是谁，不念出来、也不上字幕。"""
    names = [n for n in names if n]
    labels = sorted({*names, *ROLE_LABELS}, key=len, reverse=True)
    alt = "|".join(re.escape(x) for x in labels)
    text = re.sub(rf"^\s*[\[【(（]\s*(?:{alt})\s*[\]】)）]\s*[：:]?\s*", "", text, flags=re.I)    # 【主持人】
    text = re.sub(rf"^\s*(?:{alt})\s*[：:]\s*", "", text, flags=re.I)                           # 晓晓：
    for n in sorted(names, key=len, reverse=True):
        e = re.escape(n)
        text = re.sub(rf"(^|[。！？!?.；;]\s*){e}\s*[，,、]\s*", r"\1", text)          # 句首称呼
        text = re.sub(rf"\s*[，,]\s*{e}\s*(?=[。！？!?.]|$)", "", text)                # 句尾称呼
    return text.strip()


def normalize_lines(raw: Any, names: Iterable[str] = (), clean: bool = False) -> List[Dict[str, str]]:
    """AI 或编辑器给的台词整理成 [{"who", "text"}]：who 只认 host / expert（也认中文、英文别名），空句丢掉。
    clean=True（AI 写的台词）时再去掉说话人前缀和对名字的称呼。"""
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
