"""字幕生成：SRT（外挂）+ ASS（烧录进画面）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .tts import split_sentences


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    """一段语音在时间轴上的位置。"""
    start: float
    duration: float
    text: str
    boundaries: List[Dict[str, float]] = field(default_factory=list)


def _is_cjk(text: str) -> bool:
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ")  # i18n: ignore
    return cjk > len(text) * 0.2


def _char_time_map(boundaries: Sequence[Dict[str, float]]) -> List[tuple[int, float]]:
    """把词边界转成 (累计字符数, 时间) 的映射表：第 0 个字对应第一个词开始说的时间
    （视频里的讲话往往过几秒才开口，字幕不该提前出来），之后每个词对应它说完的时间。"""
    out: List[tuple[int, float]] = [(0, float(boundaries[0].get("t", 0.0)) if boundaries else 0.0)]
    acc = 0
    for b in boundaries:
        t = float(b.get("t", 0.0))
        if acc > 0 and t > out[-1][1] + 0.05:
            out.append((acc, t))          # 前面有停顿（换句、换人）：同一个位置再记一个「开口」的时间
        acc += len(b.get("text", "") or "")
        out.append((acc, t + float(b.get("d", 0.0))))
    return out


def _time_at_char(cmap: List[tuple[int, float]], char_pos: int, total_chars: int,
                  duration: float, start: bool = False) -> float:
    """第 char_pos 个字的时间。start=True 用来算一行字幕从哪开始：正好落在停顿处时取停顿后开口的时间，
    不然字幕会在上一句刚说完、下一句还没开口时就提前出来。"""
    if not cmap or len(cmap) < 2 or cmap[-1][0] <= 0:
        return duration * (char_pos / max(1, total_chars))
    scale = cmap[-1][0] / max(1, total_chars)
    target = char_pos * scale
    if start:
        exact = [t for c, t in cmap if abs(c - target) < 1e-6]
        if len(exact) > 1:
            return max(exact)
    prev_c, prev_t = cmap[0]
    for c, t in cmap:
        if c >= target:
            if c == prev_c:
                return t
            ratio = (target - prev_c) / (c - prev_c)
            return prev_t + (t - prev_t) * ratio
        prev_c, prev_t = c, t
    return cmap[-1][1]


def segment_to_cues(seg: Segment, max_chars: Optional[int] = None) -> List[Cue]:
    text = (seg.text or "").strip()
    if not text or seg.duration <= 0:
        return []
    if max_chars is None:
        max_chars = 20 if _is_cjk(text) else 46
    lines = split_sentences(text, max_chars)
    if not lines:
        return []
    total = sum(len(x) for x in lines) or 1
    cmap = _char_time_map(seg.boundaries)
    cues: List[Cue] = []
    pos = 0
    for line in lines:
        t0 = _time_at_char(cmap, pos, total, seg.duration, start=True)
        pos += len(line)
        t1 = _time_at_char(cmap, pos, total, seg.duration)
        if t1 <= t0:
            t1 = t0 + max(0.6, seg.duration / len(lines))
        # 截取范围以外的讲话时间是负的 / 超出这一段：夹到这一段以内，完全在外面的会被下面滤掉
        cues.append(Cue(seg.start + max(0.0, t0), seg.start + min(t1, seg.duration), line))
    # 修掉重叠
    for i in range(len(cues) - 1):
        if cues[i].end > cues[i + 1].start:
            cues[i].end = cues[i + 1].start
    return [c for c in cues if c.end > c.start + 0.05]


def build_cues(segments: Sequence[Segment]) -> List[Cue]:
    cues: List[Cue] = []
    for seg in segments:
        cues.extend(segment_to_cues(seg))
    return cues


# ---- SRT ----------------------------------------------------------------

def _srt_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: Sequence[Cue], path: Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(c.start)} --> {_srt_time(c.end)}")
        lines.append(c.text)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---- ASS（烧录用，可控制样式） --------------------------------------------

def _ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR"""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{size},{primary},{primary},{outline},{back},0,0,0,0,100,100,0,0,3,{border},0,2,{ml},{mr},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(cues: Sequence[Cue], path: Path, width: int = 1920, height: int = 1080,
              font_name: str = "Microsoft YaHei", font_size: int = 0,
              primary: str = "#FFFFFF", outline: str = "#000000",
              box_alpha: int = 90) -> Path:
    font_size = font_size or max(28, int(height * 0.040))
    margin_v = int(height * 0.055)
    header = ASS_HEADER.format(
        w=width, h=height, font=font_name, size=font_size,
        primary=_ass_color(primary), outline=_ass_color(outline, 0),
        back=_ass_color("#000000", box_alpha),
        border=max(2, int(font_size * 0.09)),
        ml=int(width * 0.10), mr=int(width * 0.10), mv=margin_v,
    )
    body = []
    for c in cues:
        txt = c.text.replace("\n", "\\N").replace("{", "(").replace("}", ")")
        body.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Sub,,0,0,0,,{txt}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(body) + "\n", encoding="utf-8")
    return path


def write_vtt(cues: Sequence[Cue], path: Path) -> Path:
    lines = ["WEBVTT", ""]
    for c in cues:
        lines.append(f"{_srt_time(c.start).replace(',', '.')} --> {_srt_time(c.end).replace(',', '.')}")
        lines.append(c.text)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
