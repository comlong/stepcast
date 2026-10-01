"""Make Pillow draw text of any language correctly: pick a font that has the characters + Arabic joining + right-to-left order.

Pillow doesn't do either by itself (Pillow on Windows has no raqm / fribidi):
- Fonts: the default Microsoft YaHei has no Arabic, Hebrew, Hindi, Thai or Korean and lacks some Vietnamese letters, so they come out as boxes.
  Here a Windows font that has all characters actually used in the text is picked.
- Arabic / Persian / Urdu: letters change shape depending on their neighbours; Hebrew and Arabic are written right to left.
  arabic-reshaper does the joining and python-bidi reorders by the Unicode bidi algorithm, giving a string that can be drawn left to right.

Brahmic scripts such as Hindi and Thai have more complex combining rules and need HarfBuzz for full shaping; here we ensure no boxes
and that line breaks never separate vowel signs from their consonants.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, List, Optional

from PIL import features

from .. import config

try:
    import arabic_reshaper
except Exception:  # pragma: no cover - without it, no joining
    arabic_reshaper = None
try:
    from bidi import get_display
except Exception:  # pragma: no cover
    try:
        from bidi.algorithm import get_display
    except Exception:
        get_display = None

# When Pillow has raqm (HarfBuzz + FriBiDi) it shapes text itself and must not be processed by hand again
RAQM = bool(features.check("raqm"))

_FONTS = r"C:\Windows\Fonts"
# (regular, bold). The order is the priority: Microsoft YaHei first, so Chinese / English look as before;
# for missing characters try the next ones. All ship with Windows 10 / 11.
_CANDIDATES = [
    ("msyh.ttc", "msyhbd.ttc"),           # Chinese, Japanese, English
    ("segoeui.ttf", "segoeuib.ttf"),      # Latin, Cyrillic, Greek, Vietnamese, Arabic, Hebrew
    ("malgun.ttf", "malgunbd.ttf"),       # Korean
    ("YuGothM.ttc", "YuGothB.ttc"),       # Japanese (rare characters)
    ("Nirmala.ttf", "NirmalaB.ttf"),      # Hindi, Bengali, Tamil and other Indic scripts
    ("LeelawUI.ttf", "LeelaUIb.ttf"),     # Thai, Lao, Khmer
    ("tahoma.ttf", "tahomabd.ttf"),       # Arabic, Hebrew, Thai (fallback)
    ("ebrima.ttf", "ebrimabd.ttf"),       # Amharic (Ethiopia) and other African scripts
    ("arial.ttf", "arialbd.ttf"),
    ("simhei.ttf", "simhei.ttf"),
    ("seguisym.ttf", "seguisym.ttf"),     # symbols
]
_LINUX = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"]

_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_ARABIC = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def _candidates(bold: bool) -> List[str]:
    out: List[str] = []
    custom = config.get("font_path")
    if custom and Path(custom).exists():
        out.append(custom)                       # the font chosen in the settings always comes first
    for reg, bd in _CANDIDATES:
        p = str(Path(_FONTS) / (bd if bold else reg))
        if Path(p).exists():
            out.append(p)
    out += [p for p in _LINUX if Path(p).exists()]
    return out


@lru_cache(maxsize=64)
def _charset(path: str) -> FrozenSet[int]:
    """The characters a font has (reads only the cmap table, fast)."""
    try:
        from fontTools.ttLib import TTCollection, TTFont
        if path.lower().endswith((".ttc", ".otc")):
            font = TTCollection(path, lazy=True).fonts[0]
        else:
            font = TTFont(path, lazy=True)
        return frozenset((font.getBestCmap() or {}).keys())
    except Exception:
        return frozenset()


@lru_cache(maxsize=1024)
def font_path_for(text: str, bold: bool = False) -> str:
    """Pick a font that can draw every character of text; if none covers all, the one missing the fewest."""
    cands = _candidates(bold)
    if not cands:
        return config.font_path(bold)
    need = {ord(c) for c in text if not c.isspace() and unicodedata.category(c)[0] != "C"}
    if not need:
        return cands[0]
    best, best_missing = cands[0], None
    for p in cands:
        cs = _charset(p)
        if not cs:
            continue
        missing = len(need - cs)
        if missing == 0:
            return p
        if best_missing is None or missing < best_missing:
            best, best_missing = p, missing
    return best


# Scripts that need a real shaping engine: right-to-left ones (Hebrew, Arabic, Syriac …), Indic, Thai / Lao, Tibetan, Myanmar, Khmer
_COMPLEX = re.compile("[\u0590-\u08FF\u0900-\u0DFF\u0E00-\u0EFF\u0F00-\u0FFF"
                      "\u1000-\u109F\u1780-\u17FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def is_complex(text: str) -> bool:
    return bool(text) and bool(_COMPLEX.search(text))


def is_rtl(text: str) -> bool:
    return bool(text) and bool(_RTL.search(text))


@lru_cache(maxsize=4096)
def shape(text: str) -> str:
    """Turn the text into a string Pillow can draw left to right (Arabic joining + right-to-left reordering)."""
    if RAQM or not text or not _RTL.search(text):
        return text
    s = text
    if arabic_reshaper is not None and _ARABIC.search(s):
        try:
            s = arabic_reshaper.reshape(s)
        except Exception:
            pass
    if get_display is not None:
        try:
            s = get_display(s)
        except Exception:
            pass
    return s


def clusters(text: str) -> List[str]:
    """Split into "character + the marks attached to it": line breaks must not separate Thai tone marks or Hindi vowel signs from their letter."""
    out: List[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        joins = cat in ("Mn", "Mc", "Me") or ch in ("\u200d", "\u200c")
        # the previous character ends with a zero-width joiner or a virama (the Indic "half letter" mark, combining class 9): it forms one unit with this character
        after_link = bool(out) and (out[-1][-1] == "\u200d" or unicodedata.combining(out[-1][-1]) == 9)
        if out and (joins or after_link):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def layout_font_kwargs() -> dict:
    """Extra arguments for ImageFont.truetype: with raqm, let it do the shaping."""
    if RAQM:
        from PIL import ImageFont
        return {"layout_engine": ImageFont.Layout.RAQM}
    return {}


def strip_trailing_punct(text: str, extra: Optional[str] = None) -> str:
    """Strip trailing commas / semicolons and the like (not needed in subtitles), including Arabic ، ؛"""
    return text.rstrip("，,、；;\u060c\u061b" + (extra or ""))
