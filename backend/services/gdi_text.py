"""Draw "complex scripts" with Windows' own text engine (GDI + Uniscribe): Arabic, Hebrew, Hindi, Thai …

Pillow on Windows has no HarfBuzz / FriBiDi and draws these wrong: Arabic letters don't join, brackets face the wrong way,
Hindi vowel signs end up after the consonant, ligatures fall apart, Thai tone marks pile up. Windows' text engine handles all of this
and exists on every Windows computer, with nothing extra to install.

Only used for these scripts; Chinese, English, Japanese etc. still use Pillow and look as before.
The result is a grayscale mask (white text = 255) that the caller composites onto the frame with color and outline.
"""
from __future__ import annotations

import ctypes
import os
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

_lock = threading.Lock()
AVAILABLE = False

if os.name == "nt":
    try:
        from ctypes import wintypes as W

        _gdi32 = ctypes.WinDLL("gdi32")
        _user32 = ctypes.WinDLL("user32")

        class _BMIH(ctypes.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                        ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                        ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                        ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

        _gdi32.CreateFontW.restype = W.HFONT
        _gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR]
        _gdi32.CreateCompatibleDC.restype = W.HDC
        _gdi32.CreateCompatibleDC.argtypes = [W.HDC]
        _gdi32.SelectObject.restype = W.HGDIOBJ
        _gdi32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
        _gdi32.CreateDIBSection.restype = W.HBITMAP
        _gdi32.CreateDIBSection.argtypes = [W.HDC, ctypes.c_void_p, W.UINT,
                                            ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD]
        _gdi32.DeleteObject.argtypes = [W.HGDIOBJ]
        _gdi32.DeleteDC.argtypes = [W.HDC]
        _gdi32.SetTextColor.argtypes = [W.HDC, W.COLORREF]
        _gdi32.SetBkMode.argtypes = [W.HDC, ctypes.c_int]
        _gdi32.AddFontResourceExW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p]
        _user32.DrawTextW.argtypes = [W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.RECT), W.UINT]
        AVAILABLE = True
    except Exception:
        AVAILABLE = False

_DT_SINGLELINE, _DT_NOPREFIX, _DT_CALCRECT, _DT_NOCLIP, _DT_RTLREADING = 0x20, 0x800, 0x400, 0x100, 0x20000
_ANTIALIASED_QUALITY, _DEFAULT_CHARSET, _FR_PRIVATE = 4, 1, 0x10
_WIN_FONTS = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts").lower()


@lru_cache(maxsize=64)
def _face(path: str) -> Optional[Tuple[str, int]]:
    """Font file -> (GDI font family name, weight). Fonts outside the system font folder are registered privately first."""
    try:
        from fontTools.ttLib import TTCollection, TTFont
        if path.lower().endswith((".ttc", ".otc")):
            font = TTCollection(path, lazy=True).fonts[0]
        else:
            font = TTFont(path, lazy=True)
        family = font["name"].getDebugName(1) or ""
        weight = int(getattr(font["OS/2"], "usWeightClass", 400)) if "OS/2" in font else 400
    except Exception:
        return None
    if not family:
        return None
    if not str(Path(path).resolve()).lower().startswith(_WIN_FONTS):
        _gdi32.AddFontResourceExW(str(path), _FR_PRIVATE, None)   # visible to this process only, the system is not changed
    return family, (700 if weight >= 600 else 400)


def _rtl_paragraph(text: str) -> bool:
    """Paragraph direction follows the first character with a direction (Unicode bidi algorithm P2/P3)."""
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b == "L":
            return False
        if b in ("R", "AL"):
            return True
    return False


def _draw(text: str, family: str, weight: int, px: int, measure_only: bool):
    hdc = _gdi32.CreateCompatibleDC(None)
    font = _gdi32.CreateFontW(-px, 0, 0, 0, weight, 0, 0, 0, _DEFAULT_CHARSET, 4, 0,
                              _ANTIALIASED_QUALITY, 0, family)
    old_font = _gdi32.SelectObject(hdc, font)
    try:
        flags = _DT_SINGLELINE | _DT_NOPREFIX | (_DT_RTLREADING if _rtl_paragraph(text) else 0)
        r = W.RECT(0, 0, 0, 0)
        _user32.DrawTextW(hdc, text, -1, ctypes.byref(r), flags | _DT_CALCRECT)
        w, h = int(r.right), int(r.bottom)
        if measure_only:
            return w, h
        # margin all around: Arabic and Hindi marks extend beyond the layout box
        pad = max(4, px // 2)
        bw, bh = w + pad * 2, h + pad * 2
        bmi = _BMIH(ctypes.sizeof(_BMIH), bw, -bh, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        dib = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not dib:
            return None
        old_bmp = _gdi32.SelectObject(hdc, dib)
        try:
            ctypes.memset(bits, 0, bw * bh * 4)
            _gdi32.SetBkMode(hdc, 1)                     # TRANSPARENT
            _gdi32.SetTextColor(hdc, 0x00FFFFFF)
            box = W.RECT(pad, pad, pad + w, pad + h)
            _user32.DrawTextW(hdc, text, -1, ctypes.byref(box), flags | _DT_NOCLIP)
            _gdi32.GdiFlush()
            raw = ctypes.string_at(bits, bw * bh * 4)
        finally:
            _gdi32.SelectObject(hdc, old_bmp)
            _gdi32.DeleteObject(dib)
        mask = Image.frombuffer("RGBA", (bw, bh), raw, "raw", "BGRA", 0, 1).getchannel("G").copy()
        return mask, w, h, pad
    finally:
        _gdi32.SelectObject(hdc, old_font)
        _gdi32.DeleteObject(font)
        _gdi32.DeleteDC(hdc)


@lru_cache(maxsize=4096)
def measure(text: str, font_path: str, px: int) -> Optional[float]:
    """Laid-out width in pixels; None if GDI is unavailable (the caller measures with Pillow instead)."""
    face = _face(font_path) if AVAILABLE and font_path else None
    if not face or not text:
        return None
    with _lock:
        res = _draw(text, face[0], face[1], px, measure_only=True)
    return float(res[0]) if res else None


@lru_cache(maxsize=128)
def render(text: str, font_path: str, px: int) -> Optional[Tuple[Image.Image, int, int, int]]:
    """(mask, layout width, layout height, margin). A subtitle draws the same sentence for many frames, so the result is cached."""
    face = _face(font_path) if AVAILABLE and font_path else None
    if not face or not text:
        return None
    with _lock:
        return _draw(text, face[0], face[1], px, measure_only=False)
