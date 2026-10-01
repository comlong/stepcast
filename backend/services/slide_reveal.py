"""幻灯片「逐条出现 + 突出当前」。

导入时（需要本机 PowerPoint）：
  把每页的内容分成「常显」部分（标题区、页码、铺满整页的底图、大面板）和按阅读顺序排好的「条目」：
  同一张卡片里的底框、编号、标题、说明算一条；一个文本框里有好几条要点时，每条要点（连同下级要点）算一条。
  然后用 PowerPoint 导出一张「条目全部隐藏」的底图，以及每一条单独显示时的样子，
  两张图一比就得到这一条自己的像素，裁成带透明度的小图保存。
  版式不会变：隐藏形状用的是「不可见」，隐藏要点用的是文字全透明，其余内容的位置原封不动。

渲染时（不需要 PowerPoint）：
  以底图为背景，解说说到哪一条，那一条淡入并轻轻上浮；讲到下一条时，前面讲过的变淡、突出当前这条；
  最后全部讲完时，所有条目恢复正常亮度。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageFilter

MAX_ITEMS = 12               # 一页超过这么多条就不逐条出现了（太碎）
APPEAR = 0.45                # 一条出现用时（秒）
DIM = 0.4                    # 前面的条目变淡用时
FOCUS_ALPHA = 0.4            # 讲过的条目变淡到多少
RESTORE = 1.6                # 最后多少秒全部恢复
RESTORE_FADE = 0.5
FIRST_AT = 0.5               # 最早的出现时间（等翻页淡入完）
STAGGER = 0.2                # 同一句话讲到的几条：依次错开这么多秒出现（算同一拍，互相不变淡）

TITLE_PH = {1, 3}            # ppPlaceholderTitle / ppPlaceholderCenterTitle
BODY_PH = {2, 7}             # ppPlaceholderBody / ppPlaceholderObject


# ---- 分组（纯函数，不依赖 PowerPoint，方便测试） --------------------------------

@dataclass
class Unit:
    """一个可以单独显示 / 隐藏的东西：整个形状，或者一个文本框里的一条要点（含下级要点）。"""
    shape: int                       # 形状序号（从 0 开始）
    box: List[float]                 # x, y, w, h（相对整页的比例）
    text: str = ""
    ph: int = 0                      # 占位符类型
    paras: List[int] = field(default_factory=list)   # 空 = 整个形状；否则是段落序号（从 1 开始）


def _area(b: Sequence[float]) -> float:
    return b[2] * b[3]


def _center(b: Sequence[float]) -> Tuple[float, float]:
    return b[0] + b[2] / 2, b[1] + b[3] / 2


def _inside(pt: Tuple[float, float], b: Sequence[float], pad: float = 0.004) -> bool:
    return b[0] - pad <= pt[0] <= b[0] + b[2] + pad and b[1] - pad <= pt[1] <= b[1] + b[3] + pad


def _union(boxes: Sequence[Sequence[float]]) -> List[float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def _split(idxs: List[int], boxes: List[List[float]], axis: int) -> List[List[int]]:
    """沿一个方向找没有任何条目跨过的缝，把条目分成几组（axis 0 = 竖缝分栏，1 = 横缝分行）。"""
    order = sorted(idxs, key=lambda i: boxes[i][axis])
    groups, cur = [], [order[0]]
    edge = boxes[order[0]][axis] + boxes[order[0]][axis + 2]
    for i in order[1:]:
        if boxes[i][axis] >= edge - 0.004:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
        edge = max(edge, boxes[i][axis] + boxes[i][axis + 2])
    groups.append(cur)
    return groups


def reading_order(boxes: List[List[float]]) -> List[int]:
    """XY 切分：先按横缝分行、再按竖缝分栏，递归下去。
    一行卡片 → 从左到右；左右两栏 → 先读完左栏再读右栏。"""
    def cut(idxs: List[int]) -> List[int]:
        if len(idxs) <= 1:
            return idxs
        for axis in (1, 0):
            groups = _split(idxs, boxes, axis)
            if len(groups) > 1:
                return [j for g in groups for j in cut(g)]
        # 切不开（互相叠着）：按中心点先上后左
        return sorted(idxs, key=lambda i: (round(_center(boxes[i])[1] / 0.05), boxes[i][0]))
    return cut(list(range(len(boxes))))


def group(units: List[Unit]) -> Tuple[List[int], List[List[int]]]:
    """返回 (常显的 unit 序号, 条目列表)，条目已按阅读顺序排好，每条是 unit 序号的列表。
    条目少于 2 条或多于 MAX_ITEMS 条时返回空条目（这一页整页一起出现）。"""
    keep: List[int] = []
    content: List[int] = []
    for i, u in enumerate(units):
        b = u.box
        is_text = bool(u.text)
        if u.paras:
            content.append(i)
        elif _area(b) > 0.6:                                   # 铺满整页的底图 / 底色块
            keep.append(i)
        elif u.ph in TITLE_PH or (is_text and b[1] < 0.2 and b[1] + b[3] < 0.3):
            keep.append(i)                                     # 标题区
        elif is_text and b[1] > 0.9:                           # 页脚、页码
            keep.append(i)
        else:
            content.append(i)

    def contains(ci: int, j: int) -> bool:
        return ci != j and _inside(_center(units[j].box), units[ci].box) and \
            _area(units[j].box) < _area(units[ci].box)

    # 卡片：没有文字的整个形状，面积适中，里面还装着别的形状
    boxes = [i for i in content if not units[i].text and not units[i].paras
             and 0.004 < _area(units[i].box) < 0.6 and any(contains(i, j) for j in content)]
    panels = [p for p in boxes if sum(contains(p, q) for q in boxes) >= 2]   # 装着两张以上卡片的大面板
    cards = [c for c in boxes if c not in panels]
    cards = [c for c in cards if not any(contains(c, d) for d in cards)]     # 只要最内层的卡片
    keep += panels

    owner: Dict[int, int] = {c: c for c in cards}
    for j in content:
        if j in owner or j in panels:
            continue
        hits = [c for c in cards if contains(c, j)]
        if hits:
            owner[j] = min(hits, key=lambda c: _area(units[c].box))
        elif any(contains(p, j) for p in panels):
            keep.append(j)                                     # 面板上的小标题：跟面板一起常显

    # 卡片以外的散落形状：同一行挨得近的（编号 + 文字）、上下紧挨的（标题 + 说明）并成一条；
    # 同一个文本框拆出来的要点各自成条，不和别的合并
    loose = [j for j in content if j not in owner and j not in panels and j not in keep]
    parent = {j: j for j in loose}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in loose:
        for b in loose:
            if a >= b or units[a].paras or units[b].paras:
                continue
            A, B = units[a].box, units[b].box
            v_overlap = min(A[1] + A[3], B[1] + B[3]) - max(A[1], B[1])
            h_gap = max(A[0], B[0]) - min(A[0] + A[2], B[0] + B[2])
            h_overlap = min(A[0] + A[2], B[0] + B[2]) - max(A[0], B[0])
            v_gap = max(A[1], B[1]) - min(A[1] + A[3], B[1] + B[3])
            # 同一行：只合并「编号 / 图标 + 文字」这种一窄一宽、紧挨着的；并排的两栏文字各算各的
            same_row = v_overlap > 0.5 * min(A[3], B[3]) and h_gap < 0.03 and min(A[2], B[2]) < 0.08
            stacked = h_overlap > 0.5 * min(A[2], B[2]) and v_gap < 0.02
            if same_row or stacked:
                parent[find(a)] = find(b)

    buckets: Dict[Tuple[str, int], List[int]] = {}
    for j, c in owner.items():
        buckets.setdefault(("card", c), []).append(j)
    for j in loose:
        buckets.setdefault(("loose", find(j)), []).append(j)
    items = list(buckets.values())
    if not items:
        return list(range(len(units))), []

    def box_of(it: List[int]) -> List[float]:
        return _union([units[i].box for i in it])

    # 小装饰（箭头、图标）不单独成条：跟阅读顺序里紧挨在它后面的那条一起出现
    small = [it for it in items if all(not units[i].text for i in it) and _area(box_of(it)) < 0.01]
    big = [it for it in items if it not in small]
    if not big:
        return list(range(len(units))), []
    big = [big[k] for k in reading_order([box_of(it) for it in big])]
    for it in small:
        c = _center(box_of(it))
        after = [b for b in big if _center(box_of(b))[0] >= c[0] - 0.01 and _center(box_of(b))[1] >= c[1] - 0.05]
        target = min(after or big, key=lambda b: abs(_center(box_of(b))[0] - c[0]) + abs(_center(box_of(b))[1] - c[1]))
        target.extend(it)

    # 小标题（单独一行的短文字，下面紧跟着一组内容）：跟它下面的第一条一起出现
    merged: List[List[int]] = []
    k = 0
    while k < len(big):
        it = big[k]
        b = box_of(it)
        txt = " ".join(units[i].text for i in it)
        if (k + 1 < len(big) and len(it) == 1 and not units[it[0]].paras and "\n" not in txt
                and len(txt) <= 30 and b[3] < 0.07):
            nb = box_of(big[k + 1])
            if nb[1] >= b[1] + b[3] - 0.01:
                big[k + 1] = it + big[k + 1]
                k += 1
                continue
        merged.append(it)
        k += 1
    big = merged

    if len(big) < 2 or len(big) > MAX_ITEMS:
        return list(range(len(units))), []
    used = {i for it in big for i in it}
    keep = [i for i in range(len(units)) if i not in used]
    return keep, big


# ---- 导入：用 PowerPoint 导出底图和每一条 -------------------------------------------

def _units_of_slide(slide, SW: float, SH: float) -> Tuple[List[Unit], list]:
    """读出一页的所有 unit。多段要点的文本框（不在卡片里）拆成每条要点一个 unit。"""
    shapes = [slide.Shapes(i) for i in range(1, slide.Shapes.Count + 1)]
    infos = []
    for sh in shapes:
        try:
            text = sh.TextFrame2.TextRange.Text.strip() if sh.HasTextFrame else ""
        except Exception:
            text = ""
        try:
            ph = int(sh.PlaceholderFormat.Type)
        except Exception:
            ph = 0
        box = [float(sh.Left) / SW, float(sh.Top) / SH, float(sh.Width) / SW, float(sh.Height) / SH]
        infos.append((box, text, ph))
    units: List[Unit] = []
    for si, (sh, (box, text, ph)) in enumerate(zip(shapes, infos)):
        if _splittable(si, infos):
            paras = _paragraph_groups(sh, SW, SH)
            if len(paras) >= 2:
                for p_idx, p_box, p_text in paras:
                    units.append(Unit(si, p_box, p_text, ph, p_idx))
                continue
        units.append(Unit(si, box, text, ph))
    return units, shapes


def _splittable(si: int, infos: list) -> bool:
    """这个文本框能不能按要点拆开：不是标题、不在标题区 / 页脚，也不在哪张卡片里（卡片整张一起出现）。"""
    box, text, ph = infos[si]
    if not text or "\n" not in text and "\r" not in text:
        return False
    if ph in TITLE_PH or _area(box) > 0.6 or (box[1] < 0.2 and box[1] + box[3] < 0.3) or box[1] > 0.9:
        return False
    c = _center(box)
    return not any(j != si and not t and 0.004 < _area(b) < 0.6 and _area(b) > _area(box) and _inside(c, b)
                   for j, (b, t, _p) in enumerate(infos))


def _paragraph_groups(sh, SW: float, SH: float) -> List[Tuple[List[int], List[float], str]]:
    """文本框里的要点：每条一级要点连同它下面的下级要点算一组。只有一段就返回空。"""
    try:
        tr = sh.TextFrame2.TextRange
        n = int(tr.Paragraphs.Count)
    except Exception:
        return []
    if n < 2:
        return []
    paras = []
    for p in range(1, n + 1):
        try:
            para = tr.Paragraphs.Item(p)
            text = para.Text.strip()
            if not text:
                continue
            level = int(para.ParagraphFormat.IndentLevel)
            box = [float(para.BoundLeft) / SW, float(para.BoundTop) / SH,
                   float(para.BoundWidth) / SW, float(para.BoundHeight) / SH]
            paras.append((p, level, box, text))
        except Exception:
            return []
    if len(paras) < 2:
        return []
    top = min(lv for _, lv, _, _ in paras)
    groups: List[Tuple[List[int], List[List[float]], List[str]]] = []
    for p, lv, box, text in paras:
        if lv == top or not groups:
            groups.append(([p], [box], [text]))
        else:
            groups[-1][0].append(p)
            groups[-1][1].append(box)
            groups[-1][2].append(text)
    return [(idx, _union(bxs), "\n".join(txts)) for idx, bxs, txts in groups]


class _Visibility:
    """在 PowerPoint 里显示 / 隐藏 unit（只改内存里打开的这份，不保存）。"""

    def __init__(self, units: List[Unit], shapes: list):
        self.units, self.shapes = units, shapes

    def set(self, idxs: Sequence[int], visible: bool) -> None:
        for i in idxs:
            u = self.units[i]
            sh = self.shapes[u.shape]
            if not u.paras:
                sh.Visible = -1 if visible else 0
                continue
            # 文字和项目符号都设成全透明，不去动「显示项目符号」这个开关：
            # 关掉再打开会被 PowerPoint 改成编号样式，隐藏的要点也会让后面的编号重新从 1 开始
            tr = sh.TextFrame2.TextRange
            for p in u.paras:
                para = tr.Paragraphs.Item(p)
                para.Font.Fill.Transparency = 0.0 if visible else 1.0
                try:
                    para.ParagraphFormat.Bullet.Font.Fill.Transparency = 0.0 if visible else 1.0
                except Exception:
                    pass


def layer_of(item_png: Path, clean_png: Path) -> Optional[Tuple[Image.Image, int, int]]:
    """比较「只显示这一条」和底图，得到这一条自己的像素（带透明度），裁到最小范围。"""
    with Image.open(item_png) as a, Image.open(clean_png) as b:
        item, clean = a.convert("RGB"), b.convert("RGB")
    diff = ImageChops.difference(item, clean)
    r, g, bl = diff.split()
    m = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    m = m.point(lambda v: 0 if v < 3 else 255 if v >= 14 else int((v - 3) * 255 / 11))
    m = m.filter(ImageFilter.MaxFilter(3))
    bb = m.getbbox()
    if not bb:
        return None
    layer = item.convert("RGBA")
    layer.putalpha(m)
    return layer.crop(bb), bb[0], bb[1]


def export(src: Path, pages: Sequence[int], out_dir: Path, width: int,
           progress: Optional[Callable[[float, str], None]] = None,
           label: Callable[[int, int], str] = lambda i, n: "") -> Dict[int, Dict[str, Any]]:
    """用 PowerPoint 给选中的页导出底图和条目。返回 {页码: {"clean": 文件, "items": [{file, x, y, text}]}}；
    条目不够两条的页不在结果里（这页整页一起出现）。"""
    import pythoncom
    import win32com.client

    out_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[int, Dict[str, Any]] = {}
    pythoncom.CoInitialize()
    app = pres = None
    was_busy = False
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            was_busy = bool(app.Visible) or app.Presentations.Count > 0
        except Exception:
            was_busy = True
        pres = app.Presentations.Open(str(src.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
        SW, SH = float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)
        height = int(round(width * SH / SW))
        for n, p in enumerate(pages, 1):
            if progress:
                progress((n - 1) / max(1, len(pages)), label(n, len(pages)))
            try:
                meta = _export_page(pres.Slides(p), p, SW, SH, width, height, out_dir)
            except Exception:
                meta = None                       # 这一页分析不了：整页一起出现，不影响别的页
            if meta:
                result[p] = meta
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            if app is not None and not was_busy and app.Presentations.Count == 0:
                app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
    return result


def _export_page(slide, page: int, SW: float, SH: float, W: int, H: int, out_dir: Path) -> Optional[Dict[str, Any]]:
    units, shapes = _units_of_slide(slide, SW, SH)
    _keep, items = group(units)
    if not items:
        return None
    vis = _Visibility(units, shapes)
    all_idx = [i for it in items for i in it]
    clean = out_dir / f"p{page:03d}_clean.png"
    tmp = out_dir / f"p{page:03d}_tmp.png"
    try:
        vis.set(all_idx, False)
        slide.Export(str(clean.resolve()), "PNG", W, H)
        found = []
        for k, it in enumerate(items, 1):
            vis.set(it, True)
            slide.Export(str(tmp.resolve()), "PNG", W, H)
            vis.set(it, False)
            lay = layer_of(tmp, clean)
            if lay is None:
                continue
            img, x, y = lay
            name = f"p{page:03d}_i{k:02d}.png"
            img.save(out_dir / name)
            text = "\n".join(units[i].text for i in it if units[i].text)
            found.append({"file": name, "x": x, "y": y, "text": text})
    finally:
        vis.set(all_idx, True)
        tmp.unlink(missing_ok=True)
    if len(found) < 2:
        return None
    return {"clean": clean.name, "items": found}


# ---- 出现时间：解说说到哪一条 --------------------------------------------------------

LEAD = 0.35                  # 翻页后停一拍再开口（和翻页淡入一样长）
CASCADE = 0.35               # 解说对不上时：条目在开头依次出现的间隔
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # i18n: ignore
_SENT_END = "。！？；.!?;\n"
_SENT_RE = re.compile(r"[。！？；!?;]+|\.(?=\s|$|[A-Z\u4e00-\u9fff])|\n+")    # 句号后面没空格、直接大写字母也算断句


def _strip(text: str) -> Tuple[str, List[int]]:
    """只留下字母、数字、汉字（小写），并记下每个字在原文里的位置。"""
    out, idx = [], []
    for i, ch in enumerate(text):
        if ch.isalnum():
            out.append(ch.lower())
            idx.append(i)
    return "".join(out), idx


def sentences(text: str) -> List[Tuple[int, str]]:
    """把解说切成句子：[(句子在原文里的起始位置, 句子)]。"""
    out: List[Tuple[int, str]] = []
    start = 0
    for m in _SENT_RE.finditer(text):
        seg = text[start:m.end()]
        if seg.strip():
            out.append((start + len(seg) - len(seg.lstrip()), seg.strip()))
        start = m.end()
    seg = text[start:]
    if seg.strip():
        out.append((start + len(seg) - len(seg.lstrip()), seg.strip()))
    return out


def align_key(narration: str, texts: Sequence[str]) -> str:
    """「解说 + 条目文字」的指纹：对齐结果只在这两样都没变时有效。"""
    import hashlib
    h = hashlib.sha1((narration or "").strip().encode("utf-8"))
    for t in texts:
        h.update(b"\x00" + (t or "").strip().encode("utf-8"))
    return h.hexdigest()[:16]


def match_positions(texts: Sequence[str], narration: str) -> List[Optional[int]]:
    """在解说里找每一条的原文（按阅读顺序往后找），返回说到它的那句话的开头位置；找不到是 None。"""
    norm, idx = _strip(narration)
    pos = 0
    out: List[Optional[int]] = []
    for text in texts:
        lines = sorted((ln for ln in re.split(r"[\n\r]+", text) if ln.strip()), key=len, reverse=True)
        hit = None
        for ln in lines:
            s, _ = _strip(ln)
            if not s:
                continue
            sizes = (4, 3) if _CJK.search(s) else (12, 8)
            for size in sizes:
                size = min(size, len(s))
                cands = [norm.find(s[i:i + size], pos) for i in range(0, len(s) - size + 1)]
                cands = [c for c in cands if c >= 0]
                if cands:
                    hit = min(cands)
                    break
            if hit is not None:
                break
        if hit is None:
            out.append(None)
            continue
        orig = idx[hit]
        out.append(max(narration.rfind(ch, 0, orig) for ch in _SENT_END) + 1)
        pos = hit + 1
    return out


def _time_of(pos: int, narration: str, boundaries: Sequence[Dict[str, Any]], audio_dur: float) -> float:
    """解说里某个位置是在配音的第几秒说出来的（略提前一点，条目出现时正好开口）。"""
    from .subtitles import _char_time_map, _time_at_char
    cmap = _char_time_map(boundaries) if boundaries else []
    if cmap:
        t = _time_at_char(cmap, pos, len(narration), audio_dur)
    else:
        t = audio_dur * pos / max(1, len(narration))
    return max(FIRST_AT, t - 0.15)


def _fill(times: List[Optional[float]], total_dur: float) -> List[float]:
    """补上没找到的：连续几条都没找到时，在前后两个找到的时间之间均匀排开。"""
    n = len(times)
    end = max(FIRST_AT + 0.5, total_dur * 0.8)
    if all(t is None for t in times):
        return [FIRST_AT + (end - FIRST_AT) * k / max(1, n) for k in range(n)]
    out = list(times)
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        prev = out[i - 1] if i > 0 else FIRST_AT
        nxt = out[j] if j < n else max(end, prev + 0.5)
        for k in range(i, j):
            out[k] = prev + (nxt - prev) * (k - i + 1) / (j - i + 1)
        i = j
    res = [float(t) for t in out]            # type: ignore[arg-type]
    for k in range(1, n):
        res[k] = max(res[k], res[k - 1])
    return res


def reveal_times(texts: Sequence[str], narration: str, boundaries: Sequence[Dict[str, Any]],
                 audio_dur: float, total_dur: float) -> List[float]:
    """按原文匹配算每一条的出现时间，找不到的在前后之间插补。"""
    if not narration.strip() or audio_dur <= 0:
        return _fill([None] * len(texts), total_dur)
    pos = match_positions(texts, narration)
    return _fill([None if p is None else _time_of(p, narration, boundaries, audio_dur) for p in pos], total_dur)


def _between(times: List[Optional[float]]) -> List[float]:
    """没讲到的条目（None）排在前后两个讲到的条目之间，依次出现；讲到的保持原样（顺序跟着解说走，不强求单调）。"""
    n = len(times)
    out = list(times)
    known = [t for t in out if t is not None]
    tail = max(known) + 4.0 if known else FIRST_AT
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        prev = out[i - 1] if i > 0 else FIRST_AT - CASCADE
        nxt = out[j] if j < n else max(tail, (prev or 0) + CASCADE * (j - i + 1))
        lo, hi = min(prev, nxt), max(prev, nxt)
        for k in range(i, j):
            out[k] = max(FIRST_AT, lo + (hi - lo) * (k - i + 1) / (j - i + 1))
        i = j
    return [float(t) for t in out]                    # type: ignore[arg-type]


def plan(texts: Sequence[str], centers: Sequence[Tuple[float, float]], narration: str,
         boundaries: Sequence[Dict[str, Any]], audio_dur: float, total_dur: float,
         align: Optional[Sequence[int]] = None) -> Optional[Tuple[List[float], bool]]:
    """每一条的出现时间（0 = 一开始就显示）和要不要突出当前条；整页一起出现时返回 None。

    - 有 AI 对齐结果：按解说实际讲到每一条的位置；解说没讲到的排在前后两条之间依次出现；一条都没讲到就依次快速出现
    - 没有：在解说里找页面原文。一半以上找不到（比如解说和页面不是同一种语言、解说是意译的），
      就在开头依次快速出现、不突出当前 —— 不知道讲到哪了，与其乱放不如一开始就摆好
    - 没有文字的条目（图片）跟离它最近的文字条目同时出现；整页只有图片的，翻页后一张张快速出现
    """
    n = len(texts)
    text_idx = [k for k, t in enumerate(texts) if t.strip()]
    if not text_idx:
        return [FIRST_AT + r * CASCADE for r in range(n)], False
    times: List[Optional[float]] = [None] * n
    focus = True
    speaking = bool(narration.strip()) and audio_dur > 0
    aligned = speaking and align is not None and len(align) == n
    if aligned and any(align[k] >= 0 for k in text_idx):
        got = _between([_time_of(align[k], narration, boundaries, audio_dur) if align[k] >= 0 else None
                        for k in text_idx])
        for k, t in zip(text_idx, got):
            times[k] = t
    elif aligned:
        focus = False                                 # AI 说一条都没讲到：依次快速出现
        for r, k in enumerate(text_idx):
            times[k] = FIRST_AT + r * CASCADE
    else:
        pos = match_positions([texts[k] for k in text_idx], narration) if speaking else [None] * len(text_idx)
        if speaking and sum(p is not None for p in pos) * 2 >= len(text_idx):
            tt = _fill([None if p is None else _time_of(p, narration, boundaries, audio_dur) for p in pos],
                       total_dur)
            for k, t in zip(text_idx, tt):
                times[k] = t
        else:
            focus = False
            for r, k in enumerate(text_idx):
                times[k] = FIRST_AT + r * CASCADE
    for k in range(n):
        if times[k] is None:
            near = min(text_idx, key=lambda j: (centers[k][0] - centers[j][0]) ** 2 + (centers[k][1] - centers[j][1]) ** 2)
            times[k] = times[near]
    out = [float(t) for t in times]                  # type: ignore[arg-type]
    if not any(t > 0 for t in out):
        return None                                   # 解说一条都没讲到：一起出现
    return out, focus


# ---- AI 对齐：跨语言、意译都能对上 ------------------------------------------------------

ALIGN_SYSTEM = "你负责把幻灯片的讲解词和页面上的内容条目对应起来。只输出 JSON。"  # i18n: ignore
ALIGN_TMPL = """下面每一页给出：  # i18n: ignore
- items：页面上的内容条目，编号从 1 开始，按页面阅读顺序排列（"（图片）"表示没有文字的图片）
- sentences：这一页的讲解词，已经切成句子，编号从 1 开始
讲解词和页面文字可能是不同的语言，也可能是意译，请按意思对应。
对每一页，找出每个条目在讲解里「第一次被讲到」的句子编号；讲解里没讲到的条目（包括图片）填 0。
严格按 JSON 输出：{{"pages": [{{"page": 页的编号, "map": [每个条目对应的句子编号]}}]}}
map 的长度必须和这一页 items 的个数一样。

{pages}"""  # i18n: ignore
ALIGN_BATCH = 8


def _reveal_texts(step) -> List[str]:
    return [it.text for it in step.reveal.items] if step.reveal else []


def needs_align(step) -> bool:
    rv = step.reveal
    if step.kind != "slide" or rv is None or not rv.enabled or len(rv.items) < 2 or not (step.narration or "").strip():
        return False
    if not any(t.strip() for t in _reveal_texts(step)):
        return False
    return rv.align_key != align_key(step.narration, _reveal_texts(step))


def align_steps(steps: Sequence[Any], client=None,
                progress: Optional[Callable[[float, str], None]] = None,
                label: Callable[[int, int], str] = lambda i, n: "") -> Dict[str, Tuple[List[int], str]]:
    """让 AI 标出每一条在解说里第几句开始讲。返回 {步骤 id: (每条的字符位置, 指纹)}；
    只处理解说或条目变过的页。没配 AI 时抛 LLMError（调用方照常用文字匹配）。"""
    import json
    todo = [s for s in steps if needs_align(s)]
    if not todo:
        return {}
    if client is None:
        from .llm import get_client
        client = get_client()
    result: Dict[str, Tuple[List[int], str]] = {}
    batches = [todo[i:i + ALIGN_BATCH] for i in range(0, len(todo), ALIGN_BATCH)]
    for bi, batch in enumerate(batches):
        if progress:
            progress(bi / len(batches), label(bi + 1, len(batches)))
        pages, sents = [], {}
        for k, s in enumerate(batch, 1):
            ss = sentences(s.narration)
            sents[k] = ss
            pages.append({"page": k,
                          "items": [f"{i}. {(t.strip().replace(chr(13), ' ').replace(chr(10), ' ') or '（图片）')[:160]}"  # i18n: ignore
                                    for i, t in enumerate(_reveal_texts(s), 1)],
                          "sentences": [f"{j}. {x[:300]}" for j, (_, x) in enumerate(ss, 1)]})
        data = client.chat_json([
            {"role": "system", "content": ALIGN_SYSTEM},
            {"role": "user", "content": ALIGN_TMPL.format(pages=json.dumps(pages, ensure_ascii=False, indent=1))},
        ], temperature=0)
        for entry in (data.get("pages") or []) if isinstance(data, dict) else []:
            try:
                k = int(entry.get("page"))
                mp = [int(x or 0) for x in entry.get("map") or []]
            except (TypeError, ValueError, AttributeError):
                continue
            if k not in sents:
                continue
            s = batch[k - 1]
            ss = sents[k]
            if len(mp) != len(s.reveal.items):
                continue
            offsets = [ss[j - 1][0] if 1 <= j <= len(ss) else -1 for j in mp]
            result[s.id] = (offsets, align_key(s.narration, _reveal_texts(s)))
    if progress:
        progress(1.0, label(len(batches), len(batches)))
    return result


# ---- 渲染 -----------------------------------------------------------------------

class RevealAnim:
    """一页幻灯片的逐条出现动画（画面坐标已经换算好）。
    times 里 <= 0 的条目一开始就显示（直接画进底图），其余到时间淡入；focus = 讲到下一条时前面的变淡。"""

    def __init__(self, clean_stage: Image.Image, layers: List[Tuple[Image.Image, int, int]],
                 times: List[float], duration: float, rise: float, focus: bool = True):
        base = clean_stage.copy()
        for (layer, x, y), t in zip(layers, times):
            if t <= 0:
                base.paste(layer, (x, y), layer.getchannel("A"))
        self.clean = base
        self.layers = layers
        self.times = times
        self.duration = duration
        self.rise = rise
        self.focus = focus
        self.animated = [k for k, t in enumerate(times) if t > 0]
        # 真正开始淡入的时间：同一时刻的几条按阅读顺序错开一点，一条接一条出来
        self.start = list(times)
        seen: Dict[float, int] = {}
        for k in self.animated:
            n = seen.get(times[k], 0)
            self.start[k] = times[k] + n * STAGGER
            seen[times[k]] = n + 1
        last = max((self.start[k] for k in self.animated), default=0.0)
        self.restore_at = (duration - RESTORE if focus and duration - RESTORE > last + APPEAR else None)
        self._alpha_cache: Dict[Tuple[int, int], Image.Image] = {}

    def _alpha(self, k: int, a: float) -> Image.Image:
        key = (k, int(round(a * 100)))
        m = self._alpha_cache.get(key)
        if m is None:
            base = self.layers[k][0].getchannel("A")
            m = base if key[1] >= 100 else base.point(lambda v, a=key[1] / 100: int(v * a))
            if len(self._alpha_cache) > 64:
                self._alpha_cache.clear()
            self._alpha_cache[key] = m
        return m

    def key(self, t: float) -> Optional[Tuple[int, bool]]:
        """画面静止时返回一个状态标识（同一个标识 = 画面一模一样，可以直接复用上一帧）；在动就返回 None。"""
        for k in self.animated:
            if self.start[k] <= t < self.start[k] + max(APPEAR, DIM if self.focus else 0):
                return None
        if self.restore_at is not None and self.restore_at <= t < self.restore_at + RESTORE_FADE:
            return None
        shown = sum(1 for k in self.animated if t >= self.start[k])
        return shown, self.restore_at is not None and t >= self.restore_at

    def compose(self, t: float, final: bool = False) -> Image.Image:
        from .renderer import ease_out_cubic
        if final:
            img = self.clean.copy()
            for k in self.animated:
                layer, x, y = self.layers[k]
                img.paste(layer, (x, y), layer.getchannel("A"))
            return img
        img = self.clean.copy()
        shown = [k for k in self.animated if t >= self.start[k]]
        cur = max(shown, key=lambda k: (self.times[k], k)) if shown else None
        for k in shown:
            layer, x, y = self.layers[k]
            p = ease_out_cubic(min(1.0, (t - self.start[k]) / APPEAR))
            a = p
            if self.focus and k != cur and self.times[k] < self.times[cur]:
                dim = ease_out_cubic(min(1.0, (t - self.times[cur]) / DIM))   # 同一拍的第一条出来时就开始变淡
                vis = 1 - (1 - FOCUS_ALPHA) * dim
                if self.restore_at is not None and t >= self.restore_at:
                    r = ease_out_cubic(min(1.0, (t - self.restore_at) / RESTORE_FADE))
                    vis += (1 - vis) * r
                a *= vis
            dy = int(round(self.rise * (1 - p)))
            img.paste(layer, (x, y + dy), self._alpha(k, a))
        return img


def build_anim(rend, shots_dir: Path) -> Optional[RevealAnim]:
    """给一个幻灯片 StepRenderer 准备逐条出现动画。数据不全、尺寸对不上、没有值得逐条出现的内容时
    返回 None（整页一起出现）。"""
    st = rend.step
    rv = st.reveal
    if rv is None or len(rv.items) < 2:
        return None
    clean_path = shots_dir / rv.clean
    if not clean_path.exists():
        return None
    with Image.open(clean_path) as im:
        clean = im.convert("RGB")
    if rend.shot is None or clean.size != rend.shot.size:
        return None
    x0, y0, x1, y1 = rend.draw_box
    f = (x1 - x0) / clean.size[0]
    layers, centers = [], []
    for it in rv.items:
        p = shots_dir / it.file
        if not p.exists():
            return None
        with Image.open(p) as im:
            lay = im.convert("RGBA")
        centers.append((it.x + lay.width / 2, it.y + lay.height / 2))
        w, h = max(1, int(round(lay.width * f))), max(1, int(round(lay.height * f)))
        lay = lay.resize((w, h), Image.LANCZOS)
        layers.append((lay, x0 + int(round(it.x * f)), y0 + int(round(it.y * f))))
    texts = [it.text for it in rv.items]
    narration = st.narration or st.caption or ""
    audio_dur = st.audio_duration if st.audio else 0.0
    lead = float(getattr(rend, "speech_offset", 0.0) or 0.0)
    align = rv.align if rv.align and rv.align_key == align_key(narration, texts) else None
    planned = plan(texts, centers, narration, st.boundaries, audio_dur, rend.duration - lead, align)
    if planned is None:
        return None
    times, focus = planned
    times = [t + lead if t > 0 else t for t in times]
    saved = rend.shot
    rend.shot = clean
    try:
        clean_stage = rend._build_stage()
    finally:
        rend.shot = saved
    return RevealAnim(clean_stage, layers, times, rend.duration, rise=14 * rend.H / 1080, focus=focus)
