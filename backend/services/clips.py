""""Video" steps: videos embedded in a PPT deck, or video files inserted into a tutorial.

- On PPT import, the videos on each slide are extracted with their position on the page and the trim points set in PowerPoint;
  files linked from the author's computer and online videos can't be retrieved and are marked for the user to upload.
- When rendering, ffmpeg decodes the frames, which are placed at the video's spot on the slide (or full screen), with subtitles drawn as usual;
  the sound is the video's own (trimmed), or the video is muted and the step's narration is voiced instead.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from PIL import Image

from .. import i18n, storage
from ..models import Step, VideoClip
from .ffmpeg_util import CREATE_NO_WINDOW, ffmpeg_bin

VIDEO_EXT = {".mp4", ".m4v", ".mov", ".wmv", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".asf", ".flv", ".3gp"}
MAX_BYTES = 4 * 1024 ** 3
_P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"


class ClipError(RuntimeError):
    pass


def media_dir(pid: str) -> Path:
    return storage.project_dir(pid) / "media"


def resolve(pid: str, clip: Optional[VideoClip]) -> Optional[Path]:
    """Absolute path of the video file; None if there is none or it went missing (rendering falls back to the poster)."""
    if not clip or not clip.file:
        return None
    base = media_dir(pid).resolve()
    p = (base / clip.file).resolve()
    return p if p.is_relative_to(base) and p.is_file() else None


# ---- probing ----------------------------------------------------------------

_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_LINE = re.compile(r"Stream #\S+.*?: Video: (.*)")
_SIZE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_SAR = re.compile(r"SAR (\d+):(\d+)")
_ROT = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|\brotate\s*:\s*(-?\d+)")
_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_probe_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
_probe_lock = threading.Lock()


def probe(path: Path) -> Dict[str, Any]:
    """Duration, whether there is sound, display size (corrected for rotation and non-square pixels). Uses ffmpeg only (the package has no ffprobe).

    The same file (path + size + mtime unchanged) is probed only once: the editor preview and every render segment need it."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _probe_lock:
        hit = _probe_cache.get(key)
    if hit is not None:
        return dict(hit)
    info = _probe(path)
    with _probe_lock:
        if len(_probe_cache) > 256:
            _probe_cache.clear()
        _probe_cache[key] = info
    return dict(info)


def _probe(path: Path) -> Dict[str, Any]:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    err = (proc.stderr or b"").decode("utf-8", "ignore")
    # the first real video stream (skips cover images in mp3 / m4a)
    lines = [m.group(1) for m in _VIDEO_LINE.finditer(err)]
    line = next((x for x in lines if "attached pic" not in x), None)
    size = _SIZE.search(line) if line else None
    if not size:
        raise ClipError(i18n.t("读不了这个视频文件：可能已损坏，或者不是视频格式"))
    w, h = int(size.group(1)), int(size.group(2))
    sar = _SAR.search(line)
    if sar and int(sar.group(1)) > 0 and int(sar.group(2)) > 0 and sar.group(1) != sar.group(2):
        w = max(2, round(w * int(sar.group(1)) / int(sar.group(2))))     # non-square pixels (old DV, some MPEG)
    rot = _ROT.search(err)
    if rot and round(abs(float(rot.group(1) or rot.group(2)))) % 180 == 90:
        w, h = h, w                   # shot in portrait on a phone: ffmpeg rotates it when decoding, so swap the size too
    m = _DUR.search(err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    if dur <= 0:
        dur = _scan_duration(path)
    return {"duration": round(dur, 3), "has_audio": bool(re.search(r"Stream #\S+.*?: Audio:", err)),
            "width": w, "height": h}


def _scan_duration(path: Path) -> float:
    """No duration in the file header (common for webm recorded by browsers / screen recorders): compute it by reading the packets once, without decoding."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0",
                           "-c", "copy", "-f", "null", "-"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    times = _TIME.findall((proc.stderr or b"").decode("utf-8", "ignore"))
    if not times:
        return 0.0
    hh, mm, ss = times[-1]
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def clip_range(c: VideoClip) -> Tuple[float, float]:
    """The [start, end) actually played, clamped to the video length."""
    total = c.duration if c.duration > 0 else 0.0
    end = c.end if 0 < c.end and (not total or c.end <= total) else total
    start = min(max(0.0, c.start), max(0.0, end - 0.1)) if end else max(0.0, c.start)
    return start, end


def clip_length(c: VideoClip) -> float:
    start, end = clip_range(c)
    return max(0.1, end - start)


# ---- extract videos on PPT import ---------------------------------------------

def pptx_videos(pptx_path: Path, out_dir: Path) -> Dict[int, List[Dict[str, Any]]]:
    """Videos per slide: {slide number: [{file, source, rect, start, end, missing, duration, has_audio}]}.

    Embedded video files are written to out_dir; linked local files (linked) and online videos (online) only record the address.
    """
    from pptx import Presentation
    from pptx.oxml.ns import qn

    prs = Presentation(str(pptx_path))
    sw, sh = float(prs.slide_width or 1), float(prs.slide_height or 1)
    out: Dict[int, List[Dict[str, Any]]] = {}
    for i, slide in enumerate(prs.slides, 1):
        found = []
        for el, (x, y, w, h) in _pictures(slide.shapes):
            vf = el.find(".//" + qn("a:videoFile"))
            if vf is None:
                continue                                   # images, audio and other things
            media = el.find(f".//{{{_P14}}}media")
            info: Dict[str, Any] = {
                "file": "", "missing": "", "duration": 0.0, "has_audio": True,
                "rect": {"x": x / sw, "y": y / sh, "w": w / sw, "h": h / sh},
                "start": 0.0, "end_trim": 0.0,
                "source": "",
            }
            cnv = el.find(".//" + qn("p:cNvPr"))
            if cnv is not None:
                info["source"] = cnv.get("name") or ""          # name shown in PowerPoint, usually the original file name
            trim = media.find(f"{{{_P14}}}trim") if media is not None else None
            if trim is not None:
                # PowerPoint trim: st = milliseconds cut from the start, end = milliseconds cut from the end
                info["start"] = float(trim.get("st") or 0) / 1000
                info["end_trim"] = float(trim.get("end") or 0) / 1000
            rel = None
            for rid in ((media.get(qn("r:embed")) if media is not None else None), vf.get(qn("r:link"))):
                if rid and rid in slide.part.rels:
                    rel = slide.part.rels[rid]
                    if not rel.is_external:
                        break
            if rel is None:
                info["missing"] = "linked"
            elif rel.is_external:
                target = rel.target_ref or ""
                info["missing"] = "online" if target.lower().startswith(("http://", "https://")) else "linked"
                info["source"] = target
            else:
                ext = Path(str(rel.target_part.partname)).suffix.lower() or ".mp4"
                name = f"video_{i:03d}_{len(found) + 1}{ext}"
                (out_dir / name).write_bytes(rel.target_part.blob)
                info["file"] = name
                try:
                    p = probe(out_dir / name)
                    info.update(duration=p["duration"], has_audio=p["has_audio"])
                except ClipError:
                    info["missing"] = "broken"
            found.append(info)
        if found:
            out[i] = found
    return out


def _pictures(shapes, tf: Callable[[float, float, float, float], Tuple[float, float, float, float]] = None):
    """Walk all picture shapes (including those in groups) and convert coordinates to the page."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    tf = tf or (lambda x, y, w, h: (x, y, w, h))
    for sh in shapes:
        try:
            kind = sh.shape_type
        except Exception:
            kind = None
        if kind == MSO_SHAPE_TYPE.GROUP:
            xfrm = sh._element.grpSpPr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
            try:
                off, ext = xfrm.find("{*}off"), xfrm.find("{*}ext")
                choff, chext = xfrm.find("{*}chOff"), xfrm.find("{*}chExt")
                gx, gy, gw, gh = (float(off.get("x")), float(off.get("y")),
                                  float(ext.get("cx")), float(ext.get("cy")))
                cx, cy = float(choff.get("x")), float(choff.get("y"))
                sx = gw / max(1.0, float(chext.get("cx")))
                sy = gh / max(1.0, float(chext.get("cy")))
            except Exception:
                gx = gy = cx = cy = 0.0
                sx = sy = 1.0

            def child_tf(x, y, w, h, gx=gx, gy=gy, cx=cx, cy=cy, sx=sx, sy=sy, parent=tf):
                return parent(gx + (x - cx) * sx, gy + (y - cy) * sy, w * sx, h * sy)
            yield from _pictures(sh.shapes, child_tf)
        elif sh._element.tag.endswith("}pic"):
            try:
                yield sh._element, tf(float(sh.left or 0), float(sh.top or 0),
                                      float(sh.width or 0), float(sh.height or 0))
            except Exception:
                continue


def clip_from_import(info: Dict[str, Any]) -> VideoClip:
    """Import record -> VideoClip (without file; the caller fills it in after copying the file into the project)."""
    from ..models import Rect
    dur = float(info.get("duration") or 0)
    end = max(0.0, dur - float(info.get("end_trim") or 0)) if dur and info.get("end_trim") else 0.0
    return VideoClip(source=info.get("source") or "", duration=dur, has_audio=bool(info.get("has_audio", True)),
                     start=float(info.get("start") or 0), end=end, rect=Rect(**info["rect"]) if info.get("rect") else None,
                     missing=info.get("missing") or "", mode="inset")


# ---- put video files into the project -------------------------------------------

def store(pid: str, step: Step, src: Path, original_name: str) -> VideoClip:
    """Put a video file into the project's media/ folder and update the step's clip (position / mode etc. are kept)."""
    ext = Path(original_name).suffix.lower()
    if ext not in VIDEO_EXT:
        raise ClipError(i18n.t("不支持的视频格式：{ext}（支持 mp4 / mov / wmv / avi / mkv / webm 等）", ext=ext or "?"))
    if src.stat().st_size > MAX_BYTES:
        raise ClipError(i18n.t("视频文件太大（最多 {gb} GB）", gb=MAX_BYTES // 1024 ** 3))
    info = probe(src)
    d = media_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    # every new video gets a new file name so the editor's player and thumbnails don't keep showing the old one (browsers cache by URL)
    name = f"{step.id}_{uuid.uuid4().hex[:8]}{ext}"
    # the uploaded temp file is deleted after use, so move it (a rename on the same drive; even a multi-GB video isn't copied again)
    shutil.move(str(src), str(d / name))
    old = step.clip.file if step.clip else ""
    clip = step.clip.model_copy() if step.clip else VideoClip(mode="fullscreen")
    clip.file, clip.source = name, original_name
    clip.duration, clip.has_audio, clip.missing = info["duration"], info["has_audio"], ""
    clip.start, clip.end = 0.0, 0.0
    clip.transcript, clip.words = "", []          # new video: previously recognised speech times no longer match
    if old and old != name:
        (d / old).unlink(missing_ok=True)
    return clip


def link_or_copy(src: Path, dst: Path) -> None:
    """Hard link on the same drive (instant, no second copy); copy across drives or when unsupported."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def save_poster(pid: str, clip: VideoClip) -> Optional[Tuple[str, int, int]]:
    """An inserted video isn't on any slide and has no base image: grab one frame as this step's screenshot.
    Used by the step list thumbnail, "poster only" and document export. Returns (file name, width, height)."""
    src = resolve(pid, clip)
    if src is None:
        return None
    try:
        info = probe(src)
    except ClipError:
        return None
    k = min(1.0, 1920 / max(1, info["width"]), 1080 / max(1, info["height"]))
    w = max(2, int(info["width"] * k) // 2 * 2)
    h = max(2, int(info["height"] * k) // 2 * 2)
    start = clip_range(clip)[0]
    img = grab_frame(src, start + min(1.0, clip_length(clip) * 0.1), w, h) or grab_frame(src, start, w, h)
    if img is None:
        return None
    name = Path(clip.file).stem + ".jpg"
    shots = storage.screenshots_dir(pid)
    shots.mkdir(parents=True, exist_ok=True)
    img.save(shots / name, "JPEG", quality=88)
    return name, w, h


def use_poster(pid: str, step: Step, poster: Optional[Tuple[str, int, int]]) -> None:
    """Set the result of save_poster as this step's screenshot (the old poster is deleted)."""
    if not poster:
        return
    name, w, h = poster
    old = step.screenshot
    step.screenshot, step.img_w, step.img_h = name, w, h
    step.viewport_w, step.viewport_h = w, h
    if old and old != name:
        (storage.screenshots_dir(pid) / old).unlink(missing_ok=True)


def clip_words(c: VideoClip) -> List[Dict[str, Any]]:
    """Convert recognised word times (from the video start) to times from the trim start, for subtitle alignment.
    Words outside the trimmed range get negative times or times beyond the step, so their subtitles never show."""
    start = clip_range(c)[0]
    return [{**w, "t": float(w.get("t", 0.0)) - start} for w in c.words]


# ---- rendering -----------------------------------------------------------------

def extract_audio(src: Path, clip: VideoClip, out_wav: Path) -> Optional[Path]:
    """The trimmed original sound (48k stereo wav); None if the video has no sound."""
    if not clip.has_audio:
        return None
    start, _end = clip_range(clip)
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src),
         "-t", f"{clip_length(clip):.3f}", "-vn", "-ac", "2", "-ar", "48000", str(out_wav)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 1000:
        return None
    return out_wav


def placement(clip: VideoClip, draw_box: Optional[Tuple[int, int, int, int]], has_slide: bool,
              W: int, H: int, vw: int, vh: int) -> Tuple[Tuple[int, int, int, int], bool]:
    """Where the video goes in the frame ((x, y, w, h), whether it sits on the slide).
    In place = the video's box on the slide; full screen (or no position / no slide image) = scaled as large as possible, centered."""
    if clip.mode == "inset" and clip.rect and has_slide and draw_box:
        x0, y0, x1, y1 = draw_box
        r = clip.rect
        x, y = x0 + r.x * (x1 - x0), y0 + r.y * (y1 - y0)
        w, h = r.w * (x1 - x0), r.h * (y1 - y0)
        if w >= 8 and h >= 8:
            return (int(round(x)), int(round(y)), int(round(w)) // 2 * 2, int(round(h)) // 2 * 2), True
    s = min(W / max(1, vw), H / max(1, vh))
    w, h = int(vw * s) // 2 * 2, int(vh * s) // 2 * 2
    return ((W - w) // 2, (H - h) // 2, w, h), False


def frames(src: Path, clip: VideoClip, base: Image.Image, box: Tuple[int, int, int, int], fps: int,
           n: int, text_at: Callable[[float], str],
           draw: Callable[[Image.Image, str], Image.Image]) -> Iterator[bytes]:
    """Yield rgb24 frames: ffmpeg decodes the video, which is placed on base (the slide or black), then the subtitle is drawn.
    If the video is shorter than the step (e.g. a longer narration after muting), the last frame is held."""
    x, y, w, h = box
    start, _end = clip_range(clip)
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src),
           "-t", f"{clip_length(clip):.3f}", "-an", "-vf", f"scale={w}:{h},fps={fps},format=rgb24",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW)
    size = w * h * 3
    last: Optional[Image.Image] = None
    ended = False
    frozen_text, frozen_buf = None, None
    try:
        for i in range(n):
            t = i / fps
            text = text_at(t)
            if not ended:
                raw = proc.stdout.read(size)
                if len(raw) == size:
                    last = Image.frombuffer("RGB", (w, h), raw, "raw", "RGB", 0, 1)
                else:
                    ended = True
            if ended and frozen_buf is not None and text == frozen_text:
                yield frozen_buf                     # the video has ended and the subtitle hasn't changed: identical frame, reuse it
                continue
            img = base.copy()
            if last is not None:
                img.paste(last, (x, y))
            if text:
                img = draw(img, text)
            buf = img.tobytes()
            if ended:
                frozen_text, frozen_buf = text, buf
            yield buf
    finally:
        # when the user clicks Stop or this segment's encode fails, the decoding ffmpeg must end too — no leftover processes
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()


def grab_frame(src: Path, t: float, w: int, h: int) -> Optional[Image.Image]:
    """Grab the frame at t seconds (editor preview), scaled to w×h."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout or b""
    if len(raw) < w * h * 3:
        return None
    return Image.frombuffer("RGB", (w, h), raw[:w * h * 3], "raw", "RGB", 0, 1)
