"""ffmpeg 定位与通用调用封装。

优先用系统 PATH 里的 ffmpeg；找不到就退回 imageio-ffmpeg 自带的二进制。
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
    # ffmpeg 同目录下常常有 ffprobe
    try:
        p = Path(ffmpeg_bin()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if p.exists():
            return str(p)
    except Exception:
        pass
    return None


def run(args: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """执行 ffmpeg 命令（args 不含 ffmpeg 本身）。"""
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
    """音频文件能不能正常解码（只试解开头 1 秒）。"""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
                           "-t", "1", "-f", "null", "-"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    return proc.returncode == 0


def probe_duration(path: str | Path) -> float:
    """返回媒体时长（秒），失败返回 0。"""
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
    # 退回解析 ffmpeg 的 stderr
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


# ---- 编码器（显卡加速）----------------------------------------------------
# 独显和 CPU 内置的核显都有专门的视频编码芯片。用它编码本身不一定比 CPU 快，
# 但能把 CPU 让出来给画面渲染（真正的瓶颈），并行渲染时差别明显；笔记本还更省电。
HW_ENCODERS = {
    "nvenc": "h264_nvenc",      # NVIDIA
    "qsv": "h264_qsv",          # Intel 核显 Quick Sync
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
    """imageio-ffmpeg 自带的那个 ffmpeg（打包版里就是它）。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and Path(exe).exists() else None
    except Exception:
        return None


def _ffmpeg_candidates() -> List[str]:
    """显卡编码可以用的 ffmpeg：先用平时那个（系统装的优先），不行再试自带的。

    系统装的 ffmpeg 可能比显卡驱动新：比如 ffmpeg 7.1.1 的 NVENC 要 NVIDIA 570 以上的驱动，
    驱动是 560 时就用不了显卡编码；自带的那个要求低，照样能用。"""
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
    """真的编一小段试试，返回 (用哪个 ffmpeg, 能用的参数)；这个编码器用不了就返回 None。

    显卡在支持列表里不代表能用（没装驱动、驱动太老、虚拟机里没有显卡……）。
    画质参数的名字还跟驱动版本有关（AMD 的 AMF 尤其），所以带参数编不动时，
    再用编码器自己的默认参数试一次 —— 总比白白退回 CPU 强。
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
    """用这个编码器时该调哪个 ffmpeg（见 _ffmpeg_candidates）。"""
    p = _probe(name)
    return p[0] if p else ffmpeg_bin()


@lru_cache(maxsize=4)
def pick_encoder(pref: str = "auto") -> str:
    """按设置挑编码器：auto 找一个能用的显卡编码器，找不到就用 CPU。"""
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
    """这个编码器实际能用的参数（探测时验证过的）。"""
    args = _probe_args(name)
    if args is None:
        return list(ENCODER_ARGS.get(name, ENCODER_ARGS["libx264"]))
    return list(args)


_available_cache: dict = {"ts": 0.0, "value": None}


def available() -> dict:
    """ffmpeg 能不能用。结果缓存 60 秒：扩展弹窗开着时每 2 秒查一次健康状态，不必每次都起一个 ffmpeg 进程。"""
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
    """Windows 路径放进 filter 参数（如 subtitles=）时需要转义。"""
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p
