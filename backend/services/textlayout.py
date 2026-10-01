"""让任意语言的文字都能被 Pillow 正确画进视频：挑一个有这些字的字体 + 阿拉伯文连写 + 从右往左排。

Pillow 自己不做这两件事（Windows 上的 Pillow 没有 raqm / fribidi）：
- 字体：默认的微软雅黑没有阿拉伯、希伯来、印地、泰、韩文，也缺越南语的部分字母，画出来全是方块。
  这里按文字里实际出现的字符，从 Windows 自带字体里挑一个都有的。
- 阿拉伯 / 波斯 / 乌尔都文：字母要按前后位置连写变形；希伯来文和阿拉伯文都要从右往左排。
  用 arabic-reshaper 连写、python-bidi 按 Unicode 双向算法重排，得到可以直接从左往右画的字符串。

印地语等婆罗米系文字、泰文的组合规则更复杂，完整排版需要 HarfBuzz；这里保证不出方块、
换行不把元音符号和辅音拆开。
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
except Exception:  # pragma: no cover - 没装时退化成不连写
    arabic_reshaper = None
try:
    from bidi import get_display
except Exception:  # pragma: no cover
    try:
        from bidi.algorithm import get_display
    except Exception:
        get_display = None

# Pillow 带了 raqm（HarfBuzz + FriBiDi）时它自己会排版，不能再手动处理一遍
RAQM = bool(features.check("raqm"))

_FONTS = r"C:\Windows\Fonts"
# (常规, 粗体)。顺序就是优先级：先用原来的微软雅黑保持中文 / 英文的观感不变，
# 缺字了再往后找。都是 Windows 10 / 11 自带的字体。
_CANDIDATES = [
    ("msyh.ttc", "msyhbd.ttc"),           # 中文、日文、英文
    ("segoeui.ttf", "segoeuib.ttf"),      # 西文、西里尔、希腊、越南、阿拉伯、希伯来
    ("malgun.ttf", "malgunbd.ttf"),       # 韩文
    ("YuGothM.ttc", "YuGothB.ttc"),       # 日文（生僻字）
    ("Nirmala.ttf", "NirmalaB.ttf"),      # 印地、孟加拉、泰米尔等印度文字
    ("LeelawUI.ttf", "LeelaUIb.ttf"),     # 泰、老挝、高棉
    ("tahoma.ttf", "tahomabd.ttf"),       # 阿拉伯、希伯来、泰（兜底）
    ("ebrima.ttf", "ebrimabd.ttf"),       # 阿姆哈拉（埃塞俄比亚）等非洲文字
    ("arial.ttf", "arialbd.ttf"),
    ("simhei.ttf", "simhei.ttf"),
    ("seguisym.ttf", "seguisym.ttf"),     # 符号
]
_LINUX = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"]

_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_ARABIC = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def _candidates(bold: bool) -> List[str]:
    out: List[str] = []
    custom = config.get("font_path")
    if custom and Path(custom).exists():
        out.append(custom)                       # 用户在设置里指定的字体永远排第一
    for reg, bd in _CANDIDATES:
        p = str(Path(_FONTS) / (bd if bold else reg))
        if Path(p).exists():
            out.append(p)
    out += [p for p in _LINUX if Path(p).exists()]
    return out


@lru_cache(maxsize=64)
def _charset(path: str) -> FrozenSet[int]:
    """字体里有哪些字（只读 cmap 表，很快）。"""
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
    """挑一个能把 text 里所有字都画出来的字体；没有全包的就挑缺得最少的。"""
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


# 需要真正排版引擎的文字：从右往左的（希伯来、阿拉伯、叙利亚……）、印度系、泰 / 老挝、藏、缅、高棉
_COMPLEX = re.compile("[\u0590-\u08FF\u0900-\u0DFF\u0E00-\u0EFF\u0F00-\u0FFF"
                      "\u1000-\u109F\u1780-\u17FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def is_complex(text: str) -> bool:
    return bool(text) and bool(_COMPLEX.search(text))


def is_rtl(text: str) -> bool:
    return bool(text) and bool(_RTL.search(text))


@lru_cache(maxsize=4096)
def shape(text: str) -> str:
    """变成可以直接交给 Pillow 从左往右画的字符串（阿拉伯文连写 + 从右往左重排）。"""
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
    """按「字 + 附在它上面的符号」切开：换行时不能把泰文声调、印地文元音符号和前面的字母拆开。"""
    out: List[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        joins = cat in ("Mn", "Mc", "Me") or ch in ("\u200d", "\u200c")
        # 前一个字以零宽连接符或 virama（印度文字的「半字」符号，组合类 9）结尾：和这个字是一个整体
        after_link = bool(out) and (out[-1][-1] == "\u200d" or unicodedata.combining(out[-1][-1]) == 9)
        if out and (joins or after_link):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def layout_font_kwargs() -> dict:
    """给 ImageFont.truetype 的额外参数：有 raqm 就让它来排版。"""
    if RAQM:
        from PIL import ImageFont
        return {"layout_engine": ImageFont.Layout.RAQM}
    return {}


def strip_trailing_punct(text: str, extra: Optional[str] = None) -> str:
    """去掉句尾逗号 / 分号一类（字幕里不需要），包括阿拉伯文的 ، ؛"""
    return text.rstrip("，,、；;\u060c\u061b" + (extra or ""))
