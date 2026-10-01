"""Locating ffmpeg and common call wrappers.

ffmpeg from the system PATH is preferred; otherwise fall back to the binary bundled with imageio-ffmpeg.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from .. import i18n
from typing import List, Optional, Tuple

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise FFmpegError(i18n.t(
            "未找到 ffmpeg。请安装后重试：winget install Gyan.FFmpeg\n或执行 pip install imageio-ffmpeg 使用内置版本。原因：{error}",
            error=e))


@lru_cache(maxsize=1)
def ffprobe_bin() -> Optional[str]:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    # ffprobe is often in the same folder as ffmpeg
    try:
        p = Path(ffmpeg_bin()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if p.exists():
            return str(p)
    except Exception:
        pass
    return None


def run(args: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run an ffmpeg command (args without ffmpeg itself)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + args
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        creationflags=CREATE_NO_WINDOW,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore")[-1500:]
        raise FFmpegError(i18n.t("ffmpeg 执行失败（{code}）：", code=proc.returncode)
                          + f"\n{err}\ncmd: {' '.join(cmd[:14])} ...")
    return proc


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def audio_readable(path: str | Path) -> bool:
    """Whether an audio file decodes properly (only the first second is tried)."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
                           "-t", "1", "-f", "null", "-"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    return proc.returncode == 0


def probe_duration(path: str | Path) -> float:
    """Media duration in seconds; 0 on failure."""
    path = str(path)
    if not os.path.exists(path):
        return 0.0
    probe = ffprobe_bin()
    if probe:
        try:
            proc = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
            txt = (proc.stdout or b"").decode("utf-8", "ignore").strip()
            if txt:
                return float(txt)
        except Exception:
            pass
    # fall back to parsing ffmpeg's stderr
    try:
        proc = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        m = _DUR_RE.search(err)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


# ---- encoders (GPU acceleration) ----------------------------------------------------
# Dedicated and integrated GPUs both have video encoding hardware. Encoding with it isn't necessarily faster than the CPU,
# but it frees the CPU for drawing frames (the real bottleneck), which matters with parallel rendering; laptops also save power.
HW_ENCODERS = {
    "nvenc": "h264_nvenc",      # NVIDIA
    "qsv": "h264_qsv",          # Intel integrated graphics Quick Sync
    "amf": "h264_amf",          # AMD
}
ENCODER_ARGS = {
    "libx264": ["-preset", "veryfast", "-crf", "20"],
    "h264_nvenc": ["-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "0"],
    "h264_qsv": ["-preset", "medium", "-global_quality", "24"],
    "h264_amf": ["-quality", "balanced", "-rc", "cqp", "-qp_i", "24", "-qp_p", "24"],
}


@lru_cache(maxsize=1)
def _bundled_ffmpeg() -> Optional[str]:
    """The ffmpeg bundled with imageio-ffmpeg (the one in the packaged app)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and Path(exe).exists() else None
    except Exception:
        return None


def _ffmpeg_candidates() -> List[str]:
    """ffmpeg binaries usable for GPU encoding: the usual one first (system-installed preferred), then the bundled one.

    A system ffmpeg may be newer than the GPU driver: e.g. NVENC in ffmpeg 7.1.1 needs NVIDIA driver 570 or later,
    so with driver 560 GPU encoding fails; the bundled one has lower requirements and still works."""
    out = [ffmpeg_bin()]
    b = _bundled_ffmpeg()
    if b and os.path.normcase(os.path.abspath(b)) != os.path.normcase(os.path.abspath(out[0])):
        out.append(b)
    return out


@lru_cache(maxsize=4)
def _listed_encoders(exe: Optional[str] = None) -> frozenset:
    try:
        proc = subprocess.run([exe or ffmpeg_bin(), "-hide_banner", "-encoders"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=CREATE_NO_WINDOW)
        out = (proc.stdout or b"").decode("utf-8", "ignore")
        return frozenset(re.findall(r"^\s*V\S*\s+(\S+)", out, re.M))
    except Exception:
        return frozenset()


def _try_encode(name: str, args: List[str], exe: Optional[str] = None) -> bool:
    try:
        proc = subprocess.run(
            [exe or ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=15:d=0.4",
             "-c:v", name] + args + ["-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=CREATE_NO_WINDOW)
        return proc.returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=8)
def _probe(name: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Really encode a short clip; returns (which ffmpeg, working arguments), or None if this encoder can't be used.

    Being in the supported list doesn't mean it works (no driver, an old driver, no GPU in a virtual machine …).
    Quality parameter names also depend on the driver version (AMD's AMF in particular), so if encoding with parameters fails,
    try once more with the encoder's own defaults — better than falling back to the CPU for nothing.
    """
    if name == "libx264":
        return ffmpeg_bin(), tuple(ENCODER_ARGS["libx264"])
    for exe in _ffmpeg_candidates():
        if name not in _listed_encoders(exe):
            continue
        for args in (ENCODER_ARGS.get(name, []), []):
            if _try_encode(name, list(args), exe):
                return exe, tuple(args)
    return None


def _probe_args(name: str) -> Optional[Tuple[str, ...]]:
    p = _probe(name)
    return p[1] if p else None


def encoder_works(name: str) -> bool:
    return _probe(name) is not None


def encoder_bin(name: str) -> str:
    """Which ffmpeg to call for this encoder (see _ffmpeg_candidates)."""
    p = _probe(name)
    return p[0] if p else ffmpeg_bin()


@lru_cache(maxsize=4)
def pick_encoder(pref: str = "auto") -> str:
    """Pick the encoder from the settings: auto finds a working GPU encoder, otherwise the CPU."""
    pref = (pref or "auto").strip().lower()
    if pref in ("cpu", "x264", "libx264", "software"):
        return "libx264"
    if pref in HW_ENCODERS:
        return HW_ENCODERS[pref] if encoder_works(HW_ENCODERS[pref]) else "libx264"
    if pref in ENCODER_ARGS and pref != "libx264":
        return pref if encoder_works(pref) else "libx264"
    for name in HW_ENCODERS.values():
        if encoder_works(name):
            return name
    return "libx264"


def encoder_args(name: str) -> List[str]:
    """The arguments that actually work for this encoder (verified while probing)."""
    args = _probe_args(name)
    if args is None:
        return list(ENCODER_ARGS.get(name, ENCODER_ARGS["libx264"]))
    return list(args)


_available_cache: dict = {"ts": 0.0, "value": None}


def available() -> dict:
    """Whether ffmpeg is usable. Cached for 60 seconds: with the extension popup open, health is checked every 2 seconds, no need to start ffmpeg each time."""
    now = time.time()
    if _available_cache["value"] is not None and now - _available_cache["ts"] < 60:
        return dict(_available_cache["value"])
    try:
        exe = ffmpeg_bin()
        proc = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        ver = (proc.stdout or b"").decode("utf-8", "ignore").splitlines()[:1]
        value = {"ok": True, "path": exe, "version": ver[0] if ver else ""}
    except Exception as e:
        value = {"ok": False, "path": "", "version": "", "error": str(e)}
    _available_cache.update(ts=now, value=value)
    return dict(value)


def escape_filter_path(path: str | Path) -> str:
    """Windows paths need escaping inside filter arguments (e.g. subtitles=)."""
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p
