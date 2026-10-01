"""片头 / 片尾的自定义背景：上传一张图片，或者一份 PPT / PDF（用其中一页，比如公司统一的封面页）。

上传后存到项目的 cards/<kind>_<随机>/ 目录：
    图片      -> image.png
    PPT / PDF -> slide_001.png、slide_002.png……（每页都渲染好，换页不用重新转换）
项目里只记相对路径（Project.intro_card / outro_card），换背景时删掉旧目录。
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
MAX_SIDE = 3840                      # 背景图最长边；再大没意义，只会拖慢渲染


class CardError(RuntimeError):
    pass


def cards_dir(pid: str) -> Path:
    return storage.project_dir(pid) / "cards"


def resolve(pid: str, style: Optional[CardStyle]) -> Optional[Path]:
    """背景图的绝对路径；没设置或文件已丢失时返回 None（回到默认背景）。"""
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
    """把上传的文件变成背景图，返回要写进 CardStyle 的字段。"""
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
                    im = ImageOps.exif_transpose(im)          # 手机照片的旋转方向
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
            # PPT / PDF 的页面上一般已经有自己的标题文字，默认不再叠字
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
    """改页码 / 放置方式 / 是否叠字 / 停留时间。"""
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
