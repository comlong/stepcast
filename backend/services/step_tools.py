"""Step operations for external assistants (an agent driving the local API): copy recorded steps between projects,
crop the edges of screenshots, and insert a ready-made image as a slide step.

All coordinates follow the recording: Target.rect, Step.point, redactions and text nodes are CSS pixels in viewport
coordinates; the screenshot is img_w / viewport_w times larger (device pixel ratio).
"""
from __future__ import annotations

import base64
import binascii
import io
import os
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image

from .. import i18n, storage
from ..models import Project, Step

MAX_CROP = 400                     # pixels per edge; more than that is almost certainly a mistake
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # decoded size of an image slide
# a small, well-compressed file can still unpack into a huge picture: width x height is checked before it is decoded
MAX_IMAGE_PIXELS = 40_000_000       # e.g. 8000 x 5000; a 4K slide is 8.3 million


class StepToolError(ValueError):
    """A request that can't be carried out (shown to the caller as a 400)."""


def _scale(step: Step) -> float:
    return step.img_w / step.viewport_w if step.viewport_w and step.img_w else 1.0


def _png_b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _shift(step: Step, off: float) -> None:
    """Move every viewport coordinate of the step by -off (after `off` CSS pixels were cut from the left and top)."""
    if step.target and step.target.rect:
        step.target.rect.x -= off
        step.target.rect.y -= off
    if step.point and "x" in step.point and "y" in step.point:
        step.point = {**step.point, "x": step.point["x"] - off, "y": step.point["y"] - off}
    for r in step.redactions:
        r.x -= off
        r.y -= off
    for n in step.text_nodes:
        n.x -= off
        n.y -= off


def _check_crop(px: int) -> int:
    px = int(px or 0)
    if px < 0 or px > MAX_CROP:
        raise StepToolError(i18n.t("裁边像素要在 0 到 {n} 之间", n=MAX_CROP))
    return px


def _croppable(step: Step) -> bool:
    return bool(step.screenshot) and step.kind not in ("slide", "video")


def _picture(pid: str, step: Step) -> Path:
    path = storage.screenshots_dir(pid) / step.screenshot
    if not path.exists():
        raise StepToolError(i18n.t("截图文件不存在：{name}", name=step.screenshot))
    return path


def _check_room(w: int, h: int, px: int) -> None:
    if w <= 2 * px + 10 or h <= 2 * px + 10:
        raise StepToolError(i18n.t("截图太小，不能裁掉这么多"))


def _load(path: Path) -> Image.Image:
    with Image.open(path) as im0:
        return im0.convert("RGB") if im0.mode not in ("RGB", "RGBA") else im0.copy()


def _set_picture(step: Step, fname: str, w: int, h: int, k: float) -> None:
    """The step now shows fname (w x h image pixels); the viewport follows at the recording's scale k."""
    step.screenshot, step.img_w, step.img_h = fname, w, h
    if step.viewport_w:
        step.viewport_w, step.viewport_h = round(w / k), round(h / k)
    else:
        step.viewport_w, step.viewport_h = w, h


def crop_into(src_pid: str, step: Step, dst_pid: str, new_id: str, px: int) -> None:
    """Write step's screenshot from src_pid into dst_pid under new_id, cutting px image pixels from every edge, and fix the
    step's size and coordinates to match. Without a crop the picture is copied unchanged."""
    im = _load(_picture(src_pid, step))
    k = _scale(step)
    if px and _croppable(step):
        _check_room(im.width, im.height, px)
        im = im.crop((px, px, im.width - px, im.height - px))
        _shift(step, px / k)
    fname, w, h = storage.save_screenshot(dst_pid, new_id, _png_b64(im))
    _set_picture(step, fname, w, h, k)


def _fresh(step: Step) -> Step:
    """A copy of step for another project: new id, no voice-over (it belonged to the other project's audio folder)."""
    st = Step.model_validate(step.model_dump())
    st.id = Step().id
    st.audio, st.audio_voice, st.audio_duration = "", "", 0.0
    st.voice_source = "tts"
    st.boundaries, st.line_times = [], []
    st.duration = 0.0
    st.reveal = None              # reveal layers are separate files of the source project
    st.web, st.web_clip = None, None   # an HTML slide's deck stays in its own project: here it is a still image
    return st


def copy_steps(src: Project, dst_pid: str, refs: List[str], crop: int = 0) -> List[Step]:
    """Copies of the source project's steps, chosen by id or by index ("12"), screenshots written into dst_pid.
    Video steps are refused: their video files aren't copied. On any error the screenshots already written are removed again."""
    crop = _check_crop(crop)
    by_id = {s.id: s for s in src.steps}
    out: List[Step] = []
    try:
        for ref in refs:
            ref = str(ref).strip()
            s = by_id.get(ref)
            if s is None and ref.isdigit() and int(ref) < len(src.steps):
                s = src.steps[int(ref)]
            if s is None:
                raise StepToolError(i18n.t("步骤不存在：{ref}", ref=ref))
            if s.kind == "video":
                raise StepToolError(i18n.t("视频步骤不能复制：{ref}", ref=ref))
            st = _fresh(s)
            if s.screenshot:
                crop_into(src.id, st, dst_pid, st.id, crop)
            out.append(st)
    except BaseException:
        for st in out:
            if st.screenshot:
                (storage.screenshots_dir(dst_pid) / st.screenshot).unlink(missing_ok=True)
        raise
    return out


def crop_many(pid: str, steps: List[Step], px: int) -> List[str]:
    """Crop the screenshots of several steps of this project in place, all or nothing: every picture is checked first, the
    cropped ones are written to temporary files next to the originals, and only when all of them are there do they replace
    the originals and the steps change. A failure leaves the pictures and the steps as they were.
    Returns the ids of the cropped steps; slides, videos and steps without a picture are left alone."""
    px = _check_crop(px)
    todo = [s for s in steps if px and _croppable(s)]
    for s in todo:                                   # only the size is read here, not the whole picture
        try:
            with Image.open(_picture(pid, s)) as im:
                w, h = im.size
        except StepToolError:
            raise
        except Exception:  # noqa: BLE001 — anything PIL can't read
            raise StepToolError(i18n.t("读不出这张截图：{name}", name=s.screenshot))
        _check_room(w, h, px)
    folder = storage.screenshots_dir(pid)
    staged: List[Tuple[Step, Path, int, int]] = []
    temps: List[Path] = []
    try:
        for s in todo:
            im = _load(_picture(pid, s))
            im = im.crop((px, px, im.width - px, im.height - px))
            tmp = folder / f"{s.id}.crop-tmp.png"
            temps.append(tmp)
            im.save(tmp, format="PNG")
            staged.append((s, tmp, im.width, im.height))
    except BaseException:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
        raise
    for s, tmp, w, h in staged:
        fname = f"{s.id}.png"
        os.replace(tmp, folder / fname)
        k = _scale(s)
        _shift(s, px / k)
        _set_picture(s, fname, w, h, k)
    return [s.id for s in todo]


def decode_image(b64: str) -> Image.Image:
    if "," in b64[:100]:
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        raise StepToolError(i18n.t("图片不是有效的 base64"))
    if len(raw) > MAX_IMAGE_BYTES:
        raise StepToolError(str(storage.UploadTooLarge(MAX_IMAGE_BYTES)))
    try:
        im = Image.open(io.BytesIO(raw))         # reads only the header: the size is known before anything is unpacked
    except Exception:  # noqa: BLE001 — anything PIL can't read (including PIL's own decompression-bomb refusal)
        raise StepToolError(i18n.t("读不出这张图片（支持 PNG / JPG）"))
    if im.width * im.height > MAX_IMAGE_PIXELS:
        raise StepToolError(i18n.t("图片太大：{w} × {h} 像素（最多 {n} 百万像素）", w=im.width, h=im.height,
                                   n=MAX_IMAGE_PIXELS // 1_000_000))
    try:
        im.load()
    except Exception:  # noqa: BLE001
        raise StepToolError(i18n.t("读不出这张图片（支持 PNG / JPG）"))
    return im.convert("RGB")


def image_slide(pid: str, image_b64: str, title: str = "", narration: str = "", slide_text: str = "",
                caption: Optional[str] = None) -> Step:
    """A slide step showing a ready-made picture (for example a background page drawn by the assistant)."""
    im = decode_image(image_b64)
    st = Step(kind="slide", zoom=False, highlight=False)
    fname, w, h = storage.save_screenshot(pid, st.id, _png_b64(im))
    st.screenshot, st.img_w, st.img_h, st.viewport_w, st.viewport_h = fname, w, h, w, h
    st.title, st.narration, st.slide_text = title or "", narration or "", slide_text or ""
    st.caption = narration or "" if caption is None else caption
    return st


def insert(proj: Project, steps: List[Step], position: Optional[int]) -> None:
    """Insert steps at position (0-based; None or past the end = append) and renumber."""
    at = len(proj.steps) if position is None or position < 0 or position > len(proj.steps) else int(position)
    proj.steps[at:at] = steps
    storage.reindex(proj)
