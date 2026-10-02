"""How a slide with videos is presented (its "sequence").

A video imported from a deck is a "video" step right after its slide. The sequence on the slide step decides the order:

- ""             narration first (points revealed as they are discussed), then the slide's videos play in place (default)
- "video_first"  the slide's videos play first. Text lying over a video is hidden meanwhile; afterwards the frame holds the
                 video's last frame, the text over it appears item by item and the narration only explains that text.
                 Content not over a video is visible from the start.

Text "over a video" = a reveal item with at least OVER_VIDEO of its area inside the video's box on the slide.
Hiding text during the video needs the per-item images from a PowerPoint import (Step.reveal); without them the
slide image (with the text over the video's poster) is simply shown after the video.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

from ..models import Step

SEQUENCES = ("", "video_first")
OVER_VIDEO = 0.5


def is_video_first(step: Optional[Step]) -> bool:
    return bool(step is not None and step.kind == "slide" and step.sequence == "video_first")


def videos_after(steps: Sequence[Step], i: int) -> List[Step]:
    """The video steps directly following the slide at index i (they belong to that slide)."""
    out = []
    for s in steps[i + 1:]:
        if s.kind != "video":
            break
        out.append(s)
    return out


def render_order(steps: Sequence[Step]) -> Tuple[List[Step], Dict[str, Step]]:
    """Order in which the steps are rendered: the videos of a video-first slide move in front of it (only when the
    slide itself is included). Also returns {video step id: its video-first slide}."""
    out: List[Step] = []
    owner: Dict[str, Step] = {}
    i = 0
    while i < len(steps):
        s = steps[i]
        vids = videos_after(steps, i) if s.kind == "slide" else []
        if vids and is_video_first(s) and s.include:
            out.extend(vids)
            out.append(s)
            for v in vids:
                owner[v.id] = s
            i += 1 + len(vids)
        else:
            out.append(s)
            i += 1
    return out, owner


def slide_videos(all_steps: Sequence[Step], slide: Step) -> List[Step]:
    """The included videos of this slide that really play (in list order)."""
    idx = next((k for k, s in enumerate(all_steps) if s.id == slide.id), -1)
    if idx < 0:
        return []
    return [v for v in videos_after(all_steps, idx) if v.include and v.plays_video()]


def _size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def over_video(slide: Step, videos: Sequence[Step], shots_dir: Path) -> List[bool]:
    """For each reveal item of the slide: whether it lies over one of the videos (videos playing in place only)."""
    rv = slide.reveal
    if rv is None or not rv.items:
        return []
    page = _size(shots_dir / rv.clean) if rv.clean else None
    if page is None and slide.screenshot:
        page = _size(shots_dir / slide.screenshot)
    rects = [v.clip.rect for v in videos if v.clip is not None and v.clip.mode == "inset" and v.clip.rect is not None]
    if not page or not rects:
        return [False] * len(rv.items)
    pw, ph = page
    out = []
    for it in rv.items:
        size = _size(shots_dir / it.file)
        if not size:
            out.append(False)
            continue
        x0, y0, x1, y1 = it.x / pw, it.y / ph, (it.x + size[0]) / pw, (it.y + size[1]) / ph
        area = max(1e-9, (x1 - x0) * (y1 - y0))
        inside = max(max(0.0, min(x1, r.x + r.w) - max(x0, r.x)) * max(0.0, min(y1, r.y + r.h) - max(y0, r.y))
                     for r in rects)
        out.append(inside / area >= OVER_VIDEO)
    return out


@lru_cache(maxsize=64)
def _grab_last(src: str, mtime: float, start: float, end: float, w: int, h: int) -> Optional[Image.Image]:
    from . import clips
    for back in (0.04, 0.1, 0.3, 0.8):             # the very last timestamp sometimes decodes nothing; step back a little
        t = max(start, end - back)
        frame = clips.grab_frame(Path(src), t, w, h)
        if frame is not None:
            return frame
    return clips.grab_frame(Path(src), start, w, h)


def last_frames(project_id: str, videos: Sequence[Step], draw_box, has_slide: bool,
                W: int, H: int) -> List[Optional[Tuple[Image.Image, Tuple[int, int, int, int]]]]:
    """The last frame of each video, scaled to where it plays on the canvas: (frame, (x, y, w, h)), or None for a
    video that doesn't play in place (full screen, or its file can't be read)."""
    from . import clips
    out: List[Optional[Tuple[Image.Image, Tuple[int, int, int, int]]]] = []
    for v in videos:
        src = clips.resolve(project_id, v.clip)
        frame = None
        if src is not None:
            try:
                info = clips.probe(src)
            except clips.ClipError:
                info = None
            if info is not None:
                box, inset = clips.placement(v.clip, draw_box, has_slide, W, H, info["width"], info["height"])
                if inset:                          # full-screen videos don't sit on the slide
                    start, end = clips.clip_range(v.clip)
                    frame = _grab_last(str(src), src.stat().st_mtime, start, end or info.get("duration") or start,
                                       box[2], box[3])
        out.append((frame.copy(), box) if frame is not None else None)
    return out
