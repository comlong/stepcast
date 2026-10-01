"""画面合成：把截图 + 高亮 + 光标 + 标题渲染成视频帧（Pillow）。

设计：
  stage  = 背景 + 浏览器外框 + 截图 + 遮罩变暗（静态，一步只算一次）
  frame  = stage 按缓动缩放裁剪 -> 叠加高亮框 / 点击涟漪 / 鼠标指针 / 标题角标
"""
from __future__ import annotations

import math
import threading
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .. import config
from ..models import Step
from . import gdi_text
from .textlayout import (RAQM, clusters, font_path_for, is_complex, layout_font_kwargs, shape,
                         strip_trailing_punct)


# ---- 工具 ----------------------------------------------------------------

def hex_rgb(color: str, default=(14, 17, 22)) -> Tuple[int, int, int]:
    try:
        h = color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:
        return default


def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - pow(1 - t, 3)


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 3 * t * t - 2 * t * t * t


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# 字体按线程分开缓存：并行渲染时多个线程同时用同一个 FreeTypeFont 会画花甚至崩溃
_fonts = threading.local()


def load_font(size: int, bold: bool = False, text: str = "") -> ImageFont.FreeTypeFont:
    """text 给了就挑一个有这些字的字体（阿拉伯文、韩文、印地文……微软雅黑里没有）。"""
    cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = getattr(_fonts, "cache", None)
    if cache is None:
        cache = _fonts.cache = {}
    path = font_path_for(text, bold) if text else config.font_path(bold)
    key = (path, size)
    if key in cache:
        return cache[key]
    try:
        f = ImageFont.truetype(path, size, **layout_font_kwargs()) if path else ImageFont.load_default()
    except Exception:
        f = ImageFont.load_default()
    cache[key] = f
    return f


def text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    text = shape(text)
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]
    except Exception:
        return draw.textlength(text, font=font), font.size


def is_cjk_char(ch: str) -> bool:
    return ("⺀" <= ch <= "鿿" or "　" <= ch <= "〿"  # i18n: ignore
            or "＀" <= ch <= "￯" or "가" <= ch <= "힯")


def _use_gdi(font, text: str) -> bool:
    """阿拉伯文、希伯来文、印地文、泰文等交给 Windows 的排版引擎（见 gdi_text）。"""
    return gdi_text.AVAILABLE and not RAQM and is_complex(text) and bool(getattr(font, "path", None))


def text_width(font, text: str) -> float:
    """画出来的宽度（阿拉伯文要按连写之后的字形量）。"""
    if _use_gdi(font, text):
        w = gdi_text.measure(text, font.path, font.size)
        if w is not None:
            return w
    return font.getlength(shape(text))


@lru_cache(maxsize=128)
def _gdi_outline(text: str, path: str, px: int, width: int) -> Optional[Image.Image]:
    res = gdi_text.render(text, path, px)
    return res[0].filter(ImageFilter.MaxFilter(width * 2 + 1)) if res else None


def _with_alpha(mask: Image.Image, color) -> Image.Image:
    a = color[3] if len(color) > 3 else 255
    return mask if a >= 255 else mask.point([v * a // 255 for v in range(256)])


def draw_text(d: ImageDraw.ImageDraw, xy, text: str, font, fill, anchor: str = "la",
              stroke_width: int = 0, stroke_fill=None) -> None:
    """画一段文字。复杂文字用 Windows 排版引擎画成遮罩再上色，其余照旧用 Pillow。"""
    if _use_gdi(font, text):
        res = gdi_text.render(text, font.path, font.size)
        if res is not None:
            mask, w, h, pad = res
            ax, ay = (anchor + "a")[0], (anchor + "a")[1]
            left = xy[0] - (w / 2 if ax == "m" else w if ax == "r" else 0) - pad
            top = xy[1] - (h / 2 if ay == "m" else h if ay in ("b", "d") else 0) - pad
            pos = (int(round(left)), int(round(top)))
            if stroke_width and stroke_fill is not None:
                outline = _gdi_outline(text, font.path, font.size, int(stroke_width))
                if outline is not None:
                    d.bitmap(pos, _with_alpha(outline, stroke_fill), fill=tuple(stroke_fill[:3]))
            d.bitmap(pos, _with_alpha(mask, fill), fill=tuple(fill[:3]))
            return
    d.text(xy, shape(text), font=font, fill=fill, anchor=anchor,
           stroke_width=stroke_width, stroke_fill=stroke_fill)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    """按像素宽度折行：中日韩逐字断行，西文回退到最近的空格，不劈开单词。

    返回逻辑顺序的行，画之前再用 shape() 转成显示顺序。字幕每一帧都要画同一句，所以结果缓存。
    """
    if not text:
        return []
    return list(_wrap(text, font, int(max_width)))


@lru_cache(maxsize=512)
def _wrap(text: str, font, max_width: int) -> Tuple[str, ...]:
    lines: List[str] = []
    cur = ""
    # 按「字 + 附在上面的符号」走：泰文声调、印地文元音符号不会和前面的字母拆到两行
    for ch in clusters(text):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if cur and text_width(font, trial) > max_width:
            sp = cur.rfind(" ")
            word = cur[sp + 1:]
            # 行尾这一串是西文单词的一部分 -> 整个单词挪到下一行
            if sp > 0 and ch != " " and word and not any(is_cjk_char(c) for c in word):
                lines.append(cur[:sp])
                cur = word + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return tuple(lines)


def apply_redactions(img: Image.Image, redactions, ratio: float = 1.0) -> Image.Image:
    """把打码区域直接烧进截图。坐标是 CSS 像素，ratio 是 devicePixelRatio。

    模糊/像素化都先降采样再放大（马赛克），信息是真的被丢掉了，不是盖一层。
    """
    if not redactions or img is None:
        return img
    img = img.copy()
    W, H = img.size
    for r in redactions:
        x0 = int(max(0, min(W, r.x * ratio)))
        y0 = int(max(0, min(H, r.y * ratio)))
        x1 = int(max(0, min(W, (r.x + r.w) * ratio)))
        y1 = int(max(0, min(H, (r.y + r.h) * ratio)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        box = (x0, y0, x1, y1)
        if r.mode == "solid":
            d = ImageDraw.Draw(img)
            d.rounded_rectangle(box, radius=max(2, (y1 - y0) // 6), fill=(38, 43, 54))
            continue
        block = max(4.0, (y1 - y0) / 4.5)
        sw = max(1, int((x1 - x0) / block))
        sh = max(1, int((y1 - y0) / block))
        region = img.crop(box).resize((sw, sh), Image.BILINEAR).resize(
            (x1 - x0, y1 - y0), Image.NEAREST)
        if r.mode != "pixelate":
            region = region.filter(ImageFilter.GaussianBlur(max(2, block * 0.6)))
        img.paste(region, box)
    return img


def rounded_shadow(size: Tuple[int, int], box: Tuple[int, int, int, int],
                   radius: int, blur: int, alpha: int = 150) -> Image.Image:
    layer = Image.new("L", size, 0)
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=radius, fill=alpha)
    return layer.filter(ImageFilter.GaussianBlur(blur))


# ---- 主题 ----------------------------------------------------------------

@dataclass
class Theme:
    width: int = 1920
    height: int = 1080
    bg: Tuple[int, int, int] = (14, 17, 22)
    accent: Tuple[int, int, int] = (255, 92, 57)
    dim: bool = True
    zoom: bool = True
    zoom_factor: float = 1.35
    show_cursor: bool = True
    show_badge: bool = True
    browser_frame: bool = True
    blur_backdrop: bool = True
    subtitles: bool = True
    slide_reveal: bool = False        # 幻灯片逐条出现 + 突出当前（项目设置 slides_reveal）
    sub_space: bool = True            # 给第二语言字幕留位置：主字幕底边在 93.5%，第二字幕紧贴在它下面

    @classmethod
    def from_config(cls, overrides: Optional[dict] = None) -> "Theme":
        cfg = config.load()
        if overrides:
            cfg.update({k: v for k, v in overrides.items() if v is not None})
        return cls(
            width=int(cfg.get("video_width", 1920)),
            height=int(cfg.get("video_height", 1080)),
            bg=hex_rgb(cfg.get("background_color", "#0E1116")),
            accent=hex_rgb(cfg.get("accent_color", "#FF5C39"), (255, 92, 57)),
            dim=bool(cfg.get("dim_background", True)),
            zoom=bool(cfg.get("zoom_enabled", True)),
            zoom_factor=float(cfg.get("zoom_factor", 1.35)),
            show_cursor=bool(cfg.get("show_cursor", True)),
            show_badge=bool(cfg.get("show_step_badge", True)),
            browser_frame=bool(cfg.get("browser_frame", True)),
            subtitles=bool(cfg.get("burn_subtitles", True)),
            slide_reveal=bool(cfg.get("slides_reveal", False)),
            sub_space=bool(cfg.get("second_sub_space", True)),
        )


# ---- 单步渲染器 -----------------------------------------------------------

class StepRenderer:
    """一个步骤 = 一个 StepRenderer，负责产出这一步的所有帧。"""

    CURSOR_MOVE = 0.55        # 指针飞向目标用时
    RIPPLE_DUR = 0.9          # 点击涟漪持续时间

    def __init__(self, step: Step, screenshot_path: Path, theme: Theme,
                 total_steps: int = 0, prev_point: Optional[Tuple[float, float]] = None,
                 duration: float = 4.0, step_no: int = 0, speech_offset: float = 0.0,
                 prev_frame: Optional[Image.Image] = None):
        self.step = step
        self.theme = theme
        self.total_steps = total_steps
        self.step_no = step_no or step.index + 1     # 在成片里是第几步（排除掉的步骤不算）
        self.speech_offset = speech_offset            # 配音从这一步的第几秒开始（翻页后停一拍再开口）
        self.prev_frame = prev_frame                  # 上一页最后的画面：翻页时从它淡过来，不闪黑
        self.duration = max(0.8, duration)
        self.W, self.H = theme.width, theme.height
        self.is_slide = step.kind in ("slide", "video")      # 视频步骤的底图就是它所在的那页幻灯片

        self.shot = self._load(screenshot_path)
        # 打码在最前面做：后面的缩放、模糊背景、放大镜头都基于已脱敏的图
        if self.shot is not None and step.redactions:
            self.shot = apply_redactions(self.shot, step.redactions, self._img_ratio())
        self.draw_box = self._layout()                       # 截图在画布上的位置
        self.target = self._target_rect()                    # 高亮框（画布坐标）
        self.point = self._click_point()                     # 点击点（画布坐标）
        self.prev_point = prev_point or (self.W * 0.5, self.H * 0.92)
        self.stage = self._build_stage()
        # 幻灯片逐条出现：打了码的页不用（条目图是没打码的原图），数据坏了也退回整页一起出现
        self.reveal = None
        if (self.is_slide and step.kind == "slide" and theme.slide_reveal and step.reveal is not None
                and step.reveal.enabled and not step.redactions and self.shot is not None):
            from .slide_reveal import build_anim
            try:
                self.reveal = build_anim(self, Path(screenshot_path).parent)
            except Exception:
                self.reveal = None

    @classmethod
    def click_point(cls, step: Step, screenshot_path: Path,
                    theme: "Theme") -> Optional[Tuple[float, float]]:
        """只算这一步的点击点，不解码整张截图（Image.open 只读文件头）。

        并行渲染前要先把每一步的光标起点串起来（上一步点在哪，这一步光标就从哪飞过来），
        用它就不必先把所有步骤的底图都建出来、占掉几百 MB 内存。
        """
        self = cls.__new__(cls)
        self.step, self.theme = step, theme
        self.W, self.H = theme.width, theme.height
        self.is_slide = step.kind in ("slide", "video")      # 视频步骤的底图就是它所在的那页幻灯片
        try:
            with Image.open(screenshot_path) as im:
                # 只要尺寸：读完文件头就关掉，不然每一步都占着一个文件句柄（Windows 上删不掉项目）
                self.shot = SimpleNamespace(size=im.size)
        except Exception:
            self.shot = None
        self.draw_box = self._layout()
        self.target = self._target_rect()
        return self._click_point()

    def still_after(self) -> float:
        """这个时间点之后画面不再变化（秒）；-1 表示一直在动。

        幻灯片只有开头一小段翻页淡入，之后每一帧都一模一样，可以直接复用上一帧，
        省掉大量重复绘制。录屏步骤有光标、高亮呼吸等一直在动的元素。
        """
        return 0.35 if self.is_slide else -1.0

    def frame_key(self, t: float):
        """画面静止时返回一个标识：标识相同的两帧一模一样，可以直接复用上一帧；画面在动就返回 None。"""
        if not self.is_slide or t < 0.35:
            return None
        if self.reveal is not None:
            return self.reveal.key(t)
        return "static"

    def final_frame(self) -> Image.Image:
        """这一步最后的完整画面（编辑器预览用）：逐条出现的页显示全部条目。"""
        if self.reveal is not None:
            return self.reveal.compose(0.0, final=True)
        return self.stage.copy()

    # -- 资源 --
    def _load(self, path: Path) -> Optional[Image.Image]:
        try:
            with Image.open(path) as im:
                return im.convert("RGB")
        except Exception:
            return None

    # -- 布局 --
    def _layout(self) -> Tuple[int, int, int, int]:
        """截图绘制区域 (x0,y0,x1,y1)。"""
        th = self.theme
        if self.is_slide:
            pad = int(self.H * 0.035)            # 左右
            top = int(self.H * 0.012)            # 幻灯片上面没有别的东西，离画面顶边留一点就够
            # 字幕放在页面下方，不压内容（一行主字幕底框高 5.5%，顶边正好在幻灯片下沿下面一点）
            bottom = int(self.H * (0.125 if th.sub_space else 0.105)) if th.subtitles else top
            avail_w, avail_h = self.W - pad * 2, self.H - top - bottom
            if not self.shot:
                return (pad, top, pad + avail_w, top + avail_h)
            sw, sh = self.shot.size
            scale = min(avail_w / sw, avail_h / sh)
            w, h = int(sw * scale), int(sh * scale)
            x0 = (self.W - w) // 2
            y0 = top + (avail_h - h) // 2
            return (x0, y0, x0 + w, y0 + h)
        pad_x = int(self.W * 0.035)
        pad_top = int(self.H * 0.075)
        pad_bottom = int(self.H * 0.085)      # 给字幕留位置
        avail_w = self.W - pad_x * 2
        avail_h = self.H - pad_top - pad_bottom
        bar = int(self.H * 0.035) if th.browser_frame else 0
        avail_h -= bar
        if not self.shot:
            return (pad_x, pad_top + bar, pad_x + avail_w, pad_top + bar + avail_h)
        sw, sh = self.shot.size
        scale = min(avail_w / sw, avail_h / sh)
        w, h = int(sw * scale), int(sh * scale)
        x0 = (self.W - w) // 2
        y0 = pad_top + bar + (avail_h - h) // 2
        return (x0, y0, x0 + w, y0 + h)

    def _img_ratio(self) -> float:
        """CSS 像素 -> 截图像素 的比例（devicePixelRatio）。"""
        s = self.step
        if s.viewport_w and s.img_w:
            return s.img_w / s.viewport_w
        return 1.0

    def _to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        """截图内坐标（CSS 像素）-> 画布坐标。"""
        x0, y0, x1, y1 = self.draw_box
        if not self.shot:
            return (x0 + x, y0 + y)
        r = self._img_ratio()
        sx = (x1 - x0) / self.shot.size[0]
        sy = (y1 - y0) / self.shot.size[1]
        return (x0 + x * r * sx, y0 + y * r * sy)

    def _target_rect(self) -> Optional[Tuple[float, float, float, float]]:
        s = self.step
        if not s.highlight or not s.target or not s.target.rect:
            return None
        r = s.target.rect
        if r.w <= 0 or r.h <= 0:
            return None
        ax, ay = self._to_canvas(r.x, r.y)
        bx, by = self._to_canvas(r.x + r.w, r.y + r.h)
        x0, y0, x1, y1 = self.draw_box
        # 限制在截图范围内，并给极小元素一个最小尺寸
        ax, bx = max(x0, ax), min(x1, bx)
        ay, by = max(y0, ay), min(y1, by)
        if bx - ax < 16:
            cx = (ax + bx) / 2
            ax, bx = cx - 8, cx + 8
        if by - ay < 16:
            cy = (ay + by) / 2
            ay, by = cy - 8, cy + 8
        if bx <= ax or by <= ay:
            return None
        return (ax, ay, bx, by)

    def _click_point(self) -> Optional[Tuple[float, float]]:
        s = self.step
        if s.point and ("x" in s.point):
            return self._to_canvas(float(s.point["x"]), float(s.point["y"]))
        if self.target:
            x0, y0, x1, y1 = self.target
            return ((x0 + x1) / 2, (y0 + y1) / 2)
        return None

    # -- 静态底图 --
    def _build_stage(self) -> Image.Image:
        th = self.theme
        canvas = Image.new("RGB", (self.W, self.H), th.bg)
        if self.is_slide:
            x0, y0, x1, y1 = self.draw_box
            r = max(6, int(self.H * 0.008))
            sh = rounded_shadow((self.W, self.H), (x0, y0 + 6, x1, y1 + 10), r,
                                int(self.H * 0.02), 160)
            canvas.paste(Image.new("RGB", (self.W, self.H), (0, 0, 0)), (0, 0), sh)
            if self.shot:
                page = self.shot.resize((x1 - x0, y1 - y0), Image.LANCZOS)
                mask = Image.new("L", page.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, page.size[0] - 1, page.size[1] - 1),
                                                       radius=r, fill=255)
                canvas.paste(page, (x0, y0), mask)
            return canvas

        # 背景：截图放大模糊，营造氛围
        if self.shot and th.blur_backdrop:
            try:
                bw, bh = self.shot.size
                scale = max(self.W / bw, self.H / bh) * 1.15
                big = self.shot.resize((int(bw * scale), int(bh * scale)), Image.BILINEAR)
                big = big.filter(ImageFilter.GaussianBlur(38))
                ox = (big.size[0] - self.W) // 2
                oy = (big.size[1] - self.H) // 2
                big = big.crop((ox, oy, ox + self.W, oy + self.H))
                canvas = Image.blend(canvas, big.convert("RGB"), 0.35)
            except Exception:
                pass

        x0, y0, x1, y1 = self.draw_box
        radius = max(8, int(self.H * 0.012))
        bar_h = int(self.H * 0.035) if th.browser_frame else 0

        # 投影
        shadow_box = (x0 - 6, y0 - bar_h - 6, x1 + 6, y1 + 14)
        sh = rounded_shadow((self.W, self.H), shadow_box, radius + 6, int(self.H * 0.022), 170)
        canvas.paste(Image.new("RGB", (self.W, self.H), (0, 0, 0)), (0, 0), sh)

        # 浏览器外框
        if th.browser_frame:
            frame = Image.new("RGB", (self.W, self.H))
            fd = ImageDraw.Draw(frame)
            mask = Image.new("L", (self.W, self.H), 0)
            md = ImageDraw.Draw(mask)
            md.rounded_rectangle((x0, y0 - bar_h, x1, y1), radius=radius, fill=255)
            fd.rectangle((x0, y0 - bar_h, x1, y1), fill=(32, 37, 45))
            canvas.paste(frame, (0, 0), mask)
            d = ImageDraw.Draw(canvas)
            cy = y0 - bar_h / 2
            for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
                cx = x0 + bar_h * (0.7 + i * 0.55)
                r = bar_h * 0.16
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
            # 地址栏
            pill_x0 = x0 + bar_h * 2.6
            pill_x1 = min(x1 - bar_h * 0.6, pill_x0 + (x1 - x0) * 0.55)
            ph = bar_h * 0.56
            d.rounded_rectangle((pill_x0, cy - ph / 2, pill_x1, cy + ph / 2),
                                radius=ph / 2, fill=(22, 26, 32))
            url = (self.step.url or "").replace("https://", "").replace("http://", "")
            if url:
                f = load_font(max(11, int(bar_h * 0.42)), text=url)
                tw = text_width(f, url)
                maxw = pill_x1 - pill_x0 - 24
                while tw > maxw and len(url) > 8:
                    url = url[:-4] + "…"
                    tw = text_width(f, url)
                draw_text(d, (pill_x0 + 14, cy), url, f, (150, 158, 170), anchor="lm")

        # 截图（圆角）
        if self.shot:
            shot = self.shot.resize((x1 - x0, y1 - y0), Image.LANCZOS)
            mask = Image.new("L", (x1 - x0, y1 - y0), 0)
            md = ImageDraw.Draw(mask)
            r2 = 0 if th.browser_frame else radius
            md.rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=r2, fill=255)
            canvas.paste(shot, (x0, y0), mask)

        # 变暗遮罩（高亮区域挖空）
        if th.dim and self.target:
            overlay = Image.new("L", (self.W, self.H), 0)
            od = ImageDraw.Draw(overlay)
            od.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=44)
            tx0, ty0, tx1, ty1 = self.target
            pad = max(14, int(self.H * 0.020))
            od.rounded_rectangle((tx0 - pad, ty0 - pad, tx1 + pad, ty1 + pad),
                                 radius=max(8, int(self.H * 0.014)), fill=0)
            overlay = overlay.filter(ImageFilter.GaussianBlur(max(6, int(self.H * 0.014))))
            canvas.paste(Image.new("RGB", (self.W, self.H), (0, 0, 0)), (0, 0), overlay)

        return canvas

    # -- 缩放变换 --
    def _zoom_at(self, t: float) -> Tuple[float, float, float]:
        """返回 (scale, crop_x0, crop_y0)。"""
        th = self.theme
        if self.is_slide:
            return (1.0, 0.0, 0.0)          # 幻灯片不推镜
        if not (th.zoom and self.step.zoom and self.target):
            return (1.0, 0.0, 0.0)
        # 目标越小放得越大，但不超过 zoom_factor 上限
        tx0, ty0, tx1, ty1 = self.target
        tw, thh = tx1 - tx0, ty1 - ty0
        want = min(self.W / max(tw * 3.2, 1), self.H / max(thh * 3.2, 1))
        smax = max(1.0, min(th.zoom_factor, want))
        if smax <= 1.02:
            return (1.0, 0.0, 0.0)

        ramp = 0.9
        hold_out = 0.5
        if t < self.CURSOR_MOVE * 0.6:
            k = 0.0
        elif t < self.CURSOR_MOVE * 0.6 + ramp:
            k = ease_in_out((t - self.CURSOR_MOVE * 0.6) / ramp)
        elif t > self.duration - hold_out:
            k = 1 - ease_in_out((t - (self.duration - hold_out)) / hold_out)
        else:
            k = 1.0
        s = lerp(1.0, smax, k)
        if s <= 1.001:
            return (1.0, 0.0, 0.0)

        cw, ch = self.W / s, self.H / s
        cx = lerp(self.W / 2, (tx0 + tx1) / 2, k)
        cy = lerp(self.H / 2, (ty0 + ty1) / 2, k)
        x0 = cx - cw / 2
        y0 = cy - ch / 2
        # 优先把取景框限制在截图内部，避免画面边缘露出背景
        bx0, by0, bx1, by1 = self.draw_box
        if cw <= bx1 - bx0:
            x0 = min(max(x0, bx0), bx1 - cw)
        if ch <= by1 - by0:
            y0 = min(max(y0, by0), by1 - ch)
        x0 = max(0.0, min(self.W - cw, x0))
        y0 = max(0.0, min(self.H - ch, y0))
        return (s, x0, y0)

    @staticmethod
    def _tx(pt: Tuple[float, float], s: float, ox: float, oy: float) -> Tuple[float, float]:
        return ((pt[0] - ox) * s, (pt[1] - oy) * s)

    # -- 每帧 --
    def frame(self, t: float) -> Image.Image:
        if self.is_slide:
            img = self.reveal.compose(t) if self.reveal is not None else self.stage.copy()
            if t < 0.35:   # 翻页：上一页也是幻灯片就交叉淡入淡出，否则从背景色淡入
                k = ease_out_cubic(t / 0.35)
                if self.prev_frame is not None and self.prev_frame.size == img.size:
                    img = Image.blend(self.prev_frame, img, k)
                else:
                    a = int(255 * (1 - k))
                    ImageDraw.Draw(img, "RGBA").rectangle((0, 0, self.W, self.H), fill=self.theme.bg + (a,))
            return img
        s, ox, oy = self._zoom_at(t)
        if s > 1.001:
            cw, ch = self.W / s, self.H / s
            img = self.stage.crop((int(ox), int(oy), int(ox + cw), int(oy + ch)))
            img = img.resize((self.W, self.H), Image.BILINEAR)
        else:
            img = self.stage.copy()
            s, ox, oy = 1.0, 0.0, 0.0

        # 直接在 RGB 图上用 RGBA 模式绘制，比逐层 alpha_composite 快很多
        d = ImageDraw.Draw(img, "RGBA")
        self._draw_highlight(d, t, s, ox, oy)
        self._draw_ripple(d, t, s, ox, oy)
        self._draw_value_bubble(d, t, s, ox, oy)
        self._draw_header(d, t)
        if self.theme.show_cursor:
            self._draw_cursor(d, t, s, ox, oy)
        return img

    # -- 元素 --
    def _draw_highlight(self, d: ImageDraw.ImageDraw, t: float, s: float, ox: float, oy: float):
        if not self.target:
            return
        appear = 0.25
        k = ease_out_cubic((t - self.CURSOR_MOVE * 0.5) / appear) if t > self.CURSOR_MOVE * 0.5 else 0.0
        if k <= 0:
            return
        x0, y0 = self._tx((self.target[0], self.target[1]), s, ox, oy)
        x1, y1 = self._tx((self.target[2], self.target[3]), s, ox, oy)
        pad = max(5.0, self.H * 0.006 * s)
        grow = (1 - k) * self.H * 0.02
        box = (x0 - pad - grow, y0 - pad - grow, x1 + pad + grow, y1 + pad + grow)
        pulse = 0.5 + 0.5 * math.sin(t * 3.6)
        w = max(3, int((self.H * 0.0035 + self.H * 0.0018 * pulse) * max(1.0, s * 0.7)))
        r = max(6, int(self.H * 0.010 * s))
        a = int(255 * min(1.0, k))
        col = self.theme.accent
        # 外发光
        d.rounded_rectangle(
            (box[0] - w, box[1] - w, box[2] + w, box[3] + w),
            radius=r + w, outline=col + (int(a * 0.28),), width=max(2, w * 2),
        )
        d.rounded_rectangle(box, radius=r, outline=col + (a,), width=w)

    def _draw_ripple(self, d: ImageDraw.ImageDraw, t: float, s: float, ox: float, oy: float):
        if not self.point or self.step.kind not in ("click", "key"):
            return
        t0 = self.CURSOR_MOVE
        if t < t0 or t > t0 + self.RIPPLE_DUR:
            return
        k = (t - t0) / self.RIPPLE_DUR
        cx, cy = self._tx(self.point, s, ox, oy)
        base = self.H * 0.022 * max(1.0, s * 0.8)
        for delay in (0.0, 0.28):
            kk = k - delay
            if kk <= 0 or kk >= 1:
                continue
            rr = base * (0.5 + 2.4 * ease_out_cubic(kk))
            a = int(200 * (1 - kk) ** 1.6)
            if a <= 3:
                continue
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr),
                      outline=self.theme.accent + (a,), width=max(2, int(self.H * 0.0035)))
        # 中心点
        a0 = int(230 * (1 - ease_out_cubic(min(1.0, k * 1.6))))
        if a0 > 5:
            rr = base * 0.45
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=self.theme.accent + (a0,))

    def _draw_cursor(self, d: ImageDraw.ImageDraw, t: float, s: float, ox: float, oy: float):
        if not self.point:
            return
        k = ease_out_cubic(t / self.CURSOR_MOVE) if t < self.CURSOR_MOVE else 1.0
        px = lerp(self.prev_point[0], self.point[0], k)
        py = lerp(self.prev_point[1], self.point[1], k)
        cx, cy = self._tx((px, py), s, ox, oy)
        if not (-100 < cx < self.W + 100 and -100 < cy < self.H + 100):
            return
        sz = self.H * 0.030
        press = 1.0
        if self.CURSOR_MOVE <= t < self.CURSOR_MOVE + 0.16:
            press = 0.84 + 0.16 * abs(math.cos((t - self.CURSOR_MOVE) / 0.16 * math.pi))
        sz *= press
        pts = [(0, 0), (0, 1.0), (0.28, 0.76), (0.46, 1.12), (0.63, 1.04),
               (0.45, 0.68), (0.75, 0.62)]
        poly = [(cx + x * sz, cy + y * sz) for x, y in pts]
        shadow = [(x + sz * 0.06, y + sz * 0.08) for x, y in poly]
        d.polygon(shadow, fill=(0, 0, 0, 90))
        d.polygon(poly, fill=(255, 255, 255, 255), outline=(20, 22, 28, 235))

    def _draw_header(self, d: ImageDraw.ImageDraw, t: float):
        th = self.theme
        k = ease_out_cubic(t / 0.4)
        a = int(255 * k)
        y = self.H * 0.038
        title = (self.step.title or "").strip()
        f_title = load_font(max(20, int(self.H * 0.034)), bold=True, text=title)
        f_badge = load_font(max(14, int(self.H * 0.021)), bold=True)

        x = self.W * 0.035
        badge_w = 0.0
        label = ""
        if th.show_badge and self.total_steps:
            label = f"{self.step_no} / {self.total_steps}"
            badge_w = d.textlength(label, font=f_badge) + self.H * 0.028

        title_w = 0.0
        if title:
            maxw = self.W * 0.90 - x - badge_w
            while text_width(f_title, title) > maxw and len(title) > 6:
                title = title[:-2] + "…"
            title_w = text_width(f_title, title)

        # 深色底托，保证叠在网页内容上也读得清
        bh = self.H * 0.058
        total_w = badge_w + (self.H * 0.018 + title_w if title else 0)
        if total_w > 0:
            pad = self.H * 0.016
            d.rounded_rectangle((x - pad, y - bh / 2, x + total_w + pad, y + bh / 2),
                                radius=bh / 2, fill=(10, 13, 18, int(a * 0.78)))

        if label:
            hb = self.H * 0.040
            d.rounded_rectangle((x, y - hb / 2, x + badge_w, y + hb / 2), radius=hb / 2,
                                fill=th.accent + (a,))
            d.text((x + badge_w / 2, y), label, font=f_badge, fill=(255, 255, 255, a), anchor="mm")
            x += badge_w + self.H * 0.018
        if title:
            draw_text(d, (x, y), title, f_title, (255, 255, 255, a), anchor="lm")

    def _draw_value_bubble(self, d: ImageDraw.ImageDraw, t: float, s: float, ox: float, oy: float):
        """输入类步骤：在输入框旁边显示键入的内容。"""
        st = self.step
        if st.kind != "input" or not st.value or not self.target:
            return
        if t < self.CURSOR_MOVE * 0.8:
            return
        k = ease_out_cubic((t - self.CURSOR_MOVE * 0.8) / 0.3)
        a = int(255 * min(1.0, k))
        txt = st.value if len(st.value) <= 42 else st.value[:41] + "…"
        f = load_font(max(16, int(self.H * 0.026)), bold=True, text=txt)
        tw = text_width(f, txt)
        pad = self.H * 0.014
        bw, bh = tw + pad * 2, self.H * 0.052
        x0, y0 = self._tx((self.target[0], self.target[1]), s, ox, oy)
        x1, y1 = self._tx((self.target[2], self.target[3]), s, ox, oy)
        bx = min(max(x0, 10), self.W - bw - 10)
        by = y1 + self.H * 0.014
        if by + bh > self.H * 0.92:
            by = y0 - bh - self.H * 0.014
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh * 0.28,
                            fill=(24, 28, 36, int(a * 0.94)), outline=self.theme.accent + (a,), width=2)
        draw_text(d, (bx + bw / 2, by + bh / 2), txt, f, (240, 244, 250, a), anchor="mm")


# ---- 字幕（直接画进帧里，不依赖 ffmpeg 的 libass） -------------------------

def sub_bottom(theme: Theme) -> float:
    """主字幕底边在画面高度的多少处。给第二语言字幕留位置时 93.5%：下面的 6.5% 放一行第二字幕（外挂，
    紧贴在主字幕下面）；不留时 95.5%。两种都比早先的 94.5% / 90.5% 往下，幻灯片能显示得更大。"""
    return 0.935 if theme.sub_space else 0.955


def draw_subtitle(img: Image.Image, text: str, theme: Theme) -> Image.Image:
    """在画面底部绘制一条字幕。"""
    if not text:
        return img
    W, H = theme.width, theme.height
    d = ImageDraw.Draw(img, "RGBA")
    text = strip_trailing_punct(text)
    f = load_font(max(22, int(H * 0.040)), bold=True, text=text)
    lines = wrap_text(d, text, f, int(W * 0.76))[:3]
    if not lines:
        return img
    # 底框贴着文字：上下只留一点边（以前上下各 1.6%，一行字的底框高 7.7%，现在 5.5%），
    # 文字离底框下沿近了，第二语言字幕也就离主字幕近了
    line_h = int(H * 0.050)
    pad_x = int(H * 0.018)
    pad_y = int(H * 0.007)
    box_w = int(max(text_width(f, ln) for ln in lines)) + pad_x * 2
    box_h = line_h * len(lines) + pad_y * 2 - int(line_h * 0.18)
    bx = (W - box_w) // 2
    by = int(H * sub_bottom(theme)) - box_h
    d.rounded_rectangle((bx, by, bx + box_w, by + box_h),
                        radius=int(H * 0.010), fill=(8, 10, 14, 190))
    y = by + pad_y
    for ln in lines:
        draw_text(d, (W / 2, y + line_h / 2 - int(line_h * 0.09)), ln, f,
                  (255, 255, 255, 255), anchor="mm",
                  stroke_width=max(1, int(H * 0.0016)), stroke_fill=(0, 0, 0, 210))
        y += line_h
    return img


# ---- 片头 / 片尾卡 --------------------------------------------------------

_card_bg_cache: Dict[Tuple[int, int, Tuple[int, int, int], Tuple[int, int, int]], Image.Image] = {}


def _card_background(theme: Theme) -> Image.Image:
    """片头背景（含高斯模糊光晕）只算一次，后面每帧复用。"""
    key = (theme.width, theme.height, theme.bg, theme.accent)
    if key in _card_bg_cache:
        return _card_bg_cache[key]
    W, H = theme.width, theme.height
    img = Image.new("RGB", (W, H), theme.bg)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-W * 0.25, H * 0.25, W * 0.55, H * 1.45), fill=theme.accent + (70,))
    gd.ellipse((W * 0.55, -H * 0.45, W * 1.35, H * 0.7), fill=(60, 110, 255, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(int(H * 0.09)))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    _card_bg_cache[key] = img
    return img


_card_img_cache: Dict[tuple, Image.Image] = {}


def _card_image_background(theme: Theme, path: Path, fit: str, band: bool) -> Image.Image:
    """用户上传的片头 / 片尾背景铺满画面。片头每一帧都一样，算一次缓存起来。

    fit=contain：整张完整显示，四周空出来的地方用同一张图放大模糊补齐（比纯色边框好看）；
    fit=cover：铺满画面，多出来的裁掉。带透明通道的图（比如 logo）放在默认的渐变背景上。
    band：要在上面叠字时，中间压一条柔和的暗带，亮色背景上的白字也看得清。
    """
    st = path.stat()
    key = (str(path), st.st_mtime_ns, theme.width, theme.height, theme.bg, theme.accent, fit, band)
    hit = _card_img_cache.get(key)
    if hit is not None:
        return hit
    W, H = theme.width, theme.height
    with Image.open(path) as im:
        src = im.convert("RGBA")
    opaque = src.getchannel("A").getextrema()[0] == 255
    if fit == "cover":
        fg = ImageOps.fit(src, (W, H), Image.LANCZOS)
        img = _card_background(theme).copy() if not opaque else Image.new("RGB", (W, H))
        img.paste(fg, (0, 0), fg)
    else:
        if opaque:
            img = ImageOps.fit(src.convert("RGB"), (W // 4, H // 4), Image.BILINEAR)
            img = img.filter(ImageFilter.GaussianBlur(max(2, H // 90))).resize((W, H), Image.BILINEAR)
            img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.35)
        else:
            img = _card_background(theme).copy()
        s = min(W / src.width, H / src.height)
        w, h = max(1, round(src.width * s)), max(1, round(src.height * s))
        fg = src.resize((w, h), Image.LANCZOS)
        img.paste(fg, ((W - w) // 2, (H - h) // 2), fg)
    if band:
        shade = Image.new("L", (W, H), 0)
        ImageDraw.Draw(shade).rectangle((0, int(H * 0.30), W, int(H * 0.70)), fill=150)
        shade = shade.filter(ImageFilter.GaussianBlur(int(H * 0.08)))
        img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, shade)
    if len(_card_img_cache) > 16:
        _card_img_cache.clear()
    _card_img_cache[key] = img
    return img


def render_title_card(theme: Theme, title: str, subtitle: str = "",
                      t: float = 0.0, duration: float = 3.0, background: Optional[Path] = None,
                      show_text: bool = True, fit: str = "contain") -> Image.Image:
    """片头卡。background 是用户自定义的背景图（图片或 PPT 的一页）；show_text=False 时只显示背景。"""
    W, H = theme.width, theme.height
    show_text = show_text or background is None          # 默认背景上没有字就什么都没有了
    has_text = show_text and bool((title or "").strip() or (subtitle or "").strip())
    base = None
    if background is not None:
        try:
            base = _card_image_background(theme, background, fit, band=has_text)
        except Exception:
            base = None                                   # 图坏了就用默认背景，不让整段渲染失败
    img = (base if base is not None else _card_background(theme)).copy()
    if not has_text:
        return img
    d = ImageDraw.Draw(img, "RGBA")

    k = ease_out_cubic(min(1.0, t / 0.7))
    fade_out = 1.0
    if t > duration - 0.4:
        fade_out = max(0.0, 1 - (t - (duration - 0.4)) / 0.4)
    a = int(255 * k * fade_out)
    rise = (1 - k) * H * 0.05

    f_title = load_font(max(40, int(H * 0.075)), bold=True, text=title or "")
    f_sub = load_font(max(20, int(H * 0.030)), text=subtitle or "")

    lines = wrap_text(d, title or "", f_title, int(W * 0.78))[:3]
    line_h = H * 0.095
    total_h = len(lines) * line_h + (H * 0.075 if subtitle else 0)
    y = H / 2 - total_h / 2 + rise

    # 主色小横杠
    d.rounded_rectangle((W * 0.5 - H * 0.05, y - H * 0.065, W * 0.5 + H * 0.05, y - H * 0.055),
                        radius=H * 0.005, fill=theme.accent + (a,))
    for ln in lines:
        draw_text(d, (W / 2, y + line_h / 2), ln, f_title, (255, 255, 255, a), anchor="mm")
        y += line_h
    if subtitle:
        sub_lines = wrap_text(d, subtitle, f_sub, int(W * 0.66))[:2]
        y += H * 0.012
        for ln in sub_lines:
            draw_text(d, (W / 2, y + H * 0.026), ln, f_sub, (178, 186, 200, a), anchor="mm")
            y += H * 0.042
    return img


def render_outro_card(theme: Theme, text: str, t: float = 0.0, duration: float = 3.0,
                      background: Optional[Path] = None, show_text: bool = True,
                      fit: str = "contain") -> Image.Image:
    # 有自定义背景时没写片尾文字就只显示背景；默认背景上至少放个 ✓
    return render_title_card(theme, text or ("" if background is not None else "✓"), "", t, duration,
                             background=background, show_text=show_text, fit=fit)
