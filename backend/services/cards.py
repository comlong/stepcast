"""Custom intro / outro backgrounds: upload an image, or a PPT / PDF (one of its pages, e.g. your company's standard cover slide).

Uploads are stored in the project's cards/<kind>_<random>/ folder:
    image     -> image.png
    PPT / PDF -> slide_001.png, slide_002.png … (every page is rendered, so switching pages needs no new conversion)
The project only stores relative paths (Project.intro_card / outro_card); the old folder is deleted when the background changes.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageOps

from .. import i18n, storage
from ..models import CardStyle
from . import slides

KINDS = ("intro", "outro")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DOC_EXT = {".pptx", ".ppt", ".pdf"}
MAX_BYTES = 80 * 1024 * 1024
MAX_SIDE = 3840                      # longest side of a background image; larger is pointless and only slows rendering


class CardError(RuntimeError):
    pass


def cards_dir(pid: str) -> Path:
    return storage.project_dir(pid) / "cards"


def resolve(pid: str, style: Optional[CardStyle]) -> Optional[Path]:
    """Absolute path of the background image; None if not set or the file is missing (back to the default background)."""
    if not style or not style.image:
        return None
    base = cards_dir(pid).resolve()
    p = (base / style.image).resolve()
    return p if p.is_relative_to(base) and p.is_file() else None


def page_file(style: CardStyle, page: int) -> str:
    folder = style.image.split("/")[0]
    return f"{folder}/slide_{page:03d}.png"


def import_background(pid: str, kind: str, upload: Path, filename: str,
                      progress=None) -> Dict[str, Any]:
    """Turn the uploaded file into a background image; returns the fields to write into CardStyle."""
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXT | DOC_EXT:
        raise CardError(i18n.t("只支持图片（PNG / JPG / WebP）、PPT 或 PDF"))
    if upload.stat().st_size > MAX_BYTES:
        raise CardError(i18n.t("文件太大（最多 {mb} MB）", mb=MAX_BYTES // 1024 // 1024))
    folder = cards_dir(pid) / f"{kind}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if ext in IMAGE_EXT:
            if progress:
                progress(0.3, i18n.t("读取文件…"))
            try:
                with Image.open(upload) as im:
                    im = ImageOps.exif_transpose(im)          # rotation of phone photos
                    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                    im.thumbnail((MAX_SIDE, MAX_SIDE))
                    im.save(folder / "image.png")
            except Exception as e:
                raise CardError(i18n.t("读取图片失败：{error}", error=e))
            fields = {"image": f"{folder.name}/image.png", "pages": 0, "page": 1,
                      "show_text": True}
        else:
            src = folder / ("source" + ext)
            shutil.copyfile(upload, src)
            n = _render_document(src, folder, progress)
            src.unlink(missing_ok=True)
            # PPT / PDF pages usually have their own title text already, so no text is overlaid by default
            fields = {"image": f"{folder.name}/slide_001.png", "pages": n, "page": 1,
                      "show_text": False}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    fields.update(source=filename, fit="contain")
    return fields


def _render_document(src: Path, out_dir: Path, progress=None) -> int:
    if src.suffix.lower() == ".pdf":
        return int(slides._render_pdf(src, out_dir, progress)["count"])
    if slides.powerpoint_available():
        try:
            return int(slides._render_powerpoint(src, out_dir, progress)["count"])
        except slides.SlidesError:
            raise
        except Exception as e:
            if progress:
                progress(0.2, i18n.t("PowerPoint 导出失败，改用其他方式：{error}", error=str(e)[:80]))
    if slides._soffice():
        return int(slides._render_libreoffice(src, out_dir, progress)["count"])
    raise CardError(i18n.t("PPT 需要本机装有 PowerPoint 或 LibreOffice 才能转成图片；"
                           "也可以把这一页另存为 PDF 或图片再上传"))


def remove_folder(pid: str, style: Optional[CardStyle]) -> None:
    if not style or not style.image:
        return
    base = cards_dir(pid).resolve()
    folder = (base / style.image.split("/")[0]).resolve()
    if folder.is_relative_to(base) and folder != base:
        shutil.rmtree(folder, ignore_errors=True)


def apply_options(pid: str, style: CardStyle, body: Dict[str, Any]) -> None:
    """Change page / fit / text overlay / display time."""
    if "page" in body and style.pages:
        page = int(body["page"])
        if not 1 <= page <= style.pages:
            raise CardError(i18n.t("页码超出范围（共 {n} 页）", n=style.pages))
        name = page_file(style, page)
        if not (cards_dir(pid) / name).is_file():
            raise CardError(i18n.t("页码超出范围（共 {n} 页）", n=style.pages))
        style.page, style.image = page, name
    if body.get("fit") in ("contain", "cover"):
        style.fit = body["fit"]
    if "show_text" in body:
        style.show_text = bool(body["show_text"])
    if "duration" in body:
        style.duration = max(0.0, min(60.0, float(body["duration"] or 0)))
