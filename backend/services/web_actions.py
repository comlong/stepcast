"""What happens on an HTML slide while its narration plays: clicks on its controls, its builds (→), highlights on what is talked about.

Speaker notes of interactive decks carry stage directions in brackets, e.g. "(click the button)", "（依次点 1 到 4 号热点）" —
instructions for the presenter, never spoken. This module
  * finds them (`find_cues`) and takes them out of the text that is read aloud (`strip_cues`, also used for PowerPoint notes);
  * places actions: a direction becomes clicks on the controls it names (by their text, by the kind of control — "the cards",
    "hotspots 1 to 4" —, or the slide's only / main button), each at the point of the narration that talks about that control,
    or where the direction stood. A slide without directions gets clicks only where the narration plainly names a control;
  * highlights a piece of the slide (a card, a list item …; read from the page's structure) where the narration names it;
  * turns a place in the narration into a second of the voice-over.
Positions are character offsets into the narration; a quote of the text there finds the place again after the narration was edited.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import Step, WebAction, WebBlock, WebControl

# ---- stage directions in the notes --------------------------------------------------------------------------------------------

_OPEN, _CLOSE = "（(【[［", "）)】]］"
_CUE = re.compile(r"[（(【\[［]\s*([^（）()【】\[\]［］\n]{1,90}?)\s*[）)】\]］]")
_LEAD_IN = re.compile(r"^(?:然后|再|先|接着|现在|此时|这时|此处|这里|在这里|在此|then|now|and|here)\s*[,，:：]?\s*", re.I)  # i18n: ignore
# starts of a direction, in the interface languages: verbs of clicking / showing, never of content ("按照" = "according to")
_DIRECTION = re.compile(
    r"^(?:点击|点按|点一下|点开|点|单击|双击|轻点|按下|按(?!照|时|期|需|月|年|天|日|季|计划|顺序)|依次|逐个|逐一|挨个|分别点|再点|翻面|翻页|翻到|"  # i18n: ignore
    r"翻开|切换到|切到|演示|悬停|鼠标|拖动|拖到|左滑|右滑|上滑|下滑|滑动|停顿|暂停|看效果|动画|下一步|下一页|"  # i18n: ignore
    r"播放(?=动画|视频|演示)|等待|稍等|展开|收起|"  # i18n: ignore
    # whole words only: "(Drucker …)" isn't "(druk …)"
    r"(?:click|tap|press|hover|flip|toggle|demo|pause|next|advance|animate|animation|swipe|"
    r"klick|klicken|anklicken|nacheinander|tippen|weiter|clic|cliquer|cliquez|appuyer|appuyez|survoler|haz clic|hacer clic|"
    r"haga clic|pulsa|pulsar|pulse|toca|clicca|cliccare|premi|tocca|klik|klikken|tik|druk|kliknij|naciśnij)\b)",
    re.I)
_SEQUENCE = re.compile(r"依次|逐个|逐一|一个个|一个一个|挨个|分别|轮流|顺序|each|one by one|in turn|in order|nacheinander|"  # i18n: ignore
                       r"tour à tour|l'un après l'autre|uno por uno|uno alla volta|één voor één|po kolei", re.I)


class Cue:
    """A stage direction found in the notes: [start, end) in the notes, and its text without the brackets."""
    __slots__ = ("start", "end", "body")

    def __init__(self, start: int, end: int, body: str):
        self.start, self.end, self.body = start, end, body

    def __repr__(self) -> str:
        return f"Cue({self.start}, {self.end}, {self.body!r})"


def find_cues(text: str) -> List[Cue]:
    out = []
    for m in _CUE.finditer(text or ""):
        body = m.group(1).strip()
        if _DIRECTION.match(_LEAD_IN.sub("", body)):
            out.append(Cue(m.start(), m.end(), body))
    return out


def strip_cues(text: str) -> str:
    """The text without its stage directions (and without the gap they leave), for reading aloud."""
    text = text or ""
    cues = find_cues(text)
    if not cues:
        return text
    parts, last = [], 0
    for c in cues:
        parts.append(text[last:c.start])
        last = c.end
    parts.append(text[last:])
    out = "".join(parts)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", out)
    return re.sub(r"[ \t]+\n", "\n", out).strip()


# ---- normalised text ----------------------------------------------------------------------------------------------------------

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # i18n: ignore


def _norm(text: str) -> Tuple[str, List[int]]:
    """Letters, digits and CJK characters only (lowercased), and where each one was in the original."""
    out, idx = [], []
    for i, ch in enumerate(text or ""):
        if ch.isalnum():
            out.append(ch.lower())
            idx.append(i)
    return "".join(out), idx


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


# a control's text that says nothing about what it is
_GENERIC = re.compile(r"^(?:点击翻面|点击|翻面|再点[一下次]*(?:翻回|返回)?|点我|查看|详情|更多|按钮|click|tap|here|more|button|open|close|ok|go)$",  # i18n: ignore
                      re.I)
_SPLIT = re.compile(r"[·•|/\\:：,，;；。.!！?？()（）\[\]【】「」『』“”\"'‘’<>《》→←›‹»«▶►◀▸▾▲▼✓✔✗×+＋\-–—…~～\n\t]+")
_NUMBERING = re.compile(r"^(?:\d{1,2}|[①-⑳])(?=\D)")


def phrases(c: WebControl) -> List[str]:
    """What the narration may call a control: pieces of its label and text, without numbering, emoji and generic words.
    Chinese / Japanese / Korean words are also split at spaces ("产品路线图 第三季度" → two names); other languages keep their
    words together ("List view" is one name: "view" alone would also match "views")."""
    out: List[str] = []
    for src in (c.name, c.text):
        for chunk in _SPLIT.split(src or ""):
            for part in (chunk.split() if _CJK.search(chunk) else [chunk]):
                p = "".join(ch for ch in part if ch.isalnum() or ch in " &'")
                p = _NUMBERING.sub("", p.strip()).strip()
                n, _ = _norm(p)
                if not n or n.isdigit() or _GENERIC.match(p):
                    continue
                if (_CJK.search(n) and len(n) >= 2) or (not _CJK.search(n) and len(n) >= 3):
                    if n not in out:
                        out.append(n)
    return out


# ---- the narration in units ---------------------------------------------------------------------------------------------------

_SENT_RE = re.compile(r"[。！？；!?;]+|\.(?=\s|$)|\n+")
_ORDINAL = re.compile(r"第\s*[一二三四五六七八九十两\d]+\s*(?:步|点|个|条|项|件|种)|首先|其次|再次|最后|"  # i18n: ignore
                      r"\b(?:first(?:ly)?|second(?:ly)?|third(?:ly)?|fourth|fifth|sixth|finally|step\s*\d+)\b", re.I)


def units(narration: str) -> List[Tuple[int, int]]:
    """Sentences (and the clauses of a long one, split at ；) as [start, end) offsets."""
    out: List[Tuple[int, int]] = []
    start = 0
    for m in _SENT_RE.finditer(narration or ""):
        if narration[start:m.start()].strip():
            out.append((start, m.end()))
        start = m.end()
    if (narration or "")[start:].strip():
        out.append((start, len(narration)))
    # trim leading spaces
    return [(a + len(narration[a:b]) - len(narration[a:b].lstrip()), b) for a, b in out]


def _unit_of(pos: int, us: Sequence[Tuple[int, int]]) -> int:
    for k, (a, b) in enumerate(us):
        if pos < b:
            return k
    return max(0, len(us) - 1)


_FILLER = set("个的了一把在将这那些地得着")  # i18n: ignore


def _longest_common(a: str, b: str) -> Tuple[int, int]:
    """Length of the longest common substring of a and b, and where it starts in b."""
    best, at = 0, -1
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, at = cur[j], j - cur[j]
        prev = cur
    return best, at


def _subsequence(phrase: str, text: str) -> int:
    """Start of the shortest stretch of text holding the phrase's characters in order (filler words of the phrase may be missing),
    at most twice as long as the phrase; -1 if none."""
    core = "".join(ch for ch in phrase if ch not in _FILLER) or phrase
    if len(core) < 2:
        return -1
    best, at = None, -1
    for s in (i for i, ch in enumerate(text) if ch == core[0]):
        j, k = s, 0
        while j < len(text) and k < len(core) and j - s < 2 * len(phrase):
            if text[j] == core[k]:
                k += 1
            j += 1
        if k == len(core) and (best is None or j - s < best):
            best, at = j - s, s
    return at


def find_phrase(narration: str, phrase: str, start: int = 0) -> Optional[Tuple[int, float, bool]]:
    """Where the narration names `phrase` (a normalised phrase) at or after `start`: (offset in the narration, strength, clear).
    Exact = strength = the phrase's length. Otherwise (CJK) a long common stretch, the phrase's characters in order with filler
    words left out ("做个备份" ~ "做备份"), or a clause sharing most of its character pairs; (others) most of its words."""
    norm, idx = _norm(narration)
    if not norm or not phrase:
        return None
    # normalised start
    ns = next((k for k, i in enumerate(idx) if i >= start), len(norm))
    hit = norm.find(phrase, ns)
    if hit >= 0:
        return idx[hit], float(len(phrase)), True
    us = [u for u in units(narration) if u[1] > start]
    best: Optional[Tuple[int, float, bool]] = None

    def keep(pos: int, score: float, clear: bool = False) -> None:
        nonlocal best
        if best is None or score > best[1] + 1e-9:
            best = (pos, score, clear)
    if _CJK.search(phrase):
        common, at = _longest_common(phrase, norm[ns:])
        if common >= 4 and common >= 0.6 * len(phrase):
            keep(idx[ns + at], float(common), True)              # "每周自动生成报表" ~ "每周自动生成"
        if len(phrase) <= 6:
            s = _subsequence(phrase, norm[ns:])
            if s >= 0:
                keep(idx[ns + s], len(phrase) * 0.9)
        pb = _bigrams(phrase)
        if len(pb) >= 2:
            for a, b in us:
                ub = _bigrams(_norm(narration[max(a, start):b])[0])
                shared = len(pb & ub)
                ratio = shared / len(pb)
                if shared >= 2 and ratio >= 0.5:
                    keep(max(a, start), ratio * len(phrase) * 0.8)
    else:
        pw = [w for w in re.findall(r"[a-z0-9]+", phrase) if len(w) >= 3] or [phrase]
        if len(pw) < 2:
            return None
        for a, b in us:
            uw = set(re.findall(r"[a-z0-9]+", narration[max(a, start):b].lower()))
            shared = sum(1 for w in pw if w in uw or any(x.startswith(w[:5]) for x in uw if len(w) >= 5))
            if shared >= 2 and shared / len(pw) >= 0.6:
                keep(max(a, start), shared / len(pw) * len(phrase) * 0.8)
    return best


_CLAUSE = re.compile(r"[，,、：:；;。！？!?]|\.(?=\s)|\n")


def clause_start(narration: str, pos: int) -> int:
    """Start of the clause (between commas / full stops) that holds pos: an action belongs where its clause begins."""
    a = 0
    for m in _CLAUSE.finditer(narration or ""):
        if m.end() > pos:
            break
        a = m.end()
    while a < pos and narration[a].isspace():
        a += 1
    return a


def mention(narration: str, c: WebControl, start: int = 0, strong: bool = False) -> Optional[int]:
    """The first place (at or after `start`) where the narration names control `c`, as the start of that clause;
    strong = only clear mentions (a phrase of it word for word, or most of a long one)."""
    found: Optional[int] = None
    for p in phrases(c):
        r = find_phrase(narration, p, start)
        if r is None:
            continue
        pos, score, clear = r
        need = 2.0 if _CJK.search(p) else 4.0
        if strong and not clear:
            continue
        if score < need * 0.8:
            continue
        if found is None or pos < found:
            found = pos
    return None if found is None else max(start, clause_start(narration, found))


# ---- what a direction points at -----------------------------------------------------------------------------------------------

# kinds of control a direction may name, and words in the controls' classes / ids / groups that mark them
_NOUNS: Sequence[Tuple[Tuple[str, ...], Tuple[str, ...]]] = (
    (("热点", "hotspot", "hot spot", "标记点", "marker", "pin"), ("hot", "spot", "pin", "marker", "beacon")),  # i18n: ignore
    (("卡片", "卡牌", "card", "tile", "翻面", "flip"), ("card", "flip", "tile")),  # i18n: ignore
    (("把手", "拉手", "handle", "抽屉", "drawer", "侧边条"), ("handle", "grip", "drawer", "pull")),  # i18n: ignore
    (("节点", "node", "里程碑", "milestone", "时间轴", "timeline"), ("node", "milestone", "tl-", "timeline")),  # i18n: ignore
    (("标签", "选项卡", "页签", "tab"), ("tab", "chip")),  # i18n: ignore
    (("场景", "scenario", "scene", "例子", "case"), ("scen", "case", "example")),  # i18n: ignore
    (("模式", "mode"), ("mode",)),  # i18n: ignore
    (("选项", "option", "choice"), ("opt", "option", "choice")),  # i18n: ignore
    (("开关", "switch", "toggle"), ("switch", "toggle")),  # i18n: ignore
    (("步骤", "step"), ("step",)),  # i18n: ignore
    (("图标", "icon"), ("icon",)),  # i18n: ignore
    (("菜单", "menu"), ("menu",)),  # i18n: ignore
)
_BUTTON_WORDS = ("按钮", "button", "btn", "knopf", "bouton", "botón", "pulsante", "knop", "przycisk")  # i18n: ignore
_PARTS = re.compile(r"[，,、；;]|和|与|及|以及|然后|再|\band\b|\bthen\b", re.I)  # i18n: ignore
_RANGE = re.compile(r"(\d{1,2}|[①-⑳])\s*(?:到|至|-|–|~|～|to)\s*(\d{1,2}|[①-⑳])", re.I)  # i18n: ignore
_NUM = re.compile(r"(?<!\d)(\d{1,2})(?!\d)|[①-⑳]")


def _num(s: str) -> int:
    return ord(s) - ord("①") + 1 if "①" <= s <= "⑳" else int(s)


def _numbers(text: str) -> List[int]:
    m = _RANGE.search(text)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if 0 < a <= b <= 30:
            return list(range(a, b + 1))
    return [_num(x.group(0)) for x in _NUM.finditer(text) if 0 < _num(x.group(0)) <= 30]


def _haystack(c: WebControl) -> str:
    return " ".join((c.cls, c.group, c.path)).lower()


def _primary(controls: Sequence[WebControl]) -> Optional[int]:
    """The slide's main button: its only control, or the one button that starts something (▶ …) or is styled as the button."""
    if len(controls) == 1:
        return 0
    starts = [k for k, c in enumerate(controls) if re.match(r"^\s*[▶►▷→⏵⯈]", c.text or "")]
    if len(starts) == 1:
        return starts[0]
    groups: Dict[str, int] = {}
    for c in controls:
        groups[c.group] = groups.get(c.group, 0) + 1
    lone = [k for k, c in enumerate(controls) if groups[c.group] == 1 and re.search(r"(^|[\s-])(btn|button|primary|cta|run|start|play)", _haystack(c))]
    return lone[0] if len(lone) == 1 else None


def _targets(part: str, controls: Sequence[WebControl]) -> List[int]:
    """Controls one part of a direction points at, in order."""
    low = part.lower()
    pn, _ = _norm(part)
    # 1. named by their own text ("点「运行」", "click Run")
    named = [k for k, c in enumerate(controls) if any(len(p) >= 2 and p in pn for p in phrases(c))]
    if named:
        return named
    nums = _numbers(part)
    # 2. a kind of control ("the cards", "hotspots 1 to 4")
    kinds: List[int] = []
    for words, marks in _NOUNS:
        if any(w in low for w in words):
            kinds = [k for k, c in enumerate(controls) if any(m in _haystack(c) for m in marks)]
            if kinds:
                break
    if nums:
        pool = kinds or list(range(len(controls)))
        by_num = {}
        for k in pool:
            t = (controls[k].text or "").strip()
            if re.fullmatch(r"\d{1,2}|[①-⑳]", t):
                by_num[_num(t)] = k
        hit = [by_num[n] for n in nums if n in by_num]
        if hit:
            return hit
        if kinds and len(nums) == 1 and 0 < nums[0] <= len(kinds):
            return [kinds[nums[0] - 1]]               # "the 2nd card"
    if kinds:
        return kinds
    # 3. "the button" / "click": the slide's main button
    if any(w in low for w in _BUTTON_WORDS) or _DIRECTION.match(_LEAD_IN.sub("", part.strip())):
        k = _primary(controls)
        return [k] if k is not None else []
    return []


# ---- placing the actions ------------------------------------------------------------------------------------------------------

def _cue_positions(notes: str, narration: str) -> List[Tuple[Cue, int]]:
    """Each direction of the notes and the place in the narration it belongs to (where the text after it starts).
    The narration is the notes read aloud (maybe tidied up a little, maybe rewritten): the text before the direction is looked up."""
    out = []
    norm_n, idx_n = _norm(narration)
    clean_before = 0
    for c in find_cues(notes):
        before = _norm(strip_cues(notes[:c.start]))[0]
        pos: Optional[int] = None
        if not before:
            pos = 0
        else:
            for size in (24, 14, 8, 5):
                tail = before[-size:]
                if len(tail) < min(size, 4):
                    continue
                hit = norm_n.find(tail, clean_before if clean_before < len(norm_n) else 0)
                if hit < 0:
                    hit = norm_n.find(tail)
                if hit >= 0:
                    end = hit + len(tail)
                    pos = idx_n[end] if end < len(idx_n) else len(narration)
                    clean_before = end
                    break
        if pos is None:                       # rewritten narration: the same share of the way through
            pos = int(len(narration) * len(before) / max(1, len(_norm(strip_cues(notes))[0])))
        # the place: the start of the next sentence (or the end)
        us = units(narration)
        nxt = next((a for a, b in us if a >= pos - 1), len(narration))
        out.append((c, min(len(narration), max(0, nxt if nxt - pos <= 3 else pos))))
    return out


def _fill(positions: List[Optional[int]], lo: int, hi: int) -> List[int]:
    """Places not found are spread between the found ones around them (lo / hi bound the run)."""
    out = list(positions)
    n = len(out)
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        a = out[i - 1] if i > 0 else lo
        b = out[j] if j < n else hi
        for k in range(i, j):
            out[k] = int(a + (b - a) * (k - i + 1) / (j - i + 1))
        i = j
    res = [int(x) for x in out]                  # type: ignore[arg-type]
    for k in range(1, n):
        res[k] = max(res[k], res[k - 1])
    return res


def quote(narration: str, pos: int) -> str:
    return (narration or "")[pos:pos + 24].strip()


# words too general to say which card the narration means
_BLOCK_GENERIC = re.compile(r"^(?:第?[一二三四五六七八九十两\d]+[个条项点步]?|步骤|示例|例子|说明|注意|提示|结果|总结|小结|问题|答案|方法|优点|缺点|优势|"  # i18n: ignore
                            r"特点|背景|目标|内容|其他|其它|介绍|概述|step\d*|note|notes|example|examples|tip|tips|result|results|"  # i18n: ignore
                            r"summary|overview|more|other|intro|introduction)$", re.I)


def block_phrases(b: WebBlock) -> List[str]:
    """What the narration may call a piece of content: its name and the opening of its text, without numbering and general words."""
    out: List[str] = []
    first = _SPLIT.split(b.text or "", maxsplit=1)[0]
    for src in (b.key, first):
        for p in phrases(WebControl(text=src or "")):
            if not _BLOCK_GENERIC.match(p) and p not in out:
                out.append(p)
    return sorted(out, key=len, reverse=True)


def _whole_word(narration: str, pos: int, phrase: str) -> bool:
    """A name in a language with spaces must stand as a word of its own: "art" isn't said in "start"."""
    if _CJK.search(phrase):
        return True
    norm, idx = _norm(narration)
    try:
        at = idx.index(pos)
    except ValueError:
        return False
    if norm[at:at + len(phrase)] != phrase:          # (found by similarity, not word for word)
        return False
    end = idx[at + len(phrase) - 1] + 1
    return (pos == 0 or not narration[pos - 1].isalnum()) and (end >= len(narration) or not narration[end].isalnum())


def _focus(narration: str, blocks: Sequence[WebBlock], taken: Sequence[WebAction]) -> List[WebAction]:
    """A highlight wherever the narration plainly names a piece of the slide — only there: a wrong highlight is worse than none. A
    sentence in which the page itself does something (a click, a build) is left alone: the page already shows where to look."""
    if not narration.strip() or not blocks:
        return []
    us = units(narration)
    busy = {_unit_of(a.pos, us) for a in taken}
    owners: Dict[str, set] = {}
    for k, b in enumerate(blocks):
        for p in block_phrases(b):
            owners.setdefault(p, set()).add(k)
    hits: List[Tuple[int, int, int]] = []                # (place, -length of the name, block)
    for k, b in enumerate(blocks):
        best: Optional[Tuple[int, int]] = None
        for p in block_phrases(b):
            if len(owners[p]) > 1:                       # two pieces with this name: which one is meant can't be told
                continue
            r = find_phrase(narration, p)
            if r is None or not r[2] or not _whole_word(narration, r[0], p):
                continue
            if best is None or r[0] < best[0]:
                best = (r[0], len(p))
        if best is not None:
            hits.append((best[0], -best[1], k))
    out: List[WebAction] = []
    last_end = -1
    last_at = -1
    for pos, neg_len, k in sorted(hits):
        if pos < last_end or _unit_of(pos, us) in busy:   # (a longer name found at this place comes first)
            continue
        last_end = pos - neg_len
        at = clause_start(narration, pos)
        if at <= last_at:                                 # the same clause names another one ("A and B"): where its name is said
            at = pos
        last_at = at
        out.append(WebAction(kind="focus", control=k, pos=at, quote=quote(narration, at)))
    return out


def plan(narration: str, notes: str, controls: Sequence[WebControl], builds: int = 0,
         build_texts: Sequence[str] = (), blocks: Sequence[WebBlock] = ()) -> List[WebAction]:
    """Actions for one HTML slide, in time order (see the module docstring)."""
    narration = narration or ""
    acts: List[WebAction] = []
    end = len(narration)
    cues = _cue_positions(notes or "", narration) if notes else []
    used: set = set()
    for cue, anchor in cues:
        parts = [p for p in _PARTS.split(cue.body) if p.strip()] or [cue.body]
        seq = bool(_SEQUENCE.search(cue.body))
        items: List[Tuple[int, Optional[int]]] = []        # (control, place)
        for part in parts:
            tg = _targets(part, controls)
            if not tg:
                continue
            if len(tg) == 1 and seq and len(parts) == 1:
                # one control clicked again and again ("click Next in turn"): at each counted point of the narration
                # ("first …, second …"), from the second one on
                ords = [m.start() for m in _ORDINAL.finditer(narration)]
                marks = sorted({units(narration)[_unit_of(p, units(narration))][0] for p in ords})
                if len(marks) >= 2:
                    items += [(tg[0], p) for p in marks[1:]]
                    continue
            for k in tg:
                if k in used and len(tg) > 1:
                    continue
                # a mention of the control near the direction: the narration's own words decide the moment.
                # The direction comes after the text it belongs to, so look before it as well
                lo = 0 if anchor >= end - 2 or len(tg) > 1 else max(0, anchor - 400)
                place = mention(narration, controls[k], lo)
                if place is None:
                    # the kind of control is named in the narration ("… the handle on the side …")
                    for words, _marks in _NOUNS:
                        for w in words:
                            if w.lower() in part.lower():
                                hit = narration.lower().find(w.lower(), lo)
                                if hit >= 0:
                                    place = units(narration)[_unit_of(hit, units(narration))][0]
                                    break
                        if place is not None:
                            break
                if place is not None and len(tg) == 1 and not seq and place > anchor:
                    place = None                    # a single click belongs where the direction stands
                items.append((k, place))
        if not items:
            continue
        found = [p for _, p in items if p is not None]
        if not found:
            places = [anchor] * len(items)
        else:
            # controls the narration doesn't name go between their neighbours, on the direction's side of the text
            lo = 0 if anchor >= end - 2 or min(found) < anchor else anchor
            hi = anchor if max(found) < anchor else end
            places = _fill([p for _, p in items], lo, hi)
            us = units(narration)
            places = [p if p in found else next((a for a, _b in us if a >= p), p) for p in places]
        for (k, _), pos in zip(items, places):
            used.add(k)
            acts.append(WebAction(kind="click", control=k, pos=pos, quote=quote(narration, pos)))
    if not cues:
        # no directions: click only what the narration plainly names, once each, one control per name
        seen_at: Dict[Tuple[int, str], bool] = {}
        for k, c in enumerate(controls):
            pos = mention(narration, c, 0, strong=True)
            if pos is None:
                continue
            key = (pos, (phrases(c) or [""])[0])
            if key in seen_at:
                continue
            seen_at[key] = True
            acts.append(WebAction(kind="click", control=k, pos=pos, quote=quote(narration, pos)))
    if builds > 0:
        texts = list(build_texts)[:builds] + [""] * max(0, builds - len(build_texts))
        found: List[Optional[int]] = []
        start = 0
        for t in texts:
            hit = None
            for p in sorted({_norm(x)[0] for x in re.split(r"[\n。.;；]+", t) if len(_norm(x)[0]) >= 3}, key=len, reverse=True):
                r = find_phrase(narration, p[:12], start)
                if r is not None:
                    hit = clause_start(narration, r[0])
                    break
            found.append(hit)
            if hit is not None:
                start = hit
        if all(f is None for f in found):
            us = units(narration)
            # spread over the narration: the k-th build at the k-th of builds+1 equal parts
            found = [us[min(len(us) - 1, int(len(us) * (k + 1) / (builds + 1)))][0] if us else 0 for k in range(builds)]
        for pos in _fill(found, 0, end):
            acts.append(WebAction(kind="next", control=-1, pos=pos, quote=quote(narration, pos)))
    acts += _focus(narration, blocks, acts)
    acts.sort(key=lambda a: a.pos)
    return stamp(narration, acts)


def plan_key(step: Step) -> str:
    """Fingerprint of everything automatic actions are placed from."""
    w = step.web
    h = hashlib.sha1((step.narration or "").encode("utf-8"))
    h.update(b"\x00" + (step.slide_notes or "").encode("utf-8"))
    if w is not None:
        for c in w.controls:
            h.update(b"\x00" + "|".join((c.path, c.text, c.name, c.group)).encode("utf-8"))
        for b in w.blocks:
            h.update(b"\x01" + "|".join((b.path, b.key, b.text)).encode("utf-8"))
        h.update(f"\x00{w.builds}\x00{w.clicks}".encode())
    return h.hexdigest()[:16]


def relocate(narration: str, a: WebAction, same_units: bool = False) -> int:
    """Where a hand-placed action belongs in an edited narration: its quote looked up again; else (same_units: the narration
    still has as many sentences, e.g. translated) its sentence; else where it was, as far as the text still goes."""
    narration = narration or ""
    if 0 <= a.pos <= len(narration) and narration[a.pos:a.pos + 24].strip() == a.quote:
        return a.pos
    q = _norm(a.quote)[0]
    norm, idx = _norm(narration)
    for size in (len(q), 12, 8, 5):
        if size < 4 or size > len(q):
            continue
        hit = norm.find(q[:size])
        if hit >= 0:
            pos = idx[hit]
            us = units(narration)
            return units(narration)[_unit_of(pos, us)][0] if us else pos
    if same_units and a.unit >= 0:
        return _at_unit(narration, a.unit)
    return min(max(0, a.pos), len(narration))


def refresh(step: Step) -> bool:
    """Place the automatic actions again if the narration / notes changed since; hand-placed ones follow their quotes.
    Returns whether something changed."""
    w = step.web
    if w is None:
        return False
    key = plan_key(step)
    if w.manual:
        narration = step.narration or ""
        n_units = len(units(narration))
        changed = False
        for a in w.actions:
            p = relocate(narration, a, w.unit_count == n_units and n_units > 0)
            if p != a.pos:
                a.pos, a.quote, changed = p, quote(narration, p), True
        if changed:
            stamp(narration, w.actions)
        w.unit_count = n_units
        return changed
    if w.plan_key == key:
        return False
    narration = step.narration or ""
    new = plan(narration, step.slide_notes, w.controls if w.clicks else [], w.builds, w.build_texts, w.blocks)
    n_units = len(units(narration))
    if (w.actions and len(new) < len(w.actions) and w.unit_count == n_units and n_units
            and _script(narration) != _script(strip_cues(step.slide_notes or ""))):
        # the narration is now in another language than the notes (translated): the controls' names can't be found in it any
        # more, but it still has the same sentences — the clicks stay in their sentences
        for a in w.actions:
            p = _at_unit(narration, a.unit)
            a.pos, a.quote = p, quote(narration, p)
    else:
        w.actions = new
    w.plan_key, w.unit_count = key, n_units
    return True


def _script(text: str) -> str:
    """"cjk" for Chinese / Japanese / Korean text, else "latin" (enough to tell a translation into another script)."""
    letters = sum(1 for ch in text if ch.isalpha())
    return "cjk" if letters and len(_CJK.findall(text)) > 0.2 * letters else "latin"


def _at_unit(narration: str, unit: int) -> int:
    us = units(narration)
    if unit < 0 or not us:
        return 0
    return len(narration) if unit >= len(us) else us[unit][0]


def stamp(narration: str, acts: List[WebAction]) -> List[WebAction]:
    """Note each action's sentence (len(units) = after the narration)."""
    us = units(narration or "")
    end = len((narration or "").rstrip())
    for a in acts:
        a.unit = len(us) if a.pos >= end else _unit_of(a.pos, us)
    return acts


# ---- seconds ---------------------------------------------------------------------------------------------------------------

MIN_FIRST = 1.0          # nothing is clicked before the slide has come in
MIN_GAP = 0.9            # two actions are at least this far apart (the pointer needs time to travel)
AFTER_END = 0.4          # an action placed after the last word: this long after the voice-over ends


def pos_time(step: Step, pos: int, lead: float, total: float) -> float:
    """Second (from the start of the step) at which the narration reaches offset `pos`."""
    from .subtitles import _char_time_map, _time_at_char
    text = step.narration or ""
    audio = step.audio_duration if step.audio_duration > 0 else 0.0
    if pos >= len(text.rstrip()) or not text.strip():
        return lead + (audio if audio else max(0.0, total - lead - 0.6)) + AFTER_END
    if audio <= 0:
        span = max(0.5, total - lead - 0.6)
        return lead + span * pos / max(1, len(text))
    cmap = _char_time_map(step.boundaries) if step.boundaries else []
    t = _time_at_char(cmap, pos, len(text), audio, start=True) if cmap else audio * pos / max(1, len(text))
    return lead + max(0.0, t - 0.15)          # a moment early: the click lands as the words start


FOCUS_FIRST = 0.7        # a highlight comes after the slide has come in
FOCUS_GAP = 1.1          # a highlight stays at least this long before moving on (closer ones are left out)
FOCUS_CLEAR = 0.7        # no highlight this close to a click
FOCUS_MIN = 1.4          # how long a highlight stays, at least / at most, when nothing follows it
FOCUS_MAX = 9.0


def timeline(step: Step, lead: float, total: float, focus: bool = True) -> List[Tuple[float, WebAction]]:
    """The actions of a step with their seconds, in order: clicks and builds at least MIN_GAP apart, highlights (nothing has to travel
    to them) as the words come, each lasting to the end of its sentence or the next highlight (`until`). focus=False: no highlights."""
    w = step.web
    if w is None or not w.actions:
        return []
    out: List[Tuple[float, WebAction]] = []
    last = -1e9
    ordered = sorted(w.actions, key=lambda x: x.pos)
    for a in ordered:
        if a.kind == "focus" or (a.kind in ("click", "hover") and not (0 <= a.control < len(w.controls))):
            continue
        t = max(MIN_FIRST, pos_time(step, a.pos, lead, total) + max(0.0, a.delay))
        t = max(t, last + MIN_GAP)
        out.append((round(t, 3), a))
        last = t
    if not focus:
        return out
    spots: List[Tuple[float, WebAction]] = []
    for a in ordered:
        if a.kind != "focus" or not (0 <= a.control < len(w.blocks)):
            continue
        t = max(FOCUS_FIRST, pos_time(step, a.pos, lead, total) + max(0.0, a.delay))
        if t >= total - FOCUS_MIN or (spots and t - spots[-1][0] < FOCUS_GAP) or any(abs(t - c) < FOCUS_CLEAR for c, _ in out):
            continue
        spots.append((round(t, 3), a))
    us = units(step.narration or "")
    for k, (t, a) in enumerate(spots):
        end_pos = us[_unit_of(a.pos, us)][1] if us else 0
        said = pos_time(step, end_pos, lead, total) + 0.25
        nxt = spots[k + 1][0] if k + 1 < len(spots) else float("inf")
        until = min(nxt, max(said, t + FOCUS_MIN), t + FOCUS_MAX, total - 0.25)
        if nxt - until < 0.8:                    # no flicker: straight on to the next one
            until = min(nxt, total - 0.25)
        a.until = round(until, 3)
    return sorted(out + spots, key=lambda x: x[0])


