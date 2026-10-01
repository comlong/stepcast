"""Second-language subtitles: the main subtitle is burned into the video, the second language is an external subtitle file (WebVTT / SRT) the viewer picks (off by default).

- When rendering, the main subtitle timeline is saved as "<video>.cues.json" next to the video (older videos without it read the .srt)
- Generating a second language: main subtitle cues are merged into sentences (one cue is often half a sentence), the AI translates whole sentences, each keeping its time in the video;
  translations that are too long are split into two cues
- Files: "<video>.<lang>.vtt / .srt"; a web player package can also be exported: video + subtitles + a player page (subtitle choice and full screen work)
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .. import i18n, storage
from ..models import Project, SubtitleTrack
from .llm import ChatClient, LLMError, get_client

Progress = Optional[Callable[[float, str], None]]

CUES_SUFFIX = ".cues.json"
BATCH = 50                     # sentences per translation request
_END = re.compile(r"(?:[。！？!?；;…]|(?<![0-9])\.)[\"'”’」』）)]*$")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # i18n: ignore


# ---- main subtitle ---------------------------------------------------------------------

LEGACY_BOTTOM = 0.945          # videos rendered before 1.6.0 have the main subtitle's bottom edge at 94.5% of the frame height


def save_primary(out_dir: Path, video_name: str, cues: List[Any], language: str, burned: bool, space: bool,
                 bottom: float = 0.935) -> Path:
    """Called when rendering: store the main subtitle timeline and position (bottom edge as a fraction of the frame height), so second-language subtitles
    can be generated later without re-rendering and the player page can place the second subtitle right below the main one."""
    path = out_dir / (Path(video_name).stem + CUES_SUFFIX)
    data = {"version": 2, "video": video_name, "language": language, "burned": burned, "space": space,
            "bottom": round(bottom, 4),
            "cues": [[round(c.start, 3), round(c.end, 3), c.text] for c in cues if (c.text or "").strip()]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _parse_srt(text: str) -> List[List[Any]]:
    def ts(x: str) -> float:
        h, m, s = x.strip().replace(".", ",").split(":")
        sec, _, ms = s.partition(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int((ms or "0")[:3].ljust(3, "0")) / 1000

    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        rows = block.split("\n")
        k = next((i for i, r in enumerate(rows) if "-->" in r), -1)
        if k < 0:
            continue
        a, _, b = rows[k].partition("-->")
        try:
            out.append([ts(a), ts(b.split()[0]), " ".join(r.strip() for r in rows[k + 1:] if r.strip())])
        except (ValueError, IndexError):
            continue
    return out


def load_primary(proj: Project) -> Optional[Dict[str, Any]]:
    """Main subtitles of the current video. Videos rendered by old versions have no .cues.json: read the .srt and mark "no room for a second subtitle"."""
    if not proj.output:
        return None
    out_dir = storage.output_dir(proj.id)
    stem = Path(proj.output).stem
    p = out_dir / (stem + CUES_SUFFIX)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("cues"), list):
                d["legacy"] = False
                d.setdefault("bottom", 0.905 if d.get("space") else LEGACY_BOTTOM)     # rendered by 1.6.0
                return d
        except (OSError, ValueError):
            pass
    srt = out_dir / (stem + ".srt")
    if not srt.exists():
        return None
    return {"version": 0, "video": proj.output, "language": proj.language, "burned": True, "space": False,
            "bottom": LEGACY_BOTTOM, "legacy": True,
            "cues": _parse_srt(srt.read_text(encoding="utf-8", errors="replace"))}


def primary_key(primary: Dict[str, Any]) -> str:
    """Fingerprint of the main subtitles: after re-rendering (timeline changed) previously generated second-language subtitles are outdated."""
    return hashlib.sha1(json.dumps(primary.get("cues") or [], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _join(a: str, b: str) -> str:
    if not a:
        return b
    return a + ("" if _CJK.search(a[-1:]) or _CJK.search(b[:1]) else " ") + b


def group_sentences(cues: List[List[Any]]) -> List[Dict[str, Any]]:
    """Merge main subtitle cues into sentences: up to the sentence-ending punctuation; also break on pauses over 0.7 s, sentences over 14 s or too long."""
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for s, e, t in cues:
        t = str(t or "").strip()
        if not t:
            continue
        if cur and (s - cur["e"] > 0.7 or e - cur["s"] > 14 or len(cur["t"]) > 160):
            out.append(cur)
            cur = None
        if cur is None:
            cur = {"s": float(s), "e": float(e), "t": t, "cuts": []}
        else:
            cur["cuts"].append(float(s))                 # moment within the sentence where the main subtitle switches cue
            cur["e"] = float(e)
            cur["t"] = _join(cur["t"], t)
        if _END.search(t):
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    # very short sentences ("Right." — gone after a second) are merged into the next one so the second subtitle doesn't just flash
    merged: List[Dict[str, Any]] = []
    for x in out:
        prev = merged[-1] if merged else None
        if prev and prev["e"] - prev["s"] < 1.6 and x["s"] - prev["e"] <= 0.4 and x["e"] - prev["s"] <= 14:
            prev["cuts"] = prev["cuts"] + [x["s"]] + x["cuts"]
            prev["e"] = x["e"]
            prev["t"] = _join(prev["t"], x["t"])
        else:
            merged.append(dict(x))
    return merged


# ---- translation -----------------------------------------------------------------------

SUB_SYSTEM = """你是专业的视频字幕译员，把教学视频的字幕逐句翻译成目标语言，做成第二语言字幕。
规则：
1. 意思准确，简洁、口语化，适合在屏幕上一眼读完；不要解释，不要加原文没有的内容。
2. 产品名、型号、品牌名保持原文；界面上的按钮名、菜单名保留原文（需要时在括号里给译文）。
3. 每一句单独翻译成一句，不要合并相邻的句子，也不要漏句；上下文只用来理解意思。
4. 只输出 JSON。"""  # i18n: ignore


def _translate(client: ChatClient, sents: List[str], src: str, target: str, progress: Progress = None,
               label: str = "") -> List[str]:
    out: List[Optional[str]] = [None] * len(sents)

    def run(idx: List[int]) -> None:
        batches = [idx[i:i + BATCH] for i in range(0, len(idx), BATCH)]
        for bi, batch in enumerate(batches):
            if progress:
                progress(bi / max(1, len(batches)), label)
            items = [{"i": k, "t": sents[k]} for k in batch]
            prompt = (f"把下面每一句从{src}翻译成{target}。"  # i18n: ignore
                      '输出 JSON：{"items": [{"i": 编号, "t": "译文"}]}，条数、编号必须和输入一致。\n\n'  # i18n: ignore
                      + json.dumps(items, ensure_ascii=False, indent=0))
            data = client.chat_json([{"role": "system", "content": SUB_SYSTEM},
                                     {"role": "user", "content": prompt}], temperature=0.2)
            for it in (data.get("items") or []) if isinstance(data, dict) else []:
                try:
                    k = int(it.get("i"))
                except (TypeError, ValueError, AttributeError):
                    continue
                t = str(it.get("t") or "").strip()
                if k in batch and t:
                    out[k] = t

    run(list(range(len(sents))))
    missing = [k for k, t in enumerate(out) if t is None]
    if missing:                                   # ask once more for the missing ones
        run(missing)
    return [t if t is not None else sents[k] for k, t in enumerate(out)]


def _split(text: str, s: float, e: float, max_chars: int, cuts: Optional[List[float]] = None) -> List[List[Any]]:
    """Split translations that are too long into several cues, timed by length; if the main subtitle switches cue within 1.5 s of a split point,
    switch at that moment so both subtitles change together. Break at punctuation first, then at spaces."""
    if len(text) <= max_chars:
        return [[s, e, text]]
    n = math.ceil(len(text) / max_chars)
    parts: List[str] = []
    rest = text
    for k in range(n - 1, 0, -1):
        target = len(rest) // (k + 1)
        cut = -1
        for pat in (r"[，,、；;：:。！？!?]\s*", r"\s+"):
            cands = [m.end() for m in re.finditer(pat, rest) if 0 < m.end() < len(rest)]
            if cands:
                cut = min(cands, key=lambda x: abs(x - target))
                if abs(cut - target) <= max(8, target // 2):
                    break
        if cut <= 0:
            cut = target
        while 0 < cut < len(rest) and unicodedata.category(rest[cut])[0] == "M":
            cut += 1                                   # combining marks (Thai tone marks, Hindi vowel signs) stay with the preceding letter
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    parts.append(rest)
    parts = [p for p in parts if p]
    total = sum(len(p) for p in parts) or 1
    bounds, acc = [], 0
    for p in parts[:-1]:
        acc += len(p)
        bounds.append(s + (e - s) * acc / total)
    edges, lo = [s], s
    for b in bounds:
        near = [c for c in (cuts or []) if lo + 0.6 <= c <= e - 0.6 and abs(c - b) <= 1.5]
        b = min(near, key=lambda c: abs(c - b)) if near else min(max(b, lo + 0.3), e - 0.3)
        edges.append(b)
        lo = b
    edges.append(e)
    return [[round(a, 3), round(b, 3), p] for a, b, p in zip(edges, edges[1:], parts)]


def _fmt(t: float, sep: str) -> str:
    """Seconds → 00:01:02,345. Convert to milliseconds first, then split: 3.9996 s is 00:00:04,000 (it used to become 00:00:03,000)."""
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


UPRIGHT = ("ar",)          # Arabic is written right to left, slanting it to the right looks wrong, so no italics; all other languages (including Chinese) are italic
# Desktop players (Windows Media Player etc.) show external subtitles slightly late: the SRT (for desktop players) timeline is shifted this many seconds earlier;
# VTT and the web player package are for web players, which are accurate, so they aren't shifted
SRT_LEAD = 0.25


def italic(lang: str) -> bool:
    """Second subtitles are italic by default so they are easy to tell apart from the main subtitle."""
    return (lang or "").split("-")[0].lower() not in UPRIGHT


def write_vtt(cues: List[List[Any]], path: Path, lang: str = "") -> None:
    """WebVTT: on the very bottom line (the main subtitle has already moved up to make room), slightly smaller, italic by default."""
    rows = ["WEBVTT", "", "STYLE", "::cue {", "  font-size: 75%;", "  background-color: rgba(8, 10, 14, 0.72);", "}", ""]
    for k, (s, e, t) in enumerate(cues, 1):
        t = str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rows += [str(k), f"{_fmt(s, '.')} --> {_fmt(e, '.')} line:-1 position:50% align:center size:90%",
                 f"<i>{t}</i>" if italic(lang) else t, ""]
    path.write_text("\n".join(rows), encoding="utf-8")


def write_srt(cues: List[List[Any]], path: Path, lang: str = "") -> None:
    """SRT: for desktop players, shifted SRT_LEAD seconds earlier as a whole (shifting everything keeps consecutive cues from overlapping)."""
    rows = []
    for k, (s, e, t) in enumerate(cues, 1):
        s, e = max(0.0, s - SRT_LEAD), max(0.1, e - SRT_LEAD)
        rows += [str(k), f"{_fmt(s, ',')} --> {_fmt(e, ',')}", f"<i>{t}</i>" if italic(lang) else str(t), ""]
    path.write_text("\n".join(rows), encoding="utf-8")


def track_files(video: str, lang: str) -> Dict[str, str]:
    stem = Path(video).stem
    return {"vtt": f"{stem}.{lang}.vtt", "srt": f"{stem}.{lang}.srt", "json": f"{stem}.{lang}.json"}


def generate(proj: Project, languages: List[str], progress: Progress = None,
             client: Optional[ChatClient] = None) -> List[SubtitleTrack]:
    """Generate second-language subtitles for the current video. Returns the generated tracks (the caller writes them into the project)."""
    from .script_gen import lang_name
    primary = load_primary(proj)
    if not primary or not primary.get("cues"):
        raise LLMError(i18n.t("还没有渲染好的视频（或者视频没有字幕），先点「③ 渲染视频」。"))
    langs = [x for x in dict.fromkeys(languages) if x and x != proj.language]    # a Simplified Chinese video may have Traditional Chinese second subtitles
    if not langs:
        raise LLMError(i18n.t("选一种和视频不同的语言"))
    client = client or get_client()
    sents = group_sentences(primary["cues"])
    key = primary_key(primary)
    out_dir = storage.output_dir(proj.id)
    tracks: List[SubtitleTrack] = []
    for li, lang in enumerate(langs):
        name = lang_name(lang)

        def sub(f: float, m: str, li=li) -> None:
            if progress:
                progress((li + f) / len(langs), m)
        label = i18n.t("翻译第二语言字幕：{language}（{i}/{n}）", language=name, i=li + 1, n=len(langs))
        texts = _translate(client, [x["t"] for x in sents], lang_name(proj.language), name, sub, label)
        cjk = lang.split("-")[0] in ("zh", "ja", "ko")
        cues: List[List[Any]] = []
        for x, tr in zip(sents, texts):
            cues += _split(tr, x["s"], x["e"], 38 if cjk else 96, x.get("cuts"))
        files = track_files(proj.output, lang)
        write_vtt(cues, out_dir / files["vtt"], lang)
        write_srt(cues, out_dir / files["srt"], lang)
        (out_dir / files["json"]).write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        tracks.append(SubtitleTrack(lang=lang, name=name, video=proj.output, key=key, vtt=files["vtt"],
                                    srt=files["srt"], created_at=time.time()))
    if progress:
        sep = "、" if i18n.current() == "zh" else ", "
        progress(1.0, i18n.t("第二语言字幕已生成：{names}", names=sep.join(t.name for t in tracks)))
    return tracks


def merge_tracks(proj: Project, new: List[SubtitleTrack]) -> None:
    """Newly generated tracks replace older tracks of the same language."""
    langs = {t.lang for t in new}
    proj.subtitle_tracks = [t for t in proj.subtitle_tracks if t.lang not in langs] + new
    proj.subtitle_tracks.sort(key=lambda t: t.lang)


def state(proj: Project) -> Dict[str, Any]:
    """For the editor: main subtitle status + every second-language subtitle (those not matching the current video are marked outdated)."""
    primary = load_primary(proj)
    key = primary_key(primary) if primary else ""
    out_dir = storage.output_dir(proj.id)
    tracks = []
    for t in proj.subtitle_tracks:
        ok = bool(primary) and t.video == proj.output and t.key == key and (out_dir / t.vtt).exists()
        tracks.append({**t.model_dump(), "stale": not ok})
    return {"video": proj.output, "language": proj.language,
            "has_primary": bool(primary and primary.get("cues")),
            "burned": bool(primary and primary.get("burned")),
            "space": bool(primary and primary.get("space")),
            "legacy": bool(primary and primary.get("legacy")),
            "bottom": float((primary or {}).get("bottom") or LEGACY_BOTTOM),
            "tracks": tracks}


def track_cues(proj: Project, lang: str) -> List[List[Any]]:
    t = next((x for x in proj.subtitle_tracks if x.lang == lang), None)
    if t is None:
        return []
    p = storage.output_dir(proj.id) / track_files(t.video, t.lang)["json"]
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def delete_track(proj: Project, lang: str) -> None:
    out_dir = storage.output_dir(proj.id)
    for t in [x for x in proj.subtitle_tracks if x.lang == lang]:
        for f in track_files(t.video, t.lang).values():
            (out_dir / f).unlink(missing_ok=True)
    proj.subtitle_tracks = [x for x in proj.subtitle_tracks if x.lang != lang]


# ---- web player package ---------------------------------------------------------------------

PLAYER_HTML = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  html, body {{ margin: 0; background: #0e1116; color: #e8ecf2; font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; }}
  .wrap {{ max-width: 1280px; margin: 0 auto; padding: 12px; }}
  .stage {{ position: relative; background: #000; border-radius: 10px; overflow: hidden; }}
  .stage video {{ display: block; width: 100%; height: auto; background: #000; }}
  .stage:fullscreen {{ border-radius: 0; display: flex; align-items: center; }}
  .stage:fullscreen video {{ width: 100%; height: 100%; object-fit: contain; }}
  .sub {{ position: absolute; left: 0; right: 0; display: flex; justify-content: center; text-align: center; pointer-events: none; }}
  .sub span {{ display: inline-block; max-width: 92%; padding: 0 .45em .04em; border-radius: .3em; background: rgba(8, 10, 14, .72);
              color: #fff; line-height: 1.1; white-space: pre-line; font-style: italic; }}
  .sub.upright span {{ font-style: normal; }}
  .sub span:empty {{ display: none; }}
  .bar {{ display: flex; gap: 10px; align-items: center; justify-content: flex-end; margin-top: 10px; }}
  select, button {{ font: inherit; font-size: 14px; color: #e8ecf2; background: #1a2029; border: 1px solid #252c38;
                   border-radius: 8px; padding: 6px 10px; }}
  button {{ cursor: pointer; }}
  video::cue {{ background: rgba(8, 10, 14, .72); color: #fff; font-size: 56%; line-height: 1.1; font-style: italic; }}
  video.upright::cue {{ font-style: normal; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="stage" id="stage">
    <video id="v" src="{video}" controls disablepictureinpicture playsinline preload="metadata"></video>
    <div class="sub" id="sub"><span id="subText"></span></div>
  </div>
  <div class="bar">
    <label>&#x1F4AC; <select id="lang"><option value="">&mdash;</option>{options}</select></label>
    <button id="fs" title="Fullscreen">&#x26F6;</button>
  </div>
</div>
<script>
const TRACKS = {tracks};
const BOTTOM = {bottom};    // bottom edge of the main subtitle as a fraction of the frame height (recorded at render time)
const GAP = 0;              // the second subtitle's box sits directly below the main subtitle's box
const UPRIGHT = ['ar'];     // no italics for Arabic (written right to left, slanting it to the right looks wrong)
const v = document.getElementById('v'), stage = document.getElementById('stage');
const sub = document.getElementById('sub'), subText = document.getElementById('subText');
const sel = document.getElementById('lang');
let cues = [];
function contentRect() {{
  const W = v.clientWidth, H = v.clientHeight, vw = v.videoWidth || 16, vh = v.videoHeight || 9;
  const s = Math.min(W / vw, H / vh), w = vw * s, h = vh * s;
  return {{ top: v.offsetTop + (H - h) / 2, height: h }};
}}
function layout() {{
  // the second subtitle sits right below the main one (rendering left one line of room under it); font size about 2.8% of the frame height;
  // if two lines occasionally don't fit, move it up a little so it stays inside the frame
  const r = contentRect();
  sub.style.fontSize = Math.max(11, r.height * 0.028) + 'px';
  const top = r.top + r.height * (BOTTOM + GAP);
  const maxTop = r.top + r.height * 0.994 - subText.offsetHeight;
  sub.style.top = Math.min(top, maxTop) + 'px';
}}
function show() {{
  const t = v.currentTime;
  let lo = 0, hi = cues.length - 1, text = '';
  while (lo <= hi) {{
    const m = (lo + hi) >> 1, c = cues[m];
    if (t < c[0]) hi = m - 1; else if (t >= c[1]) lo = m + 1; else {{ text = c[2]; break; }}
  }}
  if (subText.textContent !== text) {{ subText.textContent = text; layout(); }}
}}
function loop() {{ show(); if (!v.paused) requestAnimationFrame(loop); }}

// in the browser's own full screen (the player's full-screen button, double-click, phones) the page's subtitle layer can't be shown:
// hand the second subtitle to the browser to display instead (also right below the main subtitle)
let track = null;
const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
const nativeFs = () => fsEl() === v || !!v.webkitDisplayingFullscreen;
function syncTrack() {{
  if (track) track.mode = cues.length && nativeFs() ? 'showing' : 'hidden';
  sub.style.visibility = nativeFs() ? 'hidden' : '';
  setTimeout(layout, 50);
}}
function buildTrack() {{
  if (!v.addTextTrack || typeof VTTCue === 'undefined') return;
  if (!track) track = v.addTextTrack('subtitles', 'second', '');
  track.mode = 'hidden';
  while (track.cues && track.cues.length) track.removeCue(track.cues[0]);
  for (const [a, b, t] of cues) {{
    const c = new VTTCue(a, b, t);
    c.snapToLines = false; c.line = (BOTTOM + GAP) * 100; c.position = 50; c.size = 92; c.align = 'center';
    track.addCue(c);
  }}
  syncTrack();
}}
sel.onchange = () => {{
  cues = (TRACKS[sel.value] || {{}}).cues || [];
  const upright = UPRIGHT.includes(sel.value.split('-')[0]);
  sub.classList.toggle('upright', upright);
  v.classList.toggle('upright', upright);
  show(); buildTrack();
}};
v.addEventListener('play', loop);
v.addEventListener('seeked', show);
v.addEventListener('timeupdate', show);
v.addEventListener('loadedmetadata', layout);
window.addEventListener('resize', layout);
for (const ev of ['fullscreenchange', 'webkitfullscreenchange']) document.addEventListener(ev, syncTrack);
v.addEventListener('webkitbeginfullscreen', syncTrack);
v.addEventListener('webkitendfullscreen', syncTrack);

// ⛶: full screen for the whole stage (video + second-subtitle layer), the most accurate subtitle position. If the browser refuses (e.g. in an iframe without allow="fullscreen")
// fall back to the video's own full screen; failing that, open this page in a new window (continuing at the current time and subtitle)
function openHere() {{
  const q = new URLSearchParams(location.search);
  if (sel.value) q.set('sub', sel.value); else q.delete('sub');
  q.set('t', v.currentTime.toFixed(1));
  window.open(location.pathname + '?' + q.toString(), '_blank');
}}
// request full screen with a clear outcome either way. Some embedded browsers neither grant nor refuse; not in full screen after 1.5 s counts as refused
function reqFs(el) {{
  const f = el.requestFullscreen || el.webkitRequestFullscreen;
  if (!f || !(document.fullscreenEnabled || document.webkitFullscreenEnabled)) return Promise.reject(new Error('no'));
  return new Promise((ok, bad) => {{
    const timer = setTimeout(() => {{ if (!fsEl()) bad(new Error('timeout')); }}, 1500);
    Promise.resolve(f.call(el)).then(() => {{ clearTimeout(timer); ok(); }}, (e) => {{ clearTimeout(timer); bad(e); }});
  }});
}}
function enterFs() {{
  if (!(document.fullscreenEnabled || document.webkitFullscreenEnabled) && v.webkitEnterFullscreen) {{
    v.webkitEnterFullscreen();                          // iPhone: only the video itself can go full screen
    return;
  }}
  reqFs(stage).catch(() => reqFs(v)).catch(openHere);
}}
document.getElementById('fs').onclick = () => {{
  if (fsEl()) (document.exitFullscreen || document.webkitExitFullscreen).call(document); else enterFs();
}};
const q = new URLSearchParams(location.search);
const want = q.get('sub');           // ?sub=en-US in the link opens that second subtitle directly
if (want && TRACKS[want]) {{ sel.value = want; sel.onchange(); }}
const t0 = parseFloat(q.get('t') || '');
if (t0 > 0) {{
  if (v.readyState >= 1) v.currentTime = t0;                       // metadata already loaded (e.g. from cache)
  else v.addEventListener('loadedmetadata', () => {{ v.currentTime = t0; }}, {{ once: true }});
}}
layout();
</script>
</body>
</html>
"""  # i18n: ignore


def player_html(proj: Project, video_src: str, tracks: List[SubtitleTrack]) -> str:
    """Standalone player page: the subtitle timelines of all languages are embedded (no extra files to load; it even works when opened from disk)."""
    data = {t.lang: {"name": t.name, "cues": track_cues(proj, t.lang)} for t in tracks}
    options = "".join(f'<option value="{html.escape(t.lang)}">{html.escape(t.name)}</option>' for t in tracks)
    return PLAYER_HTML.format(
        lang=html.escape(proj.language.split("-")[0]), title=html.escape(proj.title or proj.name),
        bottom=json.dumps(state(proj)["bottom"]),
        video=html.escape(quote(video_src)), options=options,
        tracks=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def export_package(proj: Project) -> Path:
    """Web player package (zip): video, second-language subtitles (VTT + SRT), player page index.html. Works in any folder of a website."""
    st = state(proj)
    tracks = [t for t in proj.subtitle_tracks if not next(x for x in st["tracks"] if x["lang"] == t.lang)["stale"]]
    out_dir = storage.output_dir(proj.id)
    video = out_dir / proj.output
    if not proj.output or not video.exists():
        raise FileNotFoundError(i18n.t("还没有渲染好的视频，先点「③ 渲染视频」。"))
    tmp = storage.work_dir(proj.id) / f"package_{uuid.uuid4().hex[:8]}.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr("index.html", player_html(proj, proj.output, tracks), compress_type=zipfile.ZIP_DEFLATED)
        z.write(video, proj.output, compress_type=zipfile.ZIP_STORED)           # the video is already compressed
        for t in tracks:
            for f in (t.vtt, t.srt):
                if (out_dir / f).exists():
                    z.write(out_dir / f, f, compress_type=zipfile.ZIP_DEFLATED)
    return tmp
