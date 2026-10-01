"""「视频」步骤：PPT 里嵌入的视频，或者自己插进教程里的视频文件。

- 导入 PPT 时把每页里的视频取出来，记下它在页面上的位置和 PowerPoint 里设的剪辑起止点；
  链接到作者电脑上的文件、在线视频（YouTube 等）取不到，标记出来让用户手动上传。
- 渲染时由 ffmpeg 解码出画面，贴到幻灯片原来的位置（或者全屏），字幕照常画；
  声音用视频原声（剪好起止点），或者静音后配这一步的解说。
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
    """视频文件的绝对路径；没有或者文件丢了返回 None（渲染时退回只显示封面）。"""
    if not clip or not clip.file:
        return None
    base = media_dir(pid).resolve()
    p = (base / clip.file).resolve()
    return p if p.is_relative_to(base) and p.is_file() else None


# ---- 探测 -------------------------------------------------------------------

_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_LINE = re.compile(r"Stream #\S+.*?: Video: (.*)")
_SIZE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_SAR = re.compile(r"SAR (\d+):(\d+)")
_ROT = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|\brotate\s*:\s*(-?\d+)")
_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_probe_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
_probe_lock = threading.Lock()


def probe(path: Path) -> Dict[str, Any]:
    """时长、有没有声音、画面显示尺寸（已按旋转、非方形像素换算）。只用 ffmpeg（打包版没有 ffprobe）。

    同一个文件（路径 + 大小 + 修改时间不变）只探测一次：编辑器预览、渲染每一段都要用。"""
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
    # 第一路真正的视频流（跳过 mp3 / m4a 里当封面的图片）
    lines = [m.group(1) for m in _VIDEO_LINE.finditer(err)]
    line = next((x for x in lines if "attached pic" not in x), None)
    size = _SIZE.search(line) if line else None
    if not size:
        raise ClipError(i18n.t("读不了这个视频文件：可能已损坏，或者不是视频格式"))
    w, h = int(size.group(1)), int(size.group(2))
    sar = _SAR.search(line)
    if sar and int(sar.group(1)) > 0 and int(sar.group(2)) > 0 and sar.group(1) != sar.group(2):
        w = max(2, round(w * int(sar.group(1)) / int(sar.group(2))))     # 非方形像素（老 DV、部分 MPEG）
    rot = _ROT.search(err)
    if rot and round(abs(float(rot.group(1) or rot.group(2)))) % 180 == 90:
        w, h = h, w                   # 手机竖着拍的：ffmpeg 解码时会自动转正，尺寸也跟着对调
    m = _DUR.search(err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    if dur <= 0:
        dur = _scan_duration(path)
    return {"duration": round(dur, 3), "has_audio": bool(re.search(r"Stream #\S+.*?: Audio:", err)),
            "width": w, "height": h}


def _scan_duration(path: Path) -> float:
    """文件头里没写时长（浏览器 / 录屏软件直接录的 webm 常这样）：不解码、只读一遍数据包算出来。"""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0",
                           "-c", "copy", "-f", "null", "-"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    times = _TIME.findall((proc.stderr or b"").decode("utf-8", "ignore"))
    if not times:
        return 0.0
    hh, mm, ss = times[-1]
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def clip_range(c: VideoClip) -> Tuple[float, float]:
    """实际播放的 [起点, 终点)，已经夹在视频长度以内。"""
    total = c.duration if c.duration > 0 else 0.0
    end = c.end if 0 < c.end and (not total or c.end <= total) else total
    start = min(max(0.0, c.start), max(0.0, end - 0.1)) if end else max(0.0, c.start)
    return start, end


def clip_length(c: VideoClip) -> float:
    start, end = clip_range(c)
    return max(0.1, end - start)


# ---- 导入 PPT 时取出视频 ------------------------------------------------------

def pptx_videos(pptx_path: Path, out_dir: Path) -> Dict[int, List[Dict[str, Any]]]:
    """每页里的视频：{页码: [{file, source, rect, start, end, missing, duration, has_audio}]}。

    嵌入的视频文件写到 out_dir；链接到本机文件的（linked）和在线视频（online）只记下地址。
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
                continue                                   # 图片、音频等别的东西
            media = el.find(f".//{{{_P14}}}media")
            info: Dict[str, Any] = {
                "file": "", "missing": "", "duration": 0.0, "has_audio": True,
                "rect": {"x": x / sw, "y": y / sh, "w": w / sw, "h": h / sh},
                "start": 0.0, "end_trim": 0.0,
                "source": "",
            }
            cnv = el.find(".//" + qn("p:cNvPr"))
            if cnv is not None:
                info["source"] = cnv.get("name") or ""          # PowerPoint 里显示的名字，通常就是原文件名
            trim = media.find(f"{{{_P14}}}trim") if media is not None else None
            if trim is not None:
                # PowerPoint 的剪辑：st = 从头剪掉多少毫秒，end = 从尾剪掉多少毫秒
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
    """遍历所有图片形状（含组合里的），坐标换算到页面上。"""
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
    """导入记录 -> VideoClip（还没有 file，调用方把文件拷进项目后再填）。"""
    from ..models import Rect
    dur = float(info.get("duration") or 0)
    end = max(0.0, dur - float(info.get("end_trim") or 0)) if dur and info.get("end_trim") else 0.0
    return VideoClip(source=info.get("source") or "", duration=dur, has_audio=bool(info.get("has_audio", True)),
                     start=float(info.get("start") or 0), end=end, rect=Rect(**info["rect"]) if info.get("rect") else None,
                     missing=info.get("missing") or "", mode="inset")


# ---- 往项目里放视频文件 --------------------------------------------------------

def store(pid: str, step: Step, src: Path, original_name: str) -> VideoClip:
    """把视频文件放进项目的 media/ 目录，更新这一步的 clip（保留原来的位置 / 模式等设置）。"""
    ext = Path(original_name).suffix.lower()
    if ext not in VIDEO_EXT:
        raise ClipError(i18n.t("不支持的视频格式：{ext}（支持 mp4 / mov / wmv / avi / mkv / webm 等）", ext=ext or "?"))
    if src.stat().st_size > MAX_BYTES:
        raise ClipError(i18n.t("视频文件太大（最多 {gb} GB）", gb=MAX_BYTES // 1024 ** 3))
    info = probe(src)
    d = media_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    # 每次换视频都用新文件名：编辑器里的播放器、缩略图不会还显示旧的（浏览器按地址缓存）
    name = f"{step.id}_{uuid.uuid4().hex[:8]}{ext}"
    # 上传的临时文件用完就删，直接挪过去（同一个盘上是改名，几个 GB 的视频也不用再拷一遍）
    shutil.move(str(src), str(d / name))
    old = step.clip.file if step.clip else ""
    clip = step.clip.model_copy() if step.clip else VideoClip(mode="fullscreen")
    clip.file, clip.source = name, original_name
    clip.duration, clip.has_audio, clip.missing = info["duration"], info["has_audio"], ""
    clip.start, clip.end = 0.0, 0.0
    clip.transcript, clip.words = "", []          # 换了视频，之前识别的讲话时间对不上了
    if old and old != name:
        (d / old).unlink(missing_ok=True)
    return clip


def link_or_copy(src: Path, dst: Path) -> None:
    """同一个盘上用硬链接（瞬间完成、不占第二份空间），跨盘或不支持时再复制。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def save_poster(pid: str, clip: VideoClip) -> Optional[Tuple[str, int, int]]:
    """插入的视频不在哪页幻灯片上，没有底图：取一帧存成这一步的截图。
    步骤列表的缩略图、「只显示封面」、导出文档都用它。返回 (文件名, 宽, 高)。"""
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
    """把 save_poster 的结果设成这一步的截图（旧的封面图删掉）。"""
    if not poster:
        return
    name, w, h = poster
    old = step.screenshot
    step.screenshot, step.img_w, step.img_h = name, w, h
    step.viewport_w, step.viewport_h = w, h
    if old and old != name:
        (storage.screenshots_dir(pid) / old).unlink(missing_ok=True)


def clip_words(c: VideoClip) -> List[Dict[str, Any]]:
    """识别出的逐词时间（从视频开头算）换成从截取起点算，给字幕对齐用。
    截取范围以外的词时间是负的或超出这一步，对应的字幕不会显示。"""
    start = clip_range(c)[0]
    return [{**w, "t": float(w.get("t", 0.0)) - start} for w in c.words]


# ---- 渲染 -------------------------------------------------------------------

def extract_audio(src: Path, clip: VideoClip, out_wav: Path) -> Optional[Path]:
    """剪好起止点的原声（48k 立体声 wav）；视频没有声音返回 None。"""
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
    """视频画面在成片里的位置 ((x, y, w, h), 是否贴在幻灯片上)。
    原位置 = 幻灯片上视频框对应的地方；全屏（或者没有位置信息、没有幻灯片底图）= 按比例放到最大、居中。"""
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
    """逐帧产出 rgb24：ffmpeg 解码出视频画面，贴到 base（幻灯片页面或黑底）上，再画字幕。
    视频比这一步短（比如静音后配的解说更长）就停在最后一帧。"""
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
                yield frozen_buf                     # 视频放完了、字幕也没变：画面一样，直接复用
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
        # 用户点了停止、或者这一段编码出错时，解码的 ffmpeg 也要跟着结束，不留进程
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
    """取 t 秒处的一帧（编辑器预览用），缩放到 w×h。"""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout or b""
    if len(raw) < w * h * 3:
        return None
    return Image.frombuffer("RGB", (w, h), raw[:w * h * 3], "raw", "RGB", 0, 1)
