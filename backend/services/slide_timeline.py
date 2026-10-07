"""Slides with animations: show each element the way the author animated it in PowerPoint, in step with the narration.

How PowerPoint animates (read back from files PowerPoint wrote, and through its object model):

- A slide has a *main sequence*: an ordered list of effects. An effect has a target (a whole shape, or one paragraph of its text),
  a kind (entrance, exit, emphasis, motion path, or "play the video"), a trigger and a delay and duration.
- Trigger "on click" starts a new click group. "With previous" starts together with the effect before it, "after previous" starts when the
  effects before it have finished. Delays are counted from there. That is the whole timing model: see `schedule`.
- Whatever has no entrance effect is simply there from the start (a card behind the text, a title, a picture) and stays where it is.
  Things with an entrance effect are hidden at the start; things with an exit effect disappear later.
- A video is played by an effect of its own (its duration is the length of the clip); effects "after previous" start after it has ended.
- A motion path moves an element along a line or curve (a light spot sliding across the picture); it has its own start and duration like any effect.

For a video the narration decides when a click happens, so each click group becomes one *beat* that is lined up with the narration
(slide_reveal.plan); inside a beat the effects keep PowerPoint's own offsets. Slides with no clicks at all run by themselves in PowerPoint;
there each "after previous" step is a beat. Every shape (or paragraph) becomes a layer; layers are drawn in PowerPoint's own z-order, so
a card that comes in later still lies behind the text that is already there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from PIL import Image

CLICK, WITH, AFTER = 1, 2, 3          # PowerPoint's trigger types (msoAnimTriggerOnPageClick / WithPrevious / AfterPrevious)
MEDIA_PLAY = 83                       # msoAnimEffectMediaPlay
MAX_LAYERS = 40                       # more layers than this: the slide is too fragmented, fall back to grouping by position
APPEAR, FLY, WIPE = 1, 2, 22          # msoAnimEffectAppear / Fly / Wipe; every other entrance fades in
INSTANT = 0.05                        # an entrance this short (seconds) is just there, whatever its name


def classify(etype: int, exit_: bool) -> str:
    """"in" = entrance, "out" = exit, "media" = play the video, "path" = motion path, "other" = emphasis / media pause or stop.
    (Measured with PowerPoint: entrance and exit effects are numbers 1-51 and 53; 52 and 54-82 are emphasis; 83-85 control media; 86 and up are paths.)"""
    if etype == MEDIA_PLAY:
        return "media"
    if 1 <= etype <= 51 or etype == 53:
        return "out" if exit_ else "in"
    if etype >= 86:
        return "path"
    return "other"


@dataclass
class Effect:
    shape: int                    # index of the top-level shape in z-order (0 = at the back)
    etype: int                    # PowerPoint's EffectType
    exit: bool = False
    trigger: int = CLICK
    delay: float = 0.0
    dur: float = 0.5
    para: int = 0                 # paragraph number (from 1) for a build by paragraph; 0 = the whole shape
    background: bool = True       # the shape's own fill and outline come in with this effect (first paragraph of a build)
    shape_id: int = 0             # PowerPoint's id of the shape (to match the effect with the file's own list)
    cls: str = ""                 # what the file says it is: entr / exit / emph / path / mediacall (when known)
    direction: str = ""           # the side a wipe or fly starts from: top / right / bottom / left (from the file)
    path: str = ""                # a motion path as PowerPoint writes it ("M 0 0 L 0.47 0.62 E": fractions of the slide)
    accel: float = 0.0            # part of the duration spent speeding up / slowing down (0..1)
    decel: float = 0.0


@dataclass
class ShapeInfo:
    text: str = ""
    paras: List[str] = field(default_factory=list)    # text of each paragraph (index 0 = paragraph 1)
    media: bool = False
    visible: bool = True


@dataclass
class Timed:
    effect: Effect
    kind: str
    group: int                    # click group (0 = what runs before the first click, or the first click)
    tgroup: int                   # step inside the group: a new one starts after each "after previous"
    start: float                  # seconds after the start of the click group
    end: float


def schedule(effects: Sequence[Effect]) -> List[Timed]:
    """When each effect starts, counted from the click that begins its group (PowerPoint's own model)."""
    out: List[Timed] = []
    group = tgroup = 0
    t0 = gend = 0.0               # start of the current step; the latest end of its effects
    for e in effects:
        if e.trigger == CLICK:
            if out:
                group += 1
            tgroup, t0, gend = 0, 0.0, 0.0
        elif e.trigger == AFTER and out:
            tgroup += 1
            t0 = gend
        start = t0 + max(0.0, e.delay)
        end = start + max(0.0, e.dur)
        gend = max(gend, end)
        out.append(Timed(e, "path" if e.cls == "path" else classify(e.etype, e.exit), group, tgroup, start, end))
    return out


@dataclass
class Layer:
    shapes: List[int]                         # top-level shapes drawn in this layer
    paras: Optional[List[int]] = None         # None = the whole shape; otherwise only these paragraphs (empty list = the shape without its text)
    text: str = ""
    media: bool = False
    enter: Optional[Tuple[int, float, str]] = None     # (beat, offset in seconds, "appear" | "fade" | "wipe" | "fly"); None = there from the start
    exit: Optional[Tuple[int, float]] = None           # (beat, offset)
    after_media: bool = False
    dur: float = 0.5                                   # how long coming in takes
    side: str = ""                                     # the side a wipe / fly starts from
    motion: Optional[Tuple[int, float, float, str, float, float]] = None    # (beat, offset, duration, path, accel, decel)

    @property
    def animated(self) -> bool:
        return self.enter is not None


@dataclass
class Analysis:
    layers: List[Layer]                       # in z-order, back first
    beats: int
    media: bool = False                       # the slide plays a video
    clicks: bool = False                      # the author set clicks (otherwise the animation runs by itself)

    def beat_texts(self, only_after_media: bool = False) -> List[str]:
        """What each beat shows, in animation order (what the narration should talk about)."""
        texts: Dict[int, List[str]] = {}
        for ly in self.layers:
            if ly.enter is None or (only_after_media and not ly.after_media):
                continue
            texts.setdefault(ly.enter[0], [])
            if ly.text.strip():
                texts[ly.enter[0]].append(ly.text.strip())
        return ["\n".join(texts[b]) for b in sorted(texts)]


def analyze(effects: Sequence[Effect], shapes: Sequence[ShapeInfo]) -> Optional[Analysis]:
    """Layers and beats of a slide, or None when it has nothing worth revealing (no entrance animation)."""
    effects = [e for e in effects if 0 <= e.shape < len(shapes)]
    timed = schedule(effects)
    content = [t for t in timed if t.kind in ("in", "out", "path")]
    if not any(t.kind == "in" for t in content):
        return None
    clicks = any(t.effect.trigger == CLICK for t in timed)

    def key(t: Timed) -> Tuple[int, int]:
        return (t.group, 0 if clicks else t.tgroup)

    # beats: the click groups (or, without clicks, the steps); a step that only makes things disappear goes with the next one that makes something appear
    keys: List[Tuple[int, int]] = []
    for t in content:
        if key(t) not in keys:
            keys.append(key(t))
    has_in = {k: any(t.kind == "in" and key(t) == k for t in content) for k in keys}
    target: Dict[Tuple[int, int], Tuple[int, int]] = {}
    for i, k in enumerate(keys):
        if has_in[k]:
            target[k] = k
            continue
        nxt = next((x for x in keys[i + 1:] if has_in[x]), None)
        prv = next((x for x in reversed(keys[:i]) if has_in[x]), None)
        target[k] = nxt or prv or k
    beat_keys = [k for k in keys if has_in[k]]
    beat_no = {k: n for n, k in enumerate(beat_keys)}
    base = {k: min(t.start for t in content if t.kind == "in" and key(t) == k) for k in beat_keys}
    own_base = {k: min(t.start for t in content if key(t) == k) for k in keys}

    def place(t: Timed) -> Tuple[int, float]:
        k, tk = key(t), target[key(t)]
        if k == tk:
            return beat_no[tk], max(0.0, t.start - base[tk])
        same_group = k[0] == tk[0]
        return beat_no[tk], max(0.0, t.start - (base[tk] if same_group else own_base[k]))

    # the video: effects that start after it has ended appear after the video
    last_media = None
    for idx, t in enumerate(timed):
        if t.kind == "media":
            last_media = (idx, t)

    def after_media(idx: int, t: Timed) -> bool:
        if last_media is None or idx < last_media[0]:
            return False
        m = last_media[1]
        return t.group > m.group or (t.group == m.group and t.start >= m.end - 0.05)

    index = {id(t): i for i, t in enumerate(timed)}
    layers: List[Layer] = []
    static_run: List[int] = []

    def flush() -> None:
        if static_run:
            layers.append(Layer(list(static_run)))
            static_run.clear()

    for s, info in enumerate(shapes):
        if not info.visible:
            continue
        mine = [t for t in content if t.effect.shape == s]
        if not mine and not info.media:
            static_run.append(s)
            continue
        flush()
        if not mine:
            layers.append(Layer([s], media=True))
            continue
        ins = [t for t in mine if t.kind == "in"]
        outs = [t for t in mine if t.kind == "out"]
        paths = [t for t in mine if t.kind == "path" and not t.effect.para and t.effect.path]
        by_para = any(t.effect.para for t in mine if t.kind != "path")
        if not by_para:
            layers.append(_layer([s], None, info.text, ins[0] if ins else None, outs[0] if outs else None,
                                 place, after_media, index, info.media, paths[0] if paths else None))
            continue
        # a build by paragraph: the shape itself (fill, outline), then a layer for each animated paragraph, then the rest of the text
        first_in = min(ins, key=lambda t: (t.group, t.start)) if ins else None
        frame_in = first_in if first_in is not None and first_in.effect.background else None
        layers.append(_layer([s], [], "", frame_in, None, place, after_media, index, False))
        animated = sorted({t.effect.para for t in mine if t.effect.para})
        rest = [p for p in range(1, len(info.paras) + 1) if p not in animated]
        if rest:
            layers.append(Layer([s], rest, "\n".join(info.paras[p - 1] for p in rest if p <= len(info.paras))))
        for p in animated:
            pin = next((t for t in ins if t.effect.para == p), None)
            pout = next((t for t in outs if t.effect.para == p), None)
            txt = info.paras[p - 1] if p <= len(info.paras) else ""
            layers.append(_layer([s], [p], txt, pin, pout, place, after_media, index, False))
    flush()
    if len(layers) > MAX_LAYERS or sum(1 for ly in layers if ly.animated) == 0:
        return None
    return Analysis(layers, len(beat_keys), media=last_media is not None, clicks=clicks)


def _layer(shapes: List[int], paras: Optional[List[int]], text: str, tin: Optional[Timed], tout: Optional[Timed],
           place, after_media, index, media: bool, tpath: Optional[Timed] = None) -> Layer:
    ly = Layer(shapes, paras, text, media=media)
    if tin is not None:
        b, off = place(tin)
        e = tin.effect
        anim = "appear" if e.etype == APPEAR or e.dur < INSTANT else "wipe" if e.etype == WIPE else "fly" if e.etype == FLY else "fade"
        ly.enter = (b, off, anim)
        ly.dur, ly.side = max(0.0, e.dur), e.direction
        ly.after_media = after_media(index[id(tin)], tin)
    if tout is not None:
        ly.exit = place(tout)
    if tpath is not None:
        b, off = place(tpath)
        ly.motion = (b, off, max(0.0, tpath.effect.dur), tpath.effect.path, tpath.effect.accel, tpath.effect.decel)
    return ly


# ---- import: read the animations and export the layers with PowerPoint ----------------------

def matte(on_black: Image.Image, on_white: Image.Image) -> Optional[Tuple[Image.Image, int, int]]:
    """One element, exported over black and over white: the difference gives each pixel's opacity and the two exports its true color,
    so the element can be laid over anything later (a card that comes in afterwards, a different background) without a halo of its old surroundings.
    Returns (RGBA image cropped to the smallest box, x, y), or None when nothing is there."""
    import numpy as np
    from PIL import ImageChops
    on_black, on_white = on_black.convert("RGB"), on_white.convert("RGB")
    # Outside the box where either export differs from its background (black / white) a pixel is black on black and white on white:
    # opacity 0, nothing to compute. Most elements are small, so working only inside that box is several times faster, with the same result.
    boxes = [bb for bb in (on_black.getbbox(), ImageChops.invert(on_white).getbbox()) if bb]
    if not boxes:
        return None
    cx0, cy0 = min(bb[0] for bb in boxes), min(bb[1] for bb in boxes)
    cx1, cy1 = max(bb[2] for bb in boxes), max(bb[3] for bb in boxes)
    b = np.asarray(on_black.crop((cx0, cy0, cx1, cy1)), dtype=np.float32)
    w = np.asarray(on_white.crop((cx0, cy0, cx1, cy1)), dtype=np.float32)
    a = np.clip(1.0 - (w - b).mean(axis=2) / 255.0, 0.0, 1.0)
    a[a < 0.012] = 0.0                                  # rendering noise
    a[a > 0.988] = 1.0
    ys, xs = np.nonzero(a)
    if not len(xs):
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    a, b = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    rgb = np.clip(b / np.maximum(a[..., None], 1e-3), 0, 255)
    out = np.dstack([rgb, a * 255.0 + 0.5]).astype(np.uint8)
    return Image.fromarray(out, "RGBA"), cx0 + x0, cy0 + y0


_NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def side_of(sub: int) -> str:
    """The side a wipe / fly starts from, from its presetSubtype: bits 1 top, 2 right, 4 bottom, 8 left; a corner is two of them
    ("top-left" = 9, measured with PowerPoint). Anything else is not a side ("")."""
    if sub <= 0 or sub & ~15 or (sub & 5) == 5 or (sub & 10) == 10:
        return ""
    return "-".join(n for bit, n in ((1, "top"), (4, "bottom"), (8, "left"), (2, "right")) if sub & bit)


def xml_effects(pptx: Path, page: int) -> List[Dict[str, Any]]:
    """The effects of a slide's main sequence read straight from the .pptx file, in the order PowerPoint plays them.
    PowerPoint's object model doesn't tell the direction of a wipe or the path of a motion; the file does."""
    import zipfile

    from lxml import etree
    try:
        with zipfile.ZipFile(pptx) as z:
            pres = etree.fromstring(z.read("ppt/presentation.xml"))
            rels = etree.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
            target = {r.get("Id"): r.get("Target") for r in rels}[list(pres.find("p:sldIdLst", _NS))[page - 1].get(_R_ID)]
            root = etree.fromstring(z.read(target.lstrip("/") if target.startswith("/") else "ppt/" + target))
    except Exception:
        return []
    main = root.xpath(".//p:cTn[@nodeType='mainSeq']", namespaces=_NS)
    out: List[Dict[str, Any]] = []
    for eff in (main[0].xpath(".//p:cTn[@presetClass]", namespaces=_NS) if main else []):
        tgt = eff.find(".//p:spTgt", _NS)
        motion = eff.find(".//p:animMotion", _NS)
        out.append({"cls": eff.get("presetClass") or "", "sub": int(eff.get("presetSubtype") or 0),
                    "spid": int(tgt.get("spid")) if tgt is not None else 0,
                    "path": (motion.get("path") or "") if motion is not None else "",
                    "accel": float(eff.get("accel") or 0) / 100000, "decel": float(eff.get("decel") or 0) / 100000})
    return out


def enrich(effects: List[Effect], xml: List[Dict[str, Any]]) -> bool:
    """Add what only the file tells to PowerPoint's own list of effects. Only when the two lists match one for one (same targets in the same order)."""
    if not xml or len(xml) != len(effects) or any(x["spid"] and e.shape_id and x["spid"] != e.shape_id for x, e in zip(xml, effects)):
        return False
    for e, x in zip(effects, xml):
        e.cls, e.direction = x["cls"], side_of(x["sub"])
        e.path, e.accel, e.decel = x["path"], x["accel"], x["decel"]
    return True


def _tr(sh) -> Any:
    return sh.TextFrame2.TextRange


def read_shapes(slide) -> List[ShapeInfo]:
    """Top-level shapes in z-order (back first) with their text."""
    out: List[ShapeInfo] = []
    for i in range(1, slide.Shapes.Count + 1):
        sh = slide.Shapes(i)
        text, paras = "", []
        try:
            if sh.HasTextFrame:
                tr = _tr(sh)
                text = tr.Text.strip()
                paras = [tr.Paragraphs.Item(p).Text.strip() for p in range(1, int(tr.Paragraphs.Count) + 1)]
        except Exception:
            text, paras = "", []
        if not text:
            text = _group_text(sh)
        try:
            media = int(sh.Type) == 16                   # msoMedia
        except Exception:
            media = False
        out.append(ShapeInfo(text, paras, media, bool(sh.Visible)))
    return out


def _group_text(sh) -> str:
    """Text inside a group or a table (they are animated as one)."""
    parts: List[str] = []
    try:
        if int(sh.Type) == 6:                            # msoGroup
            for k in range(1, int(sh.GroupItems.Count) + 1):
                item = sh.GroupItems(k)
                if item.HasTextFrame:
                    parts.append(_tr(item).Text.strip())
        elif sh.HasTable:
            tb = sh.Table
            for r in range(1, int(tb.Rows.Count) + 1):
                for c in range(1, int(tb.Columns.Count) + 1):
                    parts.append(_tr(tb.Cell(r, c).Shape).Text.strip())
    except Exception:
        pass
    return "\n".join(t for t in parts if t)


def read_effects(slide) -> List[Effect]:
    """The main sequence of a slide (in the order PowerPoint plays it)."""
    seq = slide.TimeLine.MainSequence
    count = int(seq.Count)
    if count == 0:
        return []
    ids = {}
    for i in range(1, slide.Shapes.Count + 1):
        ids[int(slide.Shapes(i).Id)] = i - 1
    out: List[Effect] = []
    for k in range(1, count + 1):
        try:
            ef = seq.Item(k)
            idx = ids.get(int(ef.Shape.Id))
            if idx is None:
                continue
            para = 0
            try:
                if int(ef.EffectInformation.BuildByLevelEffect) != 0:
                    para = int(ef.Paragraph)
            except Exception:
                para = 0
            try:
                background = int(ef.EffectInformation.AnimateBackground) != 0
            except Exception:
                background = True
            t = ef.Timing
            out.append(Effect(idx, int(ef.EffectType), bool(ef.Exit), int(t.TriggerType), float(t.TriggerDelayTime),
                              float(t.Duration), max(0, para), background, shape_id=int(ef.Shape.Id)))
        except Exception:
            continue
    return out


class _Stage:
    """Shows only some layers of a slide (in the copy of the slide that is exported; nothing is ever saved)."""

    def __init__(self, slide, shapes: List[Any], infos: List[ShapeInfo]):
        self.slide, self.shapes, self.infos = slide, shapes, infos
        self.paragraph_shapes: Dict[int, Tuple[bool, bool]] = {}          # shape -> its fill and line were visible
        self.shown: Set[int] = set()                                       # the caller has hidden every shape to begin with

    def show(self, layer: Layer) -> None:
        """Show just this layer (only the shapes that change are touched: every COM call costs time)."""
        want = set(layer.shapes)
        for s in self.shown - want:
            self.shapes[s].Visible = 0
        for s in layer.shapes:
            sh = self.shapes[s]
            if layer.paras is not None:
                self._text_only(s, sh, layer.paras)
            if s not in self.shown:
                sh.Visible = -1
        self.shown = want

    def _text_only(self, s: int, sh, paras: List[int]) -> None:
        """Show only the given paragraphs (and the shape's own fill and outline only when there are none: that is the shape itself)."""
        if s not in self.paragraph_shapes:
            try:
                self.paragraph_shapes[s] = (bool(sh.Fill.Visible), bool(sh.Line.Visible))
            except Exception:
                self.paragraph_shapes[s] = (False, False)
        fill, line = self.paragraph_shapes[s]
        try:
            sh.Fill.Visible = -1 if (fill and not paras) else 0
            sh.Line.Visible = -1 if (line and not paras) else 0
        except Exception:
            pass
        tr = _tr(sh)
        for p in range(1, int(tr.Paragraphs.Count) + 1):
            para = tr.Paragraphs.Item(p)
            hide = 0.0 if p in paras else 1.0
            para.Font.Fill.Transparency = hide
            try:
                para.ParagraphFormat.Bullet.Font.Fill.Transparency = hide
            except Exception:
                pass

    def solid_background(self, rgb: int) -> None:
        self.slide.FollowMasterBackground = 0
        self.slide.Background.Fill.Solid()
        self.slide.Background.Fill.ForeColor.RGB = rgb


def export_slide(slide, page: int, W: int, H: int, out_dir: Path, src: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Export a slide with animations: its background, and one transparent image for each layer, in PowerPoint's own z-order.
    src = the .pptx file (read for the direction of wipes and motion paths). Returns {"clean": file, "mode": "timeline", "items": [...],
    "media": bool} or None when the slide has nothing to reveal."""
    effects = read_effects(slide)
    if not any(classify(e.etype, e.exit) == "in" for e in effects):
        return None                                       # nothing comes in: not even worth reading the shapes
    if src is not None and str(src).lower().endswith(".pptx"):
        enrich(effects, xml_effects(src, page))
    infos = read_shapes(slide)
    an = analyze(effects, infos)
    if an is None:
        return None
    work = slide.Duplicate()(1)                           # everything below changes this copy only, which is deleted at the end
    clean = out_dir / f"p{page:03d}_clean.png"
    black, white = out_dir / f"p{page:03d}_black.png", out_dir / f"p{page:03d}_white.png"
    made: List[Path] = []                                 # files written; removed again when the slide is left to the grouping by position
    result: Optional[Dict[str, Any]] = None
    try:
        shapes = [work.Shapes(i) for i in range(1, work.Shapes.Count + 1)]
        stage = _Stage(work, shapes, infos)
        for sh in shapes:
            sh.Visible = 0
        work.Export(str(clean.resolve()), "PNG", W, H)                     # background and what the master draws
        made.append(clean)
        work.DisplayMasterShapes = 0
        items: List[Dict[str, Any]] = []
        for k, (ly, meta) in enumerate(zip(an.layers, to_meta(an)), 1):
            stage.show(ly)
            stage.solid_background(0x000000)
            work.Export(str(black.resolve()), "PNG", W, H)
            stage.solid_background(0xFFFFFF)
            work.Export(str(white.resolve()), "PNG", W, H)
            with Image.open(black) as ib, Image.open(white) as iw:
                got = matte(ib, iw)
            if got is None:
                continue                                  # nothing visible (an empty shape, or a text box without its text)
            img, x, y = got
            name = f"p{page:03d}_l{k:02d}.png"
            img.save(out_dir / name)
            made.append(out_dir / name)
            items.append({"file": name, "x": x, "y": y, **meta})
        if any(it["beat"] >= 0 for it in items):
            result = {"clean": clean.name, "mode": "timeline", "items": items, "media": an.media}
        return result
    finally:
        black.unlink(missing_ok=True)
        white.unlink(missing_ok=True)
        if result is None:
            for f in made:
                f.unlink(missing_ok=True)
        try:
            work.Delete()
        except Exception:
            pass


# ---- the units the narration is lined up with --------------------------------------------

def units(items: Sequence[Any], only_after_media: bool = False) -> List[List[int]]:
    """Which items belong together as one thing the narration talks about, in the order they appear.
    Items grouped by position: one unit each. Timeline layers: one unit per beat (the card, its text and its badge are one step)."""
    if not any(getattr(it, "beat", -1) >= 0 for it in items):
        return [[k] for k in range(len(items))]
    by_beat: Dict[int, List[int]] = {}
    for k, it in enumerate(items):
        if it.beat >= 0 and (it.after_media or not only_after_media):
            by_beat.setdefault(it.beat, []).append(k)
    return [by_beat[b] for b in sorted(by_beat)]


def unit_texts(items: Sequence[Any], only_after_media: bool = False) -> List[str]:
    return ["\n".join(items[k].text.strip() for k in unit if items[k].text.strip())
            for unit in units(items, only_after_media)]


def to_meta(a: Analysis) -> List[Dict[str, Any]]:
    """The layers as plain dictionaries (files and positions are added when they have been exported)."""
    out = []
    for ly in a.layers:
        d: Dict[str, Any] = {"text": ly.text, "media": ly.media, "after_media": ly.after_media, "beat": -1, "offset": 0.0,
                             "anim": "fade", "exit_beat": -1, "exit_offset": 0.0, "dur": round(ly.dur, 3), "side": ly.side}
        if ly.enter is not None:
            d["beat"], d["offset"], d["anim"] = ly.enter[0], round(ly.enter[1], 3), ly.enter[2]
        if ly.exit is not None:
            d["exit_beat"], d["exit_offset"] = ly.exit[0], round(ly.exit[1], 3)
        if ly.motion is not None:
            d.update(motion_beat=ly.motion[0], motion_offset=round(ly.motion[1], 3), motion_dur=round(ly.motion[2], 3),
                     motion_path=ly.motion[3], motion_accel=ly.motion[4], motion_decel=ly.motion[5])
        out.append(d)
    return out
