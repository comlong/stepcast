"""Slides: "reveal points one by one + highlight the current one".

At import (needs PowerPoint on this computer):
  Each slide is split into an "always visible" part (title area, page number, full-page background images, large panels) and "items" in reading order:
  the frame, number, heading and text of one card form one item; a text box with several bullets gives one item per top-level bullet (with its sub-bullets).
  PowerPoint then exports a base image with all items hidden, plus an image of each item shown on its own;
  comparing the two yields that item's own pixels, saved as a cropped image with transparency.
  The layout never changes: shapes are hidden by making them invisible, bullets by making their text fully transparent; everything else stays in place.

When rendering (no PowerPoint needed):
  On top of the base image, each item fades in and floats up slightly when the narration reaches it; when the next item comes, earlier ones dim so the current one stands out;
  at the very end all items return to full brightness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageFilter

from . import slide_timeline

MAX_ITEMS = 12               # slides with more items than this aren't revealed one by one (too fragmented)
APPEAR = 0.45                # time for one item to appear (seconds)
DIM = 0.4                    # time for earlier items to dim
FOCUS_ALPHA = 0.4            # how far discussed items dim
RESTORE = 1.6                # everything returns to full brightness during the last this-many seconds
RESTORE_FADE = 0.5
FIRST_AT = 0.5               # earliest appearance (after the slide fade-in)
STAGGER = 0.2                # items mentioned in the same sentence appear this many seconds apart (same beat, they don't dim each other)

TITLE_PH = {1, 3}            # ppPlaceholderTitle / ppPlaceholderCenterTitle
BODY_PH = {2, 7}             # ppPlaceholderBody / ppPlaceholderObject


# ---- grouping (pure functions, no PowerPoint, easy to test) --------------------

@dataclass
class Unit:
    """Something that can be shown / hidden on its own: a whole shape, or one bullet (with sub-bullets) of a text box."""
    shape: int                       # shape index (from 0)
    box: List[float]                 # x, y, w, h (fraction of the page)
    text: str = ""
    ph: int = 0                      # placeholder type
    paras: List[int] = field(default_factory=list)   # empty = the whole shape; otherwise paragraph numbers (from 1)


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
    """Find gaps along one axis that no item crosses and split the items into groups (axis 0 = vertical gaps / columns, 1 = horizontal gaps / rows)."""
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
    """XY cut: split into rows by horizontal gaps, then into columns by vertical gaps, recursively.
    A row of cards → left to right; two columns → finish the left column before the right one."""
    def cut(idxs: List[int]) -> List[int]:
        if len(idxs) <= 1:
            return idxs
        for axis in (1, 0):
            groups = _split(idxs, boxes, axis)
            if len(groups) > 1:
                return [j for g in groups for j in cut(g)]
        # can't be split (they overlap): sort by center, top first, then left
        return sorted(idxs, key=lambda i: (round(_center(boxes[i])[1] / 0.05), boxes[i][0]))
    return cut(list(range(len(boxes))))


def group(units: List[Unit]) -> Tuple[List[int], List[List[int]]]:
    """Returns (indices of always-visible units, items); items are in reading order, each a list of unit indices.
    With fewer than 2 or more than MAX_ITEMS items, no items are returned (the slide appears as a whole)."""
    keep: List[int] = []
    content: List[int] = []
    for i, u in enumerate(units):
        b = u.box
        is_text = bool(u.text)
        if u.paras:
            content.append(i)
        elif _area(b) > 0.6:                                   # full-page background image / color block
            keep.append(i)
        elif u.ph in TITLE_PH or (is_text and b[1] < 0.2 and b[1] + b[3] < 0.3):
            keep.append(i)                                     # title area
        elif is_text and b[1] > 0.9:                           # footer, page number
            keep.append(i)
        else:
            content.append(i)

    def contains(ci: int, j: int) -> bool:
        return ci != j and _inside(_center(units[j].box), units[ci].box) and \
            _area(units[j].box) < _area(units[ci].box)

    # cards: whole shapes without text, of moderate size, that contain other shapes
    boxes = [i for i in content if not units[i].text and not units[i].paras
             and 0.004 < _area(units[i].box) < 0.6 and any(contains(i, j) for j in content)]
    panels = [p for p in boxes if sum(contains(p, q) for q in boxes) >= 2]   # large panels holding two or more cards
    cards = [c for c in boxes if c not in panels]
    cards = [c for c in cards if not any(contains(c, d) for d in cards)]     # innermost cards only
    keep += panels

    owner: Dict[int, int] = {c: c for c in cards}
    for j in content:
        if j in owner or j in panels:
            continue
        hits = [c for c in cards if contains(c, j)]
        if hits:
            owner[j] = min(hits, key=lambda c: _area(units[c].box))
        elif any(contains(p, j) for p in panels):
            keep.append(j)                                     # a heading on a panel stays visible with the panel

    # loose shapes outside cards: merge close neighbours on one row (number + text) and tightly stacked ones (heading + text) into one item;
    # bullets split from the same text box stay separate items and are never merged with others
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
            # same row: only merge a narrow + wide pair right next to each other ("number / icon + text"); two text columns side by side stay separate
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

    # small decorations (arrows, icons) aren't items of their own: they appear with the item right after them in reading order
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

    # headings (short single-line text directly followed by a group of content) appear with the first item below them
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


# ---- import: export the base image and each item with PowerPoint --------------------

def _units_of_slide(slide, SW: float, SH: float) -> Tuple[List[Unit], list]:
    """Read all units of a slide. Multi-bullet text boxes (outside cards) are split into one unit per bullet."""
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
    """Whether this text box can be split by bullets: not a title, not in the title area / footer, and not inside a card (cards appear as a whole)."""
    box, text, ph = infos[si]
    if not text or "\n" not in text and "\r" not in text:
        return False
    if ph in TITLE_PH or _area(box) > 0.6 or (box[1] < 0.2 and box[1] + box[3] < 0.3) or box[1] > 0.9:
        return False
    c = _center(box)
    return not any(j != si and not t and 0.004 < _area(b) < 0.6 and _area(b) > _area(box) and _inside(c, b)
                   for j, (b, t, _p) in enumerate(infos))


def _paragraph_groups(sh, SW: float, SH: float) -> List[Tuple[List[int], List[float], str]]:
    """Bullets of a text box: each top-level bullet with its sub-bullets is one group. Returns nothing for a single paragraph."""
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
    """Show / hide units in PowerPoint (only in the copy opened in memory; nothing is saved)."""

    def __init__(self, units: List[Unit], shapes: list):
        self.units, self.shapes = units, shapes

    def set(self, idxs: Sequence[int], visible: bool) -> None:
        for i in idxs:
            u = self.units[i]
            sh = self.shapes[u.shape]
            if not u.paras:
                sh.Visible = -1 if visible else 0
                continue
            # make the text and bullet fully transparent instead of toggling "show bullets":
            # turning it off and on makes PowerPoint switch to numbering, and hidden bullets would restart later numbers at 1
            tr = sh.TextFrame2.TextRange
            for p in u.paras:
                para = tr.Paragraphs.Item(p)
                para.Font.Fill.Transparency = 0.0 if visible else 1.0
                try:
                    para.ParagraphFormat.Bullet.Font.Fill.Transparency = 0.0 if visible else 1.0
                except Exception:
                    pass


def layer_of(item_png: Path, clean_png: Path) -> Optional[Tuple[Image.Image, int, int]]:
    """Compare "only this item shown" with the base image to get the item's own pixels (with transparency), cropped to the smallest box."""
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
    """Export base images and items for the selected slides with PowerPoint. Returns {slide number: {"clean": file, "items": [{file, x, y, text}]}};
    slides with fewer than two items are left out (they appear as a whole)."""
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
                meta = _export_page(pres.Slides(p), p, SW, SH, width, height, out_dir, src)
            except Exception:
                meta = None                       # this slide can't be analysed: it appears as a whole; other slides are unaffected
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


def _export_page(slide, page: int, SW: float, SH: float, W: int, H: int, out_dir: Path,
                 src: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    try:
        meta = slide_timeline.export_slide(slide, page, W, H, out_dir, src)       # the author's own animations first
    except Exception:
        meta = None                                                          # they couldn't be read: group by position instead
    if meta:
        return meta
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


# ---- timing: which item the narration is talking about ----------------------------

LEAD = 0.35                  # short pause after a slide change before speaking (as long as the slide fade-in)
CASCADE = 0.35               # when the narration can't be matched: interval between items appearing at the start
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # i18n: ignore
_SENT_END = "。！？；.!?;\n"
_SENT_RE = re.compile(r"[。！？；!?;]+|\.(?=\s|$|[A-Z\u4e00-\u9fff])|\n+")    # a full stop followed directly by a capital letter (no space) also ends a sentence


def _strip(text: str) -> Tuple[str, List[int]]:
    """Keep only letters, digits and CJK characters (lowercased) and remember each one's position in the original text."""
    out, idx = [], []
    for i, ch in enumerate(text):
        if ch.isalnum():
            out.append(ch.lower())
            idx.append(i)
    return "".join(out), idx


_CLAUSE_END = re.compile(r"(?<!\d)[,，、:：]|[,，、:：](?!\d)")           # a comma or colon, but not inside a number ("1,000")
CLAUSE_LONG = (110, 50)      # (Latin, Chinese / Japanese / Korean): a sentence longer than this many characters is also cut at its commas
CLAUSE_MIN = (30, 14)        # ... into pieces of at least this many characters


def _clauses(start: int, sent: str) -> List[Tuple[int, str]]:
    """A long sentence (a list of things, say) cut at its commas into pieces of reasonable length, so that the AI can tell in which piece each item
    is first talked about, not just in which sentence. Short sentences stay whole. Positions are in the original text."""
    cjk = 1 if _CJK.search(sent) else 0
    if len(sent) <= CLAUSE_LONG[cjk]:
        return [(start, sent)]
    cuts = [m.end() for m in _CLAUSE_END.finditer(sent) if m.end() < len(sent)]
    spans: List[List[int]] = []
    a = 0
    for c in cuts:
        if c - a >= CLAUSE_MIN[cjk]:
            spans.append([a, c])
            a = c
    if spans and len(sent) - a < CLAUSE_MIN[cjk]:
        spans[-1][1] = len(sent)                       # a short tail goes with the piece before it
    else:
        spans.append([a, len(sent)])
    out = []
    for a, b in spans:
        seg = sent[a:b]
        out.append((start + a + len(seg) - len(seg.lstrip()), seg.strip()))
    return out


def sentences(text: str) -> List[Tuple[int, str]]:
    """Split the narration into the units the AI lines the slide up with: [(start position in the original text, text)]. Sentences, and the long
    ones also cut at their commas."""
    out: List[Tuple[int, str]] = []

    def add(start: int, seg: str) -> None:
        if seg.strip():
            out.extend(_clauses(start + len(seg) - len(seg.lstrip()), seg.strip()))

    start = 0
    for m in _SENT_RE.finditer(text):
        add(start, text[start:m.end()])
        start = m.end()
    add(start, text[start:])
    return out


def align_key(narration: str, texts: Sequence[str]) -> str:
    """Fingerprint of "narration + item texts": an alignment result is only valid while both are unchanged."""
    import hashlib
    h = hashlib.sha1((narration or "").strip().encode("utf-8"))
    for t in texts:
        h.update(b"\x00" + (t or "").strip().encode("utf-8"))
    return h.hexdigest()[:16]


def match_positions(texts: Sequence[str], narration: str) -> List[Optional[int]]:
    """Search the narration for each item's text (forward, in reading order); returns the start of the sentence that mentions it, or None."""
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
    """At which second of the voice-over a position in the narration is spoken (slightly early, so the item appears as the words start)."""
    from .subtitles import _char_time_map, _time_at_char
    cmap = _char_time_map(boundaries) if boundaries else []
    if cmap:
        t = _time_at_char(cmap, pos, len(narration), audio_dur)
    else:
        t = audio_dur * pos / max(1, len(narration))
    return max(FIRST_AT, t - 0.15)


def _fill(times: List[Optional[float]], total_dur: float) -> List[float]:
    """Fill in items that weren't found: a run of missing items is spread evenly between the found times around it."""
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
    """Appearance times from text matching; items not found are interpolated between their neighbours."""
    if not narration.strip() or audio_dur <= 0:
        return _fill([None] * len(texts), total_dur)
    pos = match_positions(texts, narration)
    return _fill([None if p is None else _time_of(p, narration, boundaries, audio_dur) for p in pos], total_dur)


def _between(times: List[Optional[float]]) -> List[float]:
    """Items not mentioned (None) appear one after another between the mentioned items around them; mentioned ones keep their time (follows the narration, not necessarily monotonic)."""
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
    """Appearance time of each item (0 = visible from the start) and whether to highlight the current one; None if the slide appears as a whole.

    - With AI alignment: at the point where the narration actually discusses each item; unmentioned items appear between their neighbours; if none is mentioned they appear quickly one after another
    - Without: search the slide text in the narration. If more than half isn't found (e.g. the narration is in another language or paraphrased),
      items appear quickly one after another at the start without highlighting — we don't know what is being discussed, so it's better to lay everything out early
    - Items without text (images) appear with the nearest text item; on image-only slides they appear quickly one after another after the slide change
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
        focus = False                                 # the AI says no item is discussed: appear quickly one after another
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
        return None                                   # the narration mentions no item: appear together
    return out, focus


# ---- AI alignment: works across languages and paraphrases ------------------------------

ALIGN_SYSTEM = "你负责把幻灯片的讲解词和页面上的内容条目对应起来。只输出 JSON。"  # i18n: ignore
ALIGN_TMPL = """下面每一页给出：  # i18n: ignore
- items：页面上的内容条目，编号从 1 开始，按页面阅读顺序排列（有动画的页按动画出现的先后；"（图片）"表示没有文字的图片）
- sentences：这一页的讲解词，已经切成句子，编号从 1 开始
讲解词和页面文字可能是不同的语言，也可能是意译，请按意思对应。
对每一页，找出每个条目在讲解里「第一次被讲到」的句子编号；讲解里没讲到的条目（包括图片）填 0。
严格按 JSON 输出：{{"pages": [{{"page": 页的编号, "map": [每个条目对应的句子编号]}}]}}
map 的长度必须和这一页 items 的个数一样。

{pages}"""  # i18n: ignore
ALIGN_BATCH = 8


def reveal_texts(step) -> List[str]:
    """What the narration is lined up with, in the order it appears: one text per item (items grouped by position),
    or per step (slides with PowerPoint animations: the card, its text and its badge are one step)."""
    return slide_timeline.unit_texts(step.reveal.items) if step.reveal else []


def needs_align(step) -> bool:
    rv = step.reveal
    if step.kind != "slide" or rv is None or not rv.enabled or not (step.narration or "").strip():
        return False
    texts = reveal_texts(step)
    if len(texts) < 2 or not any(t.strip() for t in texts):
        return False
    return rv.align_key != align_key(step.narration, texts)


def align_steps(steps: Sequence[Any], client=None,
                progress: Optional[Callable[[float, str], None]] = None,
                label: Callable[[int, int], str] = lambda i, n: "") -> Dict[str, Tuple[List[int], str]]:
    """Let the AI mark the sentence in which each item starts being discussed. Returns {step id: (character position per item, fingerprint)};
    only slides whose narration or items changed are processed. Raises LLMError without an AI configured (callers fall back to text matching)."""
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
                                    for i, t in enumerate(reveal_texts(s), 1)],
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
            if len(mp) != len(reveal_texts(s)):
                continue
            offsets = [ss[j - 1][0] if 1 <= j <= len(ss) else -1 for j in mp]
            result[s.id] = (offsets, align_key(s.narration, reveal_texts(s)))
    if progress:
        progress(1.0, label(len(batches), len(batches)))
    return result


# ---- rendering -------------------------------------------------------------------

class RevealAnim:
    """Reveal animation of one slide (positions already converted to frame coordinates).
    Items with times <= 0 are visible from the start (drawn into the base image); the others fade in on time; focus = earlier items dim when the next one comes."""

    def __init__(self, clean_stage: Image.Image, layers: List[Tuple[Image.Image, int, int]],
                 times: List[float], duration: float, rise: float, focus: bool = True,
                 under: Optional[List[Tuple[Image.Image, Tuple[int, int, int, int]]]] = None):
        base = clean_stage.copy()
        for (layer, x, y), t in zip(layers, times):
            if t <= 0:
                base.paste(layer, (x, y), layer.getchannel("A"))
        for frame, (x, y, _w, _h) in under or []:
            base.paste(frame, (x, y))                 # video-first slides: the video's last frame, under the items revealed later
        self.clean = base
        self.layers = layers
        self.times = times
        self.duration = duration
        self.rise = rise
        self.focus = focus
        self.animated = [k for k, t in enumerate(times) if t > 0]
        # actual fade-in start: items with the same time are staggered slightly in reading order, one after another
        self.start = list(times)
        seen: Dict[float, int] = {}
        for k in self.animated:
            n = seen.get(times[k], 0)
            self.start[k] = times[k] + n * STAGGER
            seen[times[k]] = n + 1
        last = max((self.start[k] for k in self.animated), default=0.0)
        self.restore_at = (duration - RESTORE if focus and duration - RESTORE > last + APPEAR else None)
        self._alpha_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._dim_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._bg_cache: Dict[int, Tuple[int, int, int]] = {}

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

    def _dimmed(self, k: int, vis: float) -> Image.Image:
        """The item with its colours taken towards the slide's own colour under it (vis = 1: as it is; 0: that colour). Same shape, still opaque:
        a see-through picture would let the picture lying behind it shine through (a photo in front of a large background drawing) and look as if the order was wrong."""
        q = int(round(vis * 20))
        key = (k, q)
        im = self._dim_cache.get(key)
        if im is None:
            layer, x, y = self.layers[k]
            bg = self._bg_cache.get(k)
            if bg is None:
                w, h = self.clean.size
                x0, y0 = max(0, min(x, w - 1)), max(0, min(y, h - 1))
                crop = self.clean.crop((x0, y0, max(x0 + 1, min(w, x + layer.width)), max(y0 + 1, min(h, y + layer.height))))
                bg = self._bg_cache[k] = crop.resize((1, 1), Image.BOX).getpixel((0, 0))[:3]
            im = Image.blend(Image.new("RGB", layer.size, bg), layer.convert("RGB"), q / 20)
            if len(self._dim_cache) > 96:
                self._dim_cache.clear()
            self._dim_cache[key] = im
        return im

    def key(self, t: float) -> Optional[Tuple[int, bool]]:
        """While the frame is static, return a state key (same key = identical frame, the previous one can be reused); None while animating."""
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
            pic = layer
            if self.focus and k != cur and self.times[k] < self.times[cur]:
                dim = ease_out_cubic(min(1.0, (t - self.times[cur]) / DIM))   # dimming starts when the first item of the same beat appears
                vis = 1 - (1 - FOCUS_ALPHA) * dim
                if self.restore_at is not None and t >= self.restore_at:
                    r = ease_out_cubic(min(1.0, (t - self.restore_at) / RESTORE_FADE))
                    vis += (1 - vis) * r
                if vis < 0.975:
                    pic = self._dimmed(k, vis)
            dy = int(round(self.rise * (1 - p)))
            img.paste(pic, (x, y + dy), self._alpha(k, p))
        return img


def build_anim(rend, shots_dir: Path) -> Optional[RevealAnim]:
    """Prepare the reveal animation for a slide StepRenderer. Returns None (the whole slide at once) if data is incomplete,
    sizes don't match, or nothing is worth revealing one by one."""
    st = rend.step
    rv = st.reveal
    if rv is not None and rv.mode == "timeline":
        return _build_timeline(rend, shots_dir)
    over = getattr(rend, "video_over", None)
    video_first = over is not None and rv is not None and len(over) == len(rv.items)
    if rv is None or len(rv.items) < (1 if video_first else 2):
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
    if video_first:
        times, focus = _video_first_times(over, texts, centers, narration, st.boundaries, audio_dur,
                                          rend.duration - lead, align, rend.theme.slide_reveal and rv.enabled)
    else:
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
    return RevealAnim(clean_stage, layers, times, rend.duration, rise=14 * rend.H / 1080, focus=focus,
                      under=getattr(rend, "video_under", None) if video_first else None)


def _video_first_times(over: Sequence[bool], texts: Sequence[str], centers: Sequence[Tuple[float, float]], narration: str,
                       boundaries: Sequence[Dict[str, Any]], audio_dur: float, total_dur: float,
                       align: Optional[Sequence[int]], reveal_on: bool) -> Tuple[List[float], bool]:
    """Times on a video-first slide: items not over a video are visible from the start (0); items over a video appear after it,
    timed by the narration like ordinary reveal items (only among themselves). With reveal switched off, or when the narration
    can't be matched, they appear one after another right away. Always > 0, so they are never visible while the video plays."""
    sub = [k for k, o in enumerate(over) if o]
    times = [0.0] * len(texts)
    focus = False
    planned = None
    if sub and reveal_on:
        planned = plan([texts[k] for k in sub], [centers[k] for k in sub], narration, boundaries, audio_dur, total_dur,
                       [align[k] for k in sub] if align is not None else None)
    if planned is not None:
        sub_times, focus = planned
    else:
        sub_times = [FIRST_AT + r * CASCADE for r in range(len(sub))]
    for k, t in zip(sub, sub_times):
        times[k] = max(t, 0.05)
    return times, focus and len(sub) >= 2


# ---- slides with PowerPoint animations (see slide_timeline) ---------------------------------

OFFSET_CAP = 1.5             # PowerPoint's delays inside one step are kept, but never longer than this (seconds)
EXIT_CAP = 4.0               # how long after its step an element may stay before it goes out again
EXIT_FADE = 0.35
STEP_GAP = 0.5               # two steps are never closer than this: they come in the author's order, one after the other


def parse_path(path: str) -> List[Tuple[float, float]]:
    """The points of a PowerPoint motion path, coordinates as fractions of the slide; curves are sampled. PowerPoint writes absolute commands
    ("M 0 0 L 0.47 0.62 E") and, for some of its built-in paths, relative ones ("M 0 0 l 0.036 0 l 0 0.036", "c ..."): lowercase = from the
    current point. Z closes the shape (back to where it started), E ends the path."""
    toks = re.findall(r"[MLCZEmlcze]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", path or "")     # numbers may also be written "1." or ".5"
    pts: List[Tuple[float, float]] = []
    cur, start, cmd, i = (0.0, 0.0), None, "", 0
    try:
        while i < len(toks):
            t = toks[i]
            if t in "MLCZEmlcze":
                i += 1
                if t in "Ee":
                    break
                if t in "Zz":
                    if start is not None and pts and pts[-1] != start:
                        pts.append(start)
                    cur = start or cur
                    continue
                cmd = t
                continue
            rel = cmd.islower()
            if cmd in ("M", "m", "L", "l"):
                x, y = float(toks[i]), float(toks[i + 1])
                i += 2
                cur = (cur[0] + x, cur[1] + y) if rel else (x, y)
                pts.append(cur)
                if cmd in ("M", "m"):
                    start = cur
                    cmd = "l" if rel else "L"                 # more pairs after a move are lines
            elif cmd in ("C", "c"):
                v = [float(toks[i + k]) for k in range(6)]
                i += 6
                x0, y0 = cur
                if rel:
                    v = [v[k] + (x0 if k % 2 == 0 else y0) for k in range(6)]
                if not pts:
                    pts.append(cur)
                x1, y1, x2, y2, x3, y3 = v
                for n in range(1, 13):
                    u = n / 12
                    c0, c1, c2, c3 = (1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u * u * (1 - u), u ** 3
                    pts.append((c0 * x0 + c1 * x1 + c2 * x2 + c3 * x3, c0 * y0 + c1 * y1 + c2 * y2 + c3 * y3))
                cur = (x3, y3)
            else:
                i += 1                                    # a number without a command: skip it
    except (ValueError, IndexError):
        return []
    return pts


class Motion:
    """A layer moving along a path (offsets in pixels from where it sits), as PowerPoint plays it: it speeds up for the first `accel` of the
    time, slows down for the last `decel`, and stays where the path ends."""

    def __init__(self, start: float, dur: float, points: List[Tuple[float, float]], accel: float = 0.0, decel: float = 0.0):
        self.start, self.dur = start, max(dur, 0.01)
        self.accel = max(0.0, min(accel, 0.9))
        self.decel = max(0.0, min(decel, 0.9 - self.accel))
        self.pts = points
        self.cum = [0.0]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            self.cum.append(self.cum[-1] + ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)

    @property
    def end(self) -> float:
        return self.start + self.dur

    def _progress(self, u: float) -> float:
        a, d = self.accel, self.decel
        v = 1 / (1 - a / 2 - d / 2)
        if a > 0 and u < a:
            return v * u * u / (2 * a)
        if d > 0 and u > 1 - d:
            return 1 - v * (1 - u) ** 2 / (2 * d)
        return v * (a / 2 + (u - a))

    def offset(self, t: float) -> Tuple[int, int]:
        if not self.pts or self.cum[-1] <= 0 or t <= self.start:
            return 0, 0
        s = self._progress(min(1.0, (t - self.start) / self.dur)) * self.cum[-1]
        for k in range(1, len(self.pts)):
            if s <= self.cum[k] or k == len(self.pts) - 1:
                seg = self.cum[k] - self.cum[k - 1]
                f = 0.0 if seg <= 0 else min(1.0, (s - self.cum[k - 1]) / seg)
                (x0, y0), (x1, y1) = self.pts[k - 1], self.pts[k]
                return int(round(x1 * f + x0 * (1 - f) - self.pts[0][0])), int(round(y1 * f + y0 * (1 - f) - self.pts[0][1]))
        return 0, 0


@dataclass
class Look:
    """How one layer comes in (and moves)."""
    anim: str = "fade"                 # "appear" | "fade" | "wipe" | "fly"
    dur: float = APPEAR                # how long coming in takes
    side: str = ""                     # a wipe / fly starts from this side: top | right | bottom | left
    motion: Optional[Motion] = None


class TimelineAnim:
    """Reveal animation of a slide with PowerPoint animations (positions already converted to frame coordinates).

    The layers are drawn in PowerPoint's own z-order, back first, every time: a card that comes in later still lies behind the text that is
    already there, and an element that is always there but lies above an animated one stays above it. Each layer has the time it comes in
    (0 = there from the start), the way it comes in (appear / fade / wipe / fly), a motion path if the author gave it one, and, if it has an
    exit, the time it goes out. The earlier steps are dimmed when the next one comes (focus) - by taking their colours towards the background,
    never by making them see-through: a see-through card would let the card lying behind it shine through and look as if the order was wrong."""

    def __init__(self, base: Image.Image, layers: List[Tuple[Image.Image, int, int]], starts: List[float], exits: List[Optional[float]],
                 beats: List[int], looks: List[Look], media: List[bool], beat_times: Dict[int, float], duration: float,
                 rise: float, focus: bool, under: Optional[List[Tuple[Image.Image, Tuple[int, int, int, int]]]] = None,
                 talk_beats: Optional[Sequence[int]] = None, bounds: Optional[Tuple[int, int, int, int]] = None):
        self.layers, self.starts, self.exits, self.beats = layers, starts, exits, beats
        self.times = starts                              # the name RevealAnim uses
        self.looks, self.media, self.beat_times = looks, media, beat_times
        # only a step that has something to say takes the focus (earlier ones dim); a card or picture coming in behind the text doesn't
        self.focus_times = {b: bt for b, bt in beat_times.items() if talk_beats is None or b in talk_beats}
        self.under = under or []
        self.duration, self.rise, self.focus = duration, rise, focus
        self.bounds = bounds or (0, 0, base.width, base.height)
        self.animated = [k for k, s in enumerate(starts) if s > 0]
        self.moving = [k for k, lk in enumerate(looks) if lk.motion is not None]
        last = max([starts[k] + self._dur(k) for k in self.animated] + [looks[k].motion.end for k in self.moving], default=0.0)
        self.restore_at = duration - RESTORE if focus and duration - RESTORE > last else None
        self._alpha_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._dim_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._bg_cache: Dict[int, Tuple[int, int, int]] = {}
        # the lowest layers that never change are drawn once
        self.floor, self.floor_n = base.copy(), 0
        while self.floor_n < len(layers) and not self._varies(self.floor_n):
            self._paste(self.floor, self.floor_n)
            self.floor_n += 1
        self._base = base
        self._clean: Optional[Image.Image] = None

    def _varies(self, k: int) -> bool:
        return self.starts[k] > 0 or self.exits[k] is not None or self.looks[k].motion is not None

    def _dur(self, k: int) -> float:
        lk = self.looks[k]
        return 0.0 if lk.anim == "appear" or lk.dur < 0.05 else lk.dur

    def _paste(self, img: Image.Image, k: int, alpha: Optional[Image.Image] = None, dx: int = 0, dy: int = 0, poster: bool = False,
               src: Optional[Image.Image] = None, box: Optional[Tuple[int, int, int, int]] = None) -> None:
        if self.media[k] and self.under and not poster:
            for frame, (x, y, _w, _h) in self.under:      # the video has played: its last frame lies where the video is
                img.paste(frame, (x, y))
            return
        layer, x, y = self.layers[k]
        pic = src if src is not None else layer
        mask = alpha if alpha is not None else layer.getchannel("A")
        if box is not None:                               # only this part of the layer is uncovered so far (wipe)
            pic, mask, x, y = pic.crop(box), mask.crop(box), x + box[0], y + box[1]
        img.paste(pic, (x + dx, y + dy), mask)

    @property
    def clean(self) -> Image.Image:
        """The slide as it is when the video plays (video-first slides): only what is there from the start, the video's own picture in place."""
        if self._clean is None:
            img = self._base.copy()
            for k in range(len(self.layers)):
                if self.starts[k] <= 0:
                    self._paste(img, k, poster=True)
            self._clean = img
        return self._clean

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

    def _dimmed(self, k: int, vis: float) -> Image.Image:
        """The layer with its colours taken towards the background under it (vis = 1: as it is; 0: the background's colour). Same shape, still opaque."""
        q = int(round(vis * 20))
        key = (k, q)
        im = self._dim_cache.get(key)
        if im is None:
            layer, x, y = self.layers[k]
            bg = self._bg_cache.get(k)
            if bg is None:
                crop = self.floor.crop((max(0, x), max(0, y), max(1, x + layer.width), max(1, y + layer.height)))
                bg = self._bg_cache[k] = crop.resize((1, 1), Image.BOX).getpixel((0, 0))[:3]
            rgb = Image.blend(Image.new("RGB", layer.size, bg), layer.convert("RGB"), q / 20)
            rgb.putalpha(layer.getchannel("A"))
            if len(self._dim_cache) > 96:
                self._dim_cache.clear()
            im = self._dim_cache[key] = rgb
        return im

    def _current(self, t: float) -> int:
        """The latest step the narration has reached (-1 = none yet)."""
        return max((b for b, bt in self.focus_times.items() if bt <= t), default=-1)

    def key(self, t: float) -> Optional[Tuple[int, int, int, bool, int]]:
        """While the frame is static, return a state key (same key = identical frame, the previous one can be reused); None while animating."""
        for k in self.animated:
            if self.starts[k] <= t < self.starts[k] + self._dur(k):
                return None
        for k in self.moving:
            if self.looks[k].motion.start <= t < self.looks[k].motion.end:
                return None
        for e in self.exits:
            if e is not None and e <= t < e + EXIT_FADE:
                return None
        if self.focus and any(bt <= t < bt + DIM for bt in self.focus_times.values()):
            return None
        if self.restore_at is not None and self.restore_at <= t < self.restore_at + RESTORE_FADE:
            return None
        return (sum(1 for k in self.animated if t >= self.starts[k]), sum(1 for e in self.exits if e is not None and t >= e),
                self._current(t), self.restore_at is not None and t >= self.restore_at,
                sum(1 for k in self.moving if t >= self.looks[k].motion.end))

    def _fly_from(self, k: int, side: str) -> Tuple[int, int]:
        """Where a layer flying in starts: just outside the slide, on the given side or corner ("top-left" …); from the bottom when unknown."""
        layer, x, y = self.layers[k]
        bx0, by0, bx1, by1 = self.bounds
        words = set((side or "").split("-"))
        dx = bx0 - (x + layer.width) if "left" in words else bx1 - x if "right" in words else 0
        dy = by0 - (y + layer.height) if "top" in words else by1 - y if "bottom" in words else 0
        return (dx, dy) if dx or dy else (0, by1 - y)

    def compose(self, t: float, final: bool = False) -> Image.Image:
        from .renderer import ease_out_cubic
        img = self.floor.copy()
        if final:                                     # the editor's picture of the whole page: every element where it sits, like PowerPoint's edit view
            for k in range(self.floor_n, len(self.layers)):
                self._paste(img, k)
            return img
        cur = self._current(t)
        for k in range(self.floor_n, len(self.layers)):
            s, lk = self.starts[k], self.looks[k]
            a, dx, dy, box = 1.0, 0, 0, None
            if s > 0:
                if t < s:
                    continue
                d = self._dur(k)
                lin = 1.0 if d <= 0 else min(1.0, (t - s) / d)
                p = ease_out_cubic(lin)
                layer = self.layers[k][0]
                if lk.anim == "fade":
                    a, dy = p, int(round(self.rise * (1 - p)))
                elif lk.anim == "wipe" and lin < 1.0:
                    side = lk.side or "bottom"
                    w, h = layer.size
                    cw, ch = max(1, int(round(w * lin))), max(1, int(round(h * lin)))
                    box = {"top": (0, 0, w, ch), "bottom": (0, h - ch, w, h), "left": (0, 0, cw, h), "right": (w - cw, 0, w, h)}.get(side, (0, h - ch, w, h))
                elif lk.anim == "fly" and lin < 1.0:
                    fx, fy = self._fly_from(k, lk.side)
                    dx, dy = int(round(fx * (1 - p))), int(round(fy * (1 - p)))
            e = self.exits[k]
            if e is not None and t >= e:
                a *= 1 - ease_out_cubic(min(1.0, (t - e) / EXIT_FADE))
                if a <= 0:
                    continue
            src = None
            if self.focus and s > 0 and 0 <= self.beats[k] < cur and not self.media[k]:
                dim = ease_out_cubic(min(1.0, (t - self.focus_times[cur]) / DIM))
                vis = 1 - (1 - FOCUS_ALPHA) * dim
                if self.restore_at is not None and t >= self.restore_at:
                    vis += (1 - vis) * ease_out_cubic(min(1.0, (t - self.restore_at) / RESTORE_FADE))
                if vis < 0.975:
                    src = self._dimmed(k, vis)
            if lk.motion is not None:
                mx, my = lk.motion.offset(t)
                dx, dy = dx + mx, dy + my
            self._paste(img, k, self._alpha(k, a) if a < 0.995 else None, dx, dy, src=src, box=box)
        return img


def _union_center(boxes: Sequence[Tuple[float, float, float, float]]) -> Tuple[float, float]:
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
    return (x0 + x1) / 2, (y0 + y1) / 2


def _build_timeline(rend, shots_dir: Path) -> Optional[TimelineAnim]:
    """The reveal animation of a slide whose layers follow the author's PowerPoint animations: each step (one click) is lined up with
    the narration like one item, and inside a step PowerPoint's own offsets are kept. None = show the slide as a whole."""
    st = rend.step
    rv = st.reveal
    video_first = getattr(rend, "video_over", None) is not None
    clean_path = shots_dir / rv.clean
    if not rv.items or not clean_path.exists():
        return None
    with Image.open(clean_path) as im:
        clean = im.convert("RGB")
    if rend.shot is None or clean.size != rend.shot.size:
        return None
    x0, y0, x1, _y1 = rend.draw_box
    f = (x1 - x0) / clean.size[0]
    layers, boxes = [], []
    for it in rv.items:
        p = shots_dir / it.file
        if not p.exists():
            return None
        with Image.open(p) as im:
            lay = im.convert("RGBA")
        boxes.append((float(it.x), float(it.y), float(lay.width), float(lay.height)))
        w, h = max(1, int(round(lay.width * f))), max(1, int(round(lay.height * f)))
        lay = lay.convert("RGBa").resize((w, h), Image.LANCZOS).convert("RGBA")       # premultiplied: no dark edges from the transparent pixels
        layers.append((lay, x0 + int(round(it.x * f)), y0 + int(round(it.y * f))))
    # what is revealed with the narration: everything, or (video first) only what the author let come in after the video; the rest is there from the start
    def revealed(it) -> bool:
        return it.beat >= 0 and (it.after_media or not video_first)

    all_beats = sorted({it.beat for it in rv.items if it.beat >= 0})
    sel = sorted({it.beat for it in rv.items if revealed(it)})
    if not sel:
        return None
    narration = st.narration or st.caption or ""
    audio_dur = st.audio_duration if st.audio else 0.0
    lead = float(getattr(rend, "speech_offset", 0.0) or 0.0)
    total = rend.duration - lead
    all_texts = slide_timeline.unit_texts(rv.items)
    align = rv.align if rv.align and len(rv.align) == len(all_beats) and rv.align_key == align_key(narration, all_texts) else None
    texts = slide_timeline.unit_texts(rv.items, only_after_media=video_first)
    centers = [_union_center([boxes[k] for k, it in enumerate(rv.items) if it.beat == b and revealed(it)]) for b in sel]
    sub_align = [align[all_beats.index(b)] for b in sel] if align is not None else None
    # the steps with text are lined up with the narration; a step without text (a card behind the text, a picture) has nothing to be found in it,
    # so it comes in its place in the author's order: between the steps around it, not together with the nearest one
    talk = [i for i, t in enumerate(texts) if t.strip()]
    planned = None
    if rend.theme.slide_reveal and rv.enabled:
        if talk:
            planned = plan([texts[i] for i in talk], [centers[i] for i in talk], narration, st.boundaries, audio_dur, total,
                           [sub_align[i] for i in talk] if sub_align is not None else None)
        elif narration.strip() and audio_dur > 0:
            planned = (_fill([None] * len(sel), total), False)
    if planned is None:
        if not video_first:
            return None
        planned = ([FIRST_AT + r * CASCADE for r in range(len(sel))], False)
    times, focus = planned
    if talk and len(talk) < len(sel):
        found = dict(zip(talk, times))
        times = _between([found.get(i) for i in range(len(sel))])
    # the steps come in the order the author clicked them, whatever order the narration happens to talk about them in
    times = list(times)
    for i in range(1, len(times)):
        times[i] = max(times[i], times[i - 1] + STEP_GAP)
    latest = max(FIRST_AT + 0.5, total - 0.4)
    times = [min(t, latest) for t in times]
    beat_times = {b: lead + t for b, t in zip(sel, times)}
    sw, sh = x1 - x0, _y1 - y0
    starts, exits, beats, looks, media, kept = [], [], [], [], [], []
    for k, it in enumerate(rv.items):
        if video_first and not revealed(it) and it.exit_beat >= 0 and it.exit_beat not in beat_times:
            continue                                      # it has already gone out again by the time the video has played
        bt = beat_times.get(it.beat) if revealed(it) else None
        starts.append(bt + min(max(0.0, it.offset), OFFSET_CAP) if bt is not None else 0.0)
        et = beat_times.get(it.exit_beat)
        exits.append(et + min(max(0.0, it.exit_offset), EXIT_CAP) if et is not None else None)
        beats.append(it.beat if bt is not None else -1)
        motion = None
        mt = beat_times.get(it.motion_beat)
        if mt is not None and it.motion_path:
            pts = [(px * sw, py * sh) for px, py in parse_path(it.motion_path)]
            if len(pts) >= 2:
                motion = Motion(mt + min(max(0.0, it.motion_offset), OFFSET_CAP), it.motion_dur, pts, it.motion_accel, it.motion_decel)
        looks.append(Look(it.anim, min(max(it.dur, 0.0), 2.0), it.side, motion))
        media.append(it.media)
        kept.append(k)
    layers = [layers[k] for k in kept]
    saved = rend.shot
    rend.shot = clean
    try:
        clean_stage = rend._build_stage()
    finally:
        rend.shot = saved
    return TimelineAnim(clean_stage, layers, starts, exits, beats, looks, media, beat_times, rend.duration,
                        rise=14 * rend.H / 1080, focus=focus and len(sel) >= 2,
                        under=getattr(rend, "video_under", None) if video_first else None,
                        talk_beats=[sel[i] for i in talk], bounds=(x0, y0, x1, _y1))
