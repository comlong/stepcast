"""HTML decks: the slides of a web page deck as steps of a project, and their recording.

Import (`analyze`): the deck (one .html file, or a .zip with its files) is opened in a headless Edge / Chrome (cdp.py) with StepCast's
page helper (web_page.js). Its slides, their notes and controls are found, a still of each slide is taken, and the online resources it
loads (fonts, script libraries, pictures) are saved with it, so that recording later works offline and looks the same every time.

Recording (`record`): when the video is rendered, every live HTML slide is shown on its own in the browser and recorded frame by frame on
the page's virtual clock — its own animations, the clicks of its actions, a pointer drawn into the page — into a short video that the
render puts where the slide's still would be. A recording is reused while nothing it was made from has changed.

The deck is served from a private origin by answering the browser's requests directly: no web server, nothing listens on a port, and
the page can't reach StepCast's own API or anything else on this computer or network.

A project's deck lives in web/: site/ (the files), cache/ (saved online resources), deck.json (how to show slide k).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from PIL import Image

from .. import i18n, storage
from ..models import Project, Step, WebBlock, WebControl
from . import cdp
from .ffmpeg_util import CREATE_NO_WINDOW, ffmpeg_bin

Progress = Optional[Callable[[float, str], None]]

ORIGIN = "https://deck.stepcast.invalid"
VIEW_W, VIEW_H = 1920, 1080          # the window the deck is laid out in (CSS pixels)
STILL_WIDTH = 1920                   # width of the slide stills
EXTS = (".html", ".htm", ".zip")
MAX_PAGES = 100
MAX_UNPACKED = 600 * 1024 ** 2
MAX_FILES = 5000
CACHE_FILE_MAX = 40 * 1024 ** 2
CACHE_TOTAL_MAX = 300 * 1024 ** 2
LOAD_TIMEOUT = 45.0
WAIT_TIMEOUT = 10.0                  # fonts / pictures of a page
SETTLE_MS = 2500                     # stills: the slide's own animations get this long before the picture is taken
NAV_SETTLE_MS = 1200                 # between two key presses on the way to a slide
IDLE_TIMEOUT = 4.0                   # how long what a slide asks for may take to load (seconds)
BUSY_MAX = 2.5                       # a request taking longer is not waited for
EXTEND_MAX = 8.0                     # an animation started by a click near the end may play this long after the narration
CAPTURE_VERSION = 1
JPEG_QUALITY = 92
WORKERS_MAX = 3


class WebDeckError(RuntimeError):
    pass


def web_dir(pid: str) -> Path:
    return storage.project_dir(pid) / "web"


_JS: Dict[str, str] = {}


def page_js() -> str:
    if "src" not in _JS:
        _JS["src"] = (Path(__file__).with_name("web_page.js")).read_text(encoding="utf-8")
        _JS["hash"] = hashlib.sha1(_JS["src"].encode("utf-8")).hexdigest()[:12]
    return _JS["src"]


def is_web(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in EXTS


# ---- unpacking ----------------------------------------------------------------------------------------------------------------

def _natural(s: str) -> List[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _zip_name(info: zipfile.ZipInfo) -> str:
    """The member's name; zips made by Windows often store non-ASCII names in the local code page without saying so."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gbk", "big5", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def _safe_rel(name: str) -> Optional[str]:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    if parts[0] == "__MACOSX" or parts[-1] in (".DS_Store", "Thumbs.db", "desktop.ini"):
        return None
    return "/".join(parts)


def unpack(upload: Path, filename: str, deck_dir: Path) -> Dict[str, Any]:
    """Put the deck's files into deck_dir/site. Returns {"entry": the page to open, "pages": [one page per slide] or []}."""
    site = deck_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix.lower()
    if ext in (".html", ".htm"):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).name).strip(" .") or "index.html"
        shutil.copyfile(upload, site / name)
        return {"entry": name, "pages": []}
    try:
        z = zipfile.ZipFile(upload)
    except zipfile.BadZipFile:
        raise WebDeckError(i18n.t("压缩包打不开（不是有效的 .zip 文件）"))
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise WebDeckError(i18n.t("压缩包里的文件太多（最多 {n} 个）", n=MAX_FILES))
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise WebDeckError(str(storage.UploadTooLarge(MAX_UNPACKED)))
        written = 0
        for info in infos:
            rel = _safe_rel(_zip_name(info))
            if rel is None:
                continue
            dst = site / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as out:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    written += len(buf)
                    if written > MAX_UNPACKED:               # sizes in the zip header can lie
                        raise WebDeckError(str(storage.UploadTooLarge(MAX_UNPACKED)))
                    out.write(buf)
    # a zip of a folder ("Send to → Compressed folder"): that folder is the site, so that absolute paths ("/assets/…" of a built
    # web app) are found
    tops = list(site.iterdir())
    if len(tops) == 1 and tops[0].is_dir():
        inner = tops[0]
        tmp = site.with_name("site_root")
        inner.rename(tmp)
        site.rmdir()
        tmp.rename(site)
    pages = [p for p in site.rglob("*") if p.is_file() and p.suffix.lower() in (".html", ".htm")]
    if not pages:
        raise WebDeckError(i18n.t("压缩包里没有 .html 网页"))
    rel = lambda p: p.relative_to(site).as_posix()            # noqa: E731
    index = sorted((p for p in pages if p.name.lower() in ("index.html", "index.htm")), key=lambda p: len(p.parts))
    if index:
        return {"entry": rel(index[0]), "pages": []}
    depth = min(len(p.relative_to(site).parts) for p in pages)
    top = sorted((p for p in pages if len(p.relative_to(site).parts) == depth), key=lambda p: _natural(p.name))
    if len(top) == 1:
        return {"entry": rel(top[0]), "pages": []}
    return {"entry": rel(top[0]), "pages": [rel(p) for p in top][:MAX_PAGES]}      # one page per slide


# ---- the browser's requests -----------------------------------------------------------------------------------------------

_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".htm": "text/html",
          ".json": "application/json", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".woff": "font/woff",
          ".ttf": "font/ttf", ".otf": "font/otf", ".wasm": "application/wasm", ".webp": "image/webp", ".avif": "image/avif",
          ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".md": "text/markdown", ".txt": "text/plain"}


_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w-]+)", re.I)


def _ctype(name: str, body: Optional[bytes] = None) -> str:
    """Content type of a file of the deck. Text in another encoding than UTF-8 (a page saved as GBK, say) is declared as what it
    says it is (<meta charset>), else as GB18030 when it reads as that (Chinese Windows' default), so that it isn't garbled."""
    ext = Path(name).suffix.lower()
    t = _TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not (t.startswith("text/") or t in ("application/json", "image/svg+xml")):
        return t
    charset = "utf-8"
    if body is not None:
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            m = _META_CHARSET.search(body[:4096]) if t == "text/html" else None
            charset = m.group(1).decode("ascii", "ignore").lower() if m else "gb18030"
            if charset == "gb18030":
                try:
                    body.decode("gb18030")
                except UnicodeDecodeError:
                    charset = "windows-1252"
    return f"{t}; charset={charset}"


def _local(url: str) -> bool:
    """A request to this computer or the local network (blocked: a deck has no business there)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return False
    if host in ("localhost", "0.0.0.0") or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False


def _range(request: Dict[str, Any]) -> Optional[str]:
    """The Range header of a paused request, if any."""
    for k, v in (request.get("headers") or {}).items():
        if k.lower() == "range":
            return v
    return None


class Net:
    """Answers one page's requests: the deck's own files, saved online resources, everything else from the network
    (saved when `save` is on); nothing local."""

    def __init__(self, page: cdp.Session, deck_dir: Path, save: bool):
        self.page, self.site, self.cache, self.save = page, deck_dir / "site", deck_dir / "cache", save
        self.index: Dict[str, Dict[str, Any]] = {}
        p = self.cache / "index.json"
        if p.exists():
            try:
                self.index = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.index = {}
        self.saved_bytes = sum(int(v.get("size", 0)) for v in self.index.values())
        self.missing: List[str] = []         # files of the deck that aren't there
        self.failed: List[str] = []          # online resources that didn't load
        self._urls: Dict[str, str] = {}
        self._busy: Dict[str, float] = {}    # requests under way: when they started

    async def enable(self) -> None:
        self.page.on("Fetch.requestPaused", self._paused)
        self.page.on("Network.requestWillBeSent", self._will_send)
        self.page.on("Network.loadingFinished", lambda p: self._busy.pop(p.get("requestId", ""), None))
        self.page.on("Network.loadingFailed", self._load_failed)
        await self.page.send("Network.enable")
        await self.page.send("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"},
                                                       {"urlPattern": "http*", "requestStage": "Response"}])

    @property
    def busy(self) -> bool:
        # (the end of a worker's script or of another site's frame is never reported here: such a request stops counting after a while)
        now = time.monotonic()
        return any(now - t < BUSY_MAX for t in self._busy.values())

    def forget(self) -> None:
        """A new page: what the last one was loading doesn't matter any more."""
        self._busy.clear()

    def _will_send(self, p: Dict[str, Any]) -> None:
        self._urls[p["requestId"]] = p["request"]["url"]
        if p.get("type") != "Media" and p["request"]["url"].startswith("http"):      # (a video keeps its request open)
            self._busy[p["requestId"]] = time.monotonic()

    async def idle(self, timeout: float = IDLE_TIMEOUT) -> None:
        """Wait until nothing is being loaded: what showing a slide asked for (pictures, background pictures) is in."""
        end = time.monotonic() + timeout
        quiet = 0.0
        while time.monotonic() < end and quiet < 0.12:
            await asyncio.sleep(0.04)
            quiet = 0.0 if self.busy else quiet + 0.04

    def _load_failed(self, p: Dict[str, Any]) -> None:
        self._busy.pop(p.get("requestId", ""), None)
        url = self._urls.get(p.get("requestId", ""), "")
        if url.startswith("http") and not url.startswith(ORIGIN) and not p.get("canceled") and \
                p.get("blockedReason") is None and url not in self.failed and not _local(url):
            self.failed.append(url)

    def write_index(self) -> None:
        if self.index:
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / "index.json").write_text(json.dumps(self.index, ensure_ascii=False, indent=1), encoding="utf-8")

    async def _paused(self, p: Dict[str, Any]) -> None:
        rid, url = p["requestId"], p["request"]["url"]
        try:
            if "responseStatusCode" in p or "responseErrorReason" in p:
                await self._keep(p)
            elif url.split("#")[0].split("?")[0].rstrip("/") == ORIGIN or url.startswith(ORIGIN + "/"):
                await self._serve(rid, url, _range(p["request"]))
            elif _local(url):
                await self.page.send("Fetch.failRequest", requestId=rid, errorReason="BlockedByClient")
            elif url in self.index and (self.cache / self.index[url]["file"]).is_file():
                e = self.index[url]
                await self._fulfil(rid, (self.cache / e["file"]).read_bytes(), e.get("type") or _ctype(url), int(e.get("status", 200)),
                                   _range(p["request"]))
            else:
                await self.page.send("Fetch.continueRequest", requestId=rid)
        except cdp.CDPError:
            pass                                    # the request was cancelled meanwhile (the page went on)

    async def _fulfil(self, rid: str, body: bytes, ctype: str, status: int = 200, rng: Optional[str] = None) -> None:
        headers = [{"name": "Content-Type", "value": ctype}, {"name": "Access-Control-Allow-Origin", "value": "*"},
                   {"name": "Cache-Control", "value": "no-store"}, {"name": "Accept-Ranges", "value": "bytes"}]
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if status == 200 and m and (m.group(1) or m.group(2)):
            # a part only (a video asks for parts; without them it can't be moved to another time)
            total = len(body)
            if m.group(1):
                a = int(m.group(1))
                b = min(total - 1, int(m.group(2))) if m.group(2) else total - 1
            else:
                a, b = max(0, total - int(m.group(2))), total - 1
            if a >= total or a > b:
                status, body = 416, b""
                headers.append({"name": "Content-Range", "value": f"bytes */{total}"})
            else:
                status, body = 206, body[a:b + 1]
                headers.append({"name": "Content-Range", "value": f"bytes {a}-{b}/{total}"})
        await self.page.send("Fetch.fulfillRequest", requestId=rid, responseCode=status, responseHeaders=headers,
                             body=base64.b64encode(body).decode("ascii"))

    async def _serve(self, rid: str, url: str, rng: Optional[str] = None) -> None:
        path = unquote(urlsplit(url).path).lstrip("/")
        f = (self.site / path).resolve() if path else self.site.resolve()
        base = self.site.resolve()
        if f.is_dir():
            f = f / "index.html" if (f / "index.html").is_file() else f / "index.htm"
        if not f.is_relative_to(base) or not f.is_file():
            app = base / "index.html"
            if path and "." not in path.rsplit("/", 1)[-1] and app.is_file():
                body = app.read_bytes()              # a route of a single-page web app ("/2"): the app itself answers it
                await self._fulfil(rid, body, _ctype(app.name, body))
                return
            if path and path not in self.missing and not path.endswith("favicon.ico"):
                self.missing.append(path)
            await self._fulfil(rid, b"Not found", "text/plain", 404)
            return
        body = f.read_bytes()
        await self._fulfil(rid, body, _ctype(f.name, body), rng=rng)

    async def _keep(self, p: Dict[str, Any]) -> None:
        rid, url = p["requestId"], p["request"]["url"]
        status = int(p.get("responseStatusCode") or 0)
        headers = {h["name"].lower(): h["value"] for h in p.get("responseHeaders") or []}
        ctype = headers.get("content-type", "")
        size = int(headers.get("content-length", "0") or 0)
        if (self.save and status == 200 and url not in self.index and size <= CACHE_FILE_MAX
                and not ctype.startswith(("video/", "audio/", "text/event-stream"))
                and self.saved_bytes < CACHE_TOTAL_MAX and p["request"].get("method", "GET") == "GET"):
            try:
                r = await self.page.send("Fetch.getResponseBody", requestId=rid)
                body = base64.b64decode(r["body"]) if r.get("base64Encoded") else r["body"].encode("utf-8")
                if len(body) <= CACHE_FILE_MAX:
                    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                    self.cache.mkdir(parents=True, exist_ok=True)
                    (self.cache / name).write_bytes(body)
                    self.index[url] = {"file": name, "type": ctype or _ctype(url), "status": 200, "size": len(body)}
                    self.saved_bytes += len(body)
            except (cdp.CDPError, KeyError, ValueError):
                pass
        await self.page.send("Fetch.continueRequest", requestId=rid)


# ---- one page in the browser ----------------------------------------------------------------------------------------------

class Page:
    """A browser page showing the deck, with the page helper injected and the requests answered by Net."""

    def __init__(self, sess: cdp.Session, net: Net):
        self.s, self.net = sess, net
        self._waits: Dict[str, asyncio.Future] = {}       # loaderId of a navigation -> its "load"
        self.dpr = 1.0
        sess.on("Page.lifecycleEvent", self._lifecycle)
        sess.on("Page.javascriptDialogOpening", self._dialog)

    async def _dialog(self, p: Dict[str, Any]) -> None:
        """A dialog the page helper can't answer ("Leave this page?"): it would stop the page for good."""
        try:
            await self.s.send("Page.handleJavaScriptDialog", accept=True)
        except cdp.CDPError:
            pass

    def _lifecycle(self, p: Dict[str, Any]) -> None:
        if p.get("name") == "load":
            loader = p.get("loaderId", "")
            fut = self._waits.get(loader)
            if fut is None:                         # it may come before Page.navigate has answered
                fut = self._waits[loader] = asyncio.get_running_loop().create_future()
            if not fut.done():
                fut.set_result(True)

    async def _navigate(self, url: str, timeout: float) -> None:
        """Navigate and wait for that navigation's own load event (a late event of the page before can't be taken for it)."""
        loop = asyncio.get_running_loop()
        r = await self.s.send("Page.navigate", url=url)
        loader = r.get("loaderId")
        if not loader:
            return                                  # same-document navigation: nothing to wait for
        fut = self._waits.setdefault(loader, loop.create_future())
        for old in [k for k, v in self._waits.items() if k != loader and v.done()]:
            self._waits.pop(old, None)
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            pass                                    # something keeps loading: go on with what is there
        finally:
            self._waits.pop(loader, None)

    @classmethod
    async def open(cls, browser: cdp.Browser, deck_dir: Path, save: bool = False) -> "Page":
        sess = await browser.new_page()
        net = Net(sess, deck_dir, save)
        page = cls(sess, net)
        await sess.send("Page.enable")
        await sess.send("Page.setLifecycleEventsEnabled", enabled=True)
        await sess.send("Page.setBypassCSP", enabled=True)          # a security policy written for its real address must not stop it here
        await sess.send("Runtime.enable")
        await net.enable()
        await page.metrics(1.0)
        await sess.send("Emulation.setEmulatedMedia", media="screen",
                        features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
        try:
            await sess.send("Emulation.setFocusEmulationEnabled", enabled=True)
        except cdp.CDPError:
            pass
        await sess.send("Page.addScriptToEvaluateOnNewDocument",
                        source="window.__scConfig=" + json.dumps({"freshStorage": True}) + ";\n" + page_js())
        return page

    async def metrics(self, dpr: float) -> None:
        self.dpr = dpr
        await self.s.send("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H, deviceScaleFactor=dpr, mobile=False)

    async def js(self, expr: str, timeout: float = cdp.CALL_TIMEOUT) -> Any:
        return await self.s.evaluate(expr, timeout=timeout)

    async def load(self, path: str) -> None:
        """Open a page of the deck afresh (path relative to the site, may end in #hash) and wait until it is ready: loaded,
        fonts and pictures in, its start-up timers run. Animations wait at their first frame meanwhile."""
        await self._navigate("about:blank", 10.0)  # a new document even when only the #hash differs
        self.net.forget()
        # index.html by its folder's address, as on a web server (a web app's router knows "/", not "/index.html")
        await self._navigate(ORIGIN + "/" + re.sub(r"(^|/)index\.html?(?=$|[?#])", r"\1", path, count=1, flags=re.I), LOAD_TIMEOUT)
        for expr in ("document.fonts ? document.fonts.ready.then(() => true) : true",
                     "[...document.images].forEach((i) => { if (i.loading === 'lazy') i.loading = 'eager'; }), "
                     "Promise.all([...document.images].filter((i) => !i.complete)"
                     ".map((i) => new Promise((r) => { i.addEventListener('load', r); i.addEventListener('error', r); })))"
                     ".then(() => true)",
                     # web fonts come in pieces by character (Chinese fonts in a hundred): load every piece the deck may show —
                     # hidden slides and texts in its scripts too — or text appearing later is drawn in a fallback font first
                     "(() => { if (!document.fonts) return true; const text = (document.documentElement.textContent || '')"
                     ".slice(0, 300000); const seen = new Set(); const jobs = []; document.fonts.forEach((f) => {"
                     " const w = String(f.weight || '400').split(' ')[0]; const k = f.family + '|' + w + '|' + f.style;"
                     " if (seen.has(k)) return; seen.add(k);"
                     " const fam = JSON.stringify(String(f.family).replace(/^[\"']|[\"']$/g, ''));"
                     " jobs.push(document.fonts.load((f.style === 'normal' ? '' : f.style + ' ') + w + ' 16px ' + fam, text)"
                     ".catch(() => 0)); }); return Promise.all(jobs).then(() => true); })()"):
            try:
                await asyncio.wait_for(self.js(expr), WAIT_TIMEOUT)
            except (asyncio.TimeoutError, cdp.CDPError):
                pass
        await self.js("__sc.advance(250)")

    async def press(self, key: str = "ArrowRight") -> None:
        codes = {"ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40), "PageDown": ("PageDown", 34), " ": ("Space", 32)}
        code, vk = codes.get(key, (key, 0))
        await self.js("__sc.blur()")
        for typ in ("rawKeyDown", "keyUp"):
            await self.s.send("Input.dispatchKeyEvent", type=typ, key=key, code=code, windowsVirtualKeyCode=vk,
                              nativeVirtualKeyCode=vk)

    async def mouse(self, typ: str, x: float, y: float) -> None:
        params: Dict[str, Any] = {"type": typ, "x": x, "y": y, "button": "none" if typ == "mouseMoved" else "left"}
        if typ != "mouseMoved":
            params["clickCount"] = 1
        await self.s.send("Input.dispatchMouseEvent", **params)

    async def click(self, x: float, y: float) -> None:
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.mouse(typ, x, y)

    async def shot(self, clip: Dict[str, float], fmt: str = "jpeg") -> bytes:
        params: Dict[str, Any] = {"format": fmt, "clip": {**clip, "scale": 1}, "captureBeyondViewport": False,
                                  "optimizeForSpeed": True}
        if fmt == "jpeg":
            params["quality"] = JPEG_QUALITY
        for attempt in range(4):
            r = await self.s.send("Page.captureScreenshot", **params)
            data = base64.b64decode(r.get("data") or "")
            if data:
                return data
            # a window the browser thinks is hidden produces no picture: bring it to the front and try again
            try:
                await self.s.send("Page.bringToFront")
            except cdp.CDPError:
                pass
            await asyncio.sleep(0.1 * (attempt + 1))
        raise WebDeckError(i18n.t("录制时浏览器没有给出画面"))


# ---- showing slide k ---------------------------------------------------------------------------------------------------------

_HASH = re.compile(r"^#(/?)(slide-?|page-?|p|s)?(\d+)$", re.I)


async def _open(page: Page, deck: Dict[str, Any], path: str) -> None:
    """Load a page of the deck and get past its cover, if it has one with a start button."""
    await page.load(path)
    start = deck.get("start")
    if start:
        r = await page.js(f"__sc.rect(-1, {json.dumps(start)})")
        if r:
            await page.click(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            await page.mouse("mouseMoved", VIEW_W - 1, VIEW_H - 1)
            await page.js(f"__sc.advance({NAV_SETTLE_MS})")


async def _detect(page: Page, deck: Dict[str, Any]) -> Dict[str, Any]:
    """The helper's view of the slides, once they are all there (a web app may render them a few seconds after it starts)."""
    info = await page.js("__sc.detect()")
    want = int(deck.get("count") or 0)
    for _ in range(4):
        if not info.get("found") or int(info.get("count", 0)) >= want:
            break
        await page.js("__sc.advance(1500)")
        await page.net.idle(1.5)
        info = await page.js("__sc.detect()")
    return info


async def _find_start(page: Page, deck: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    """A cover with a start button in front of the slides: pressed on every load if the slides are behind it."""
    start = await page.js("__sc.startButton()")
    if not start:
        return info
    deck["start"] = start
    await _open(page, deck, deck["entry"])
    after = await page.js("__sc.detect()")
    if after.get("found") and int(after.get("count", 0)) >= max(2, int(info.get("count", 0))) and not await page.js("__sc.startButton()"):
        return after
    deck.pop("start", None)                     # a button that belongs to the page after all
    await page.load(deck["entry"])
    return await page.js("__sc.detect()")


async def goto(page: Page, deck: Dict[str, Any], k: int) -> Dict[str, Any]:
    """Load the deck afresh with slide k showing (the way deck.json says) and return the helper's view of the deck."""
    nav = deck.get("nav") or {}
    how = nav.get("how", "")
    entry = deck["entry"]
    if how == "pages":
        await page.load(deck["pages"][k])
        return await page.js("__sc.detect()")
    if how == "hash":
        await _open(page, deck, entry + nav["hash"].replace("{n}", str(k + int(nav.get("base", 1)))))
        return await _detect(page, deck)
    await _open(page, deck, entry)
    info = await _detect(page, deck)
    if info.get("kind") == "reveal":
        await page.js("__sc.configureReveal()")     # no controls, progress bar, slide number or auto-advance in the picture
    if k == 0 and how not in ("api", "scroll"):
        return info
    if how in ("api", "scroll", "classes"):
        await page.js(f"__sc.show({k}, {json.dumps(how)})")
        if how == "scroll":
            await asyncio.sleep(0.15)               # observers of scrolling (reveal-on-scroll) see the new position
            await page.js("__sc.advance(16)")
        return info
    # keys / a next button: step forward until slide k shows (a step may also show a part of the same slide). The animations of
    # the slides on the way play out; slide k's own start over when the recording begins (__sc.restart)
    found = bool(info.get("found"))
    # (a press may only show a part of a slide, and not every part shows: the loop stops as soon as slide k is there)
    for i in range(2 * k + int(nav.get("extra", 0)) + 30 if found else k):
        if how == "click":
            r = await page.js(f"__sc.rect(-1, {json.dumps(nav.get('next', ''))})")
            if not r:
                break
            await page.click(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            await page.mouse("mouseMoved", VIEW_W - 1, VIEW_H - 1)
        elif how == "clickpage":
            await page.click(VIEW_W / 2, VIEW_H / 2)
            await page.mouse("mouseMoved", VIEW_W - 1, VIEW_H - 1)
        else:
            await page.press(nav.get("key", "ArrowRight"))
        if not found:                           # (a viewer page changes the file in its frame now)
            await page.net.idle()
        await page.js("__sc.advance(60)")
        if not found:
            if i == k - 1:
                break
            await page.js(f"__sc.advance({NAV_SETTLE_MS})")
            continue
        # slide k on screen yet? At once in most decks; in a deck that slides the next one in, once it covers most of the window
        # (pressing again before that would go one too far). The rest of the slide-in is cut when the recording starts
        here = await page.js("__sc.active()") == k
        waited = 60
        while not here and waited < NAV_SETTLE_MS:
            if page.net.busy:                   # a web app loads the next slide's code first
                await page.net.idle()
            await page.js("__sc.advance(120)")
            waited += 120
            here = await page.js("__sc.active()") == k
        if here:
            break
    await page.js("__sc.hold()")
    return info


async def _find_nav(page: Page, deck: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    """How to show slide k of this deck: its API, scrolling, the address (#slide-2 …), keys, a next button, its classes."""
    if info.get("kind") in ("reveal", "impress", "swiper", "shower") or info.get("api"):
        return {"how": "api"}
    if info.get("mode") == "scroll":
        return {"how": "scroll"}
    entry = deck["entry"]
    count = int(info.get("count", 0))
    if count < 2:
        return {"how": "none"}
    tried = []
    m = _HASH.match(info.get("hash") or "")
    if m:
        base = int(m.group(3)) - max(0, int(info.get("active", 0)))
        tried.append(("#" + m.group(1) + (m.group(2) or "") + "{n}", base))
    tried += [("#slide-{n}", 1), ("#/{n}", 1), ("#{n}", 1), ("#/{n}", 0)]

    async def by_hash(pattern: str, base: int) -> bool:
        await _open(page, deck, entry + pattern.replace("{n}", str(1 + base)))
        r = await _detect(page, deck)
        return r.get("found") and r.get("active") == 1

    if m and await by_hash(*tried[0]):
        return {"how": "hash", "hash": tried[0][0], "base": tried[0][1]}
    for key in ("ArrowRight", "PageDown", " ", "ArrowDown"):
        await _open(page, deck, entry)
        await _detect(page, deck)
        await page.press(key)
        await page.js(f"__sc.advance({NAV_SETTLE_MS})")
        if await page.js("__sc.active()") == 1:
            return {"how": "key", "key": key}
    for pattern, base in tried[1 if m else 0:]:
        if await by_hash(pattern, base):
            return {"how": "hash", "hash": pattern, "base": base}
    await _open(page, deck, entry)
    await _detect(page, deck)
    nxt = await page.js("__sc.nextControl()")
    if nxt:
        r = await page.js(f"__sc.rect(-1, {json.dumps(nxt)})")
        if r:
            await page.click(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            await page.js(f"__sc.advance({NAV_SETTLE_MS})")
            if await page.js("__sc.active()") == 1:
                return {"how": "click", "next": nxt}
    await _open(page, deck, entry)
    await _detect(page, deck)
    if not await page.js("__sc.centerIsControl()"):         # a click anywhere turns the page
        await page.click(VIEW_W / 2, VIEW_H / 2)
        await page.js(f"__sc.advance({NAV_SETTLE_MS})")
        if await page.js("__sc.active()") == 1:
            return {"how": "clickpage"}
        await _open(page, deck, entry)
        await _detect(page, deck)
    if await page.js("__sc.show(1, 'classes')") and await page.js("__sc.active()") == 1:
        return {"how": "classes"}
    return {"how": "none"}


async def _screen(page: Page) -> Tuple[str, bytes]:
    """What is on the screen now: its text, and a tiny picture of it."""
    text = await page.js("__sc.signature()") or ""
    data = await page.shot({"x": 0, "y": 0, "width": VIEW_W, "height": VIEW_H})
    with Image.open(io.BytesIO(data)) as im:
        return text, im.convert("RGB").resize((64, 36), Image.BILINEAR).tobytes()


def _blocks(a: bytes, b: bytes) -> int:
    """In how many of the tiny pictures' blocks two screens differ."""
    return sum(1 for k in range(0, min(len(a), len(b)), 3) if max(abs(a[k] - b[k]), abs(a[k + 1] - b[k + 1]), abs(a[k + 2] - b[k + 2])) > 14)


async def _frames_nav(page: Page, deck: Dict[str, Any], progress: Progress) -> Tuple[int, Dict[str, Any]]:
    """A deck StepCast can't read (no slides in its page — drawn on a canvas, shown in a frame, made by a web app): find what turns its
    pages, and count them by what is on the screen."""
    entry = deck["entry"]
    await _open(page, deck, entry)
    await page.js(f"__sc.advance({NAV_SETTLE_MS})")
    first = await _screen(page)
    await page.js(f"__sc.advance({NAV_SETTLE_MS})")
    second = await _screen(page)
    # what moves by itself (a clock, an animated background) doesn't count as a new page
    limit = max(2, 3 * _blocks(first[1], second[1]) + 2)
    text_moves = first[0] != second[0]

    def differs(a: Tuple[str, bytes], b: Tuple[str, bytes]) -> bool:
        return (not text_moves and a[0] != b[0]) or _blocks(a[1], b[1]) >= limit

    async def turn(how: str) -> None:
        if how == "click":
            await page.click(VIEW_W / 2, VIEW_H / 2)
            await page.mouse("mouseMoved", VIEW_W - 1, VIEW_H - 1)
        else:
            await page.press(how)
        await page.net.idle()                   # (a viewer page changes the file in its frame now)
        await page.js(f"__sc.advance({NAV_SETTLE_MS})")

    how = ""
    seen: List[Tuple[str, bytes]] = []
    for candidate in ("ArrowRight", "PageDown", " ", "ArrowDown", "click"):
        await _open(page, deck, entry)
        await page.js(f"__sc.advance({NAV_SETTLE_MS * 2})")
        if candidate == "click" and await page.js("__sc.centerIsControl()"):
            continue
        before = await _screen(page)
        await turn(candidate)
        after = await _screen(page)
        if differs(before, after):
            how, seen = candidate, [before, after]
            break
    if not how:
        return 1, {"how": "key", "key": "ArrowRight"}
    for _ in range(MAX_PAGES - 2):
        await turn(how)
        cur = await _screen(page)
        if not differs(cur, seen[-1]) or not differs(cur, seen[0]):
            break
        seen.append(cur)
        if progress:
            progress(0.15, i18n.t("翻页识别中：{n} 页", n=len(seen)))
    return len(seen), ({"how": "clickpage"} if how == "click" else {"how": "key", "key": how})


# ---- import ---------------------------------------------------------------------------------------------------------------

def analyze(upload: Path, filename: str, out_dir: Path, progress: Progress = None) -> Dict[str, Any]:
    """Read a deck for the import dialog. Writes out_dir/slide_NNN.png, thumbnails and out_dir/deck/ (site, cache, deck.json).
    Returns {"slides": [...], "engine", "deck"} with the slides in the manifest's format (i from 1)."""
    deck_dir = out_dir / "deck"
    files = unpack(upload, filename, deck_dir)
    if progress:
        progress(0.06, i18n.t("在后台浏览器里打开网页…"))
    return cdp.run(_analyze(deck_dir, files, out_dir, progress))


async def _analyze(deck_dir: Path, files: Dict[str, Any], out_dir: Path, progress: Progress) -> Dict[str, Any]:
    from .slides import THUMB_WIDTH
    browser = cdp.Browser()
    await browser.start()
    try:
        page = await Page.open(browser, deck_dir, save=True)
        major = re.search(r"/(\d+)\.", browser.version or "")
        deck: Dict[str, Any] = {"version": 1, "id": uuid.uuid4().hex[:12], "entry": files["entry"], "pages": files["pages"],
                                "browser": browser.name + (f" {major.group(1)}" if major else ""), "viewport": [VIEW_W, VIEW_H]}
        if files["pages"]:
            deck.update(kind="pages", mode="one", count=len(files["pages"]), nav={"how": "pages"})
        else:
            await page.load(deck["entry"])
            info = await _find_start(page, deck, await page.js("__sc.detect()"))
            for _ in range(4):                           # a web app may add the rest of its slides a few seconds after it starts
                await page.js("__sc.advance(1500)")
                await page.net.idle(1.5)
                later = await page.js("__sc.detect()")
                if int(later.get("count", 0)) > int(info.get("count", 0)):
                    info = later
            if info.get("found") and int(info.get("count", 0)) >= 1:
                deck.update(kind=info["kind"], mode=info["mode"], count=min(MAX_PAGES, int(info["count"])))
                if progress:
                    progress(0.1, i18n.t("找到 {n} 页，识别翻页方式…", n=deck["count"]))
                deck["nav"] = await _find_nav(page, deck, info)
                if deck["nav"]["how"] == "none" and deck["count"] > 1:
                    deck["count"] = 1
            else:
                n, nav = await _frames_nav(page, deck, progress)
                deck.update(kind="frames", mode="one", count=n, nav=nav)
        n = deck["count"]
        slides: List[Dict[str, Any]] = []
        panels: List[Dict[str, str]] = []
        skipped: List[int] = []
        for k in range(n):
            idx = k if deck["kind"] not in ("pages", "frames") else -1
            builds, texts = 0, []
            try:
                await goto(page, deck, k)
                await page.js(f"__sc.advance({SETTLE_MS})")
                seen_panels = await page.js("__sc.panels()") or {}
                # the still shows the whole slide, every part of it in: through the deck's API (fragments), else by pressing on below
                if await page.js("__sc.allBuilds()"):
                    await page.js(f"__sc.advance({SETTLE_MS})")
                w, h, s = await _still(page, k, idx, out_dir, THUMB_WIDTH)
                builds, texts = await _builds(page, deck, k, s)
                if builds and deck["kind"] not in ("reveal", "shower"):
                    await goto(page, deck, k)        # pressing on went past the slide: back to its start, then show its parts
                    for _ in range(builds):
                        await page.press((deck.get("nav") or {}).get("key", "ArrowRight"))
                        await page.net.idle(1.5)
                        await page.js(f"__sc.advance({NAV_SETTLE_MS})")
                    await page.js(f"__sc.advance({SETTLE_MS})")
                    w, h, s = await _still(page, k, idx, out_dir, THUMB_WIDTH)
            except WebDeckError:
                skipped.append(k + 1)                # one slide that can't be shown doesn't take the others with it
                continue
            panels.append(seen_panels)
            if (deck.get("nav") or {}).get("how") in ("key", "click"):
                # presses that show parts, not slides: the way to a later slide takes this many more
                deck["nav"]["extra"] = int(deck["nav"].get("extra", 0)) + builds
            slides.append({"i": k + 1, "title": (s.get("title") or "")[:120], "text": s.get("text") or "",
                           "notes": (s.get("notes") or "").strip(), "hidden": False, "w": w, "h": h, "videos": [],
                           "web": {"controls": s.get("controls") or [], "blocks": s.get("blocks") or [], "builds": builds,
                                   "build_texts": texts}})
            if progress:
                progress(0.12 + 0.8 * (k + 1) / n, i18n.t("读取第 {i}/{n} 页", i=k + 1, n=n))
        if not slides:
            raise WebDeckError(i18n.t("第 {n} 页没有显示出来", n=skipped[0] if skipped else 1))
        deck["skipped"] = skipped
        _notes_from_panels(slides, panels)
        if deck["kind"] != "pages" and sum(1 for s in slides if s["notes"]) < len(slides) / 2:
            # notes that exist only in the deck's script (a presenter view shows them when asked)
            for s, text in zip(slides, await page.js(f"__sc.scriptNotes({len(slides)})") or []):
                if text and not s["notes"]:
                    s["notes"] = text.strip()
        page.net.write_index()
        deck["missing"] = page.net.missing[:20]
        deck["failed"] = page.net.failed[:20]
        (deck_dir / "deck.json").write_text(json.dumps(deck, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"slides": slides, "engine": deck["browser"], "deck": deck}
    finally:
        await browser.close()


async def _still(page: Page, k: int, idx: int, out_dir: Path, thumb_width: int) -> Tuple[int, int, Dict[str, Any]]:
    """The picture of slide k as it is now (presenter's chrome hidden): slide_NNN.png and its thumbnail. Returns its size and what
    the helper reads from the slide (title, text, notes, controls, fragments)."""
    await page.js(f"__sc.ready({idx})", timeout=20.0)
    await page.net.idle()
    rect = await page.js(f"__sc.isolate({idx})")
    if not rect or rect["w"] < 50 or rect["h"] < 50:
        raise WebDeckError(i18n.t("第 {n} 页没有显示出来", n=k + 1))
    await page.metrics(min(3.0, STILL_WIDTH / rect["w"]))
    rect = await page.js(f"__sc.stageRect({idx})")
    png = await page.shot({"x": rect["x"], "y": rect["y"], "width": rect["w"], "height": rect["h"]}, fmt="png")
    await page.metrics(1.0)
    still = out_dir / f"slide_{k + 1:03d}.png"
    still.write_bytes(png)
    with Image.open(still) as im:
        w, h = im.size
        th = im.convert("RGB")
        th.thumbnail((thumb_width, thumb_width))
        th.save(out_dir / f"thumb_{k + 1:03d}.jpg", "JPEG", quality=82)
    return w, h, (await page.js(f"__sc.slide({idx})") or {})


async def _builds(page: Page, deck: Dict[str, Any], k: int, s: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Parts of slide k shown one at a time (→ shows the next part before going to the next slide)."""
    if deck["kind"] in ("reveal", "shower"):         # their API lists them: reveal.js fragments, Shower's inner steps
        texts = [t for t in s.get("fragments") or []]
        return len(texts), texts
    if deck["kind"] in ("pages", "impress", "swiper", "frames") or deck.get("mode") == "scroll":
        return 0, []
    if (deck.get("nav") or {}).get("how") not in ("key", "hash", "classes", "click"):
        return 0, []
    key = (deck.get("nav") or {}).get("key", "ArrowRight")
    # told by what is on the screen, not only by its text: a press may just light up the next line. What moves by itself doesn't count
    await page.js(f"__sc.advance({NAV_SETTLE_MS})")
    first = await _screen(page)
    noise = 0
    text_moves = False
    before = first
    for _ in range(3):
        await page.js(f"__sc.advance({NAV_SETTLE_MS})")
        cur = await _screen(page)
        noise = max(noise, _blocks(before[1], cur[1]))
        text_moves = text_moves or before[0] != cur[0]
        before = cur
    limit = max(2, 3 * noise + 2)
    last = k >= int(deck.get("count") or 0) - 1      # (no next slide to tell a press from: only a new text counts there)
    if last and text_moves:
        return 0, []
    n = 0
    for _ in range(30):
        await page.press(key)
        await page.net.idle(1.5)
        await page.js(f"__sc.advance({NAV_SETTLE_MS})")
        if await page.js("__sc.active()") != k:
            break
        cur = await _screen(page)
        new_text = not text_moves and cur[0] != before[0]
        if not new_text and (last or _blocks(cur[1], before[1]) < limit):
            break
        before = cur
        n += 1
    return n, []


def _notes_from_panels(slides: List[Dict[str, Any]], panels: List[Dict[str, str]]) -> None:
    """A deck that shows its notes in a panel next to the slide (presenter view) rather than in the slides themselves."""
    if len(slides) < 2 or sum(1 for s in slides if s["notes"]) >= len(slides) / 2:
        return
    keys = set().union(*panels) if panels else set()
    best, best_rank = "", (0, 0)
    for key in sorted(keys):
        texts = [p.get(key, "") for p in panels]
        distinct = len({t for t in texts if t})
        # the most slides told apart; of those the innermost box (a panel's own heading such as "Notes" left out)
        rank = (distinct, -sum(len(t) for t in texts))
        if distinct >= max(2, len(slides) // 2) and (not best or rank > best_rank):
            best, best_rank = key, rank
    if best:
        for s, p in zip(slides, panels):
            if not s["notes"]:
                s["notes"] = p.get(best, "")


def install(import_dir: Path, pid: str) -> None:
    """Copy a deck read by `analyze` into a project."""
    dst = web_dir(pid)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(import_dir / "deck", dst)


def load_deck(pid: str) -> Optional[Dict[str, Any]]:
    p = web_dir(pid) / "deck.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def controls_of(raw: List[Dict[str, Any]]) -> List[WebControl]:
    out = []
    for c in raw or []:
        try:
            out.append(WebControl(**{k: c[k] for k in WebControl.model_fields if k in c}))
        except Exception:  # noqa: BLE001 — a control that can't be described is just left out
            continue
    return out


EXPLORE_PER_SLIDE = 6
EXPLORE_TOTAL = 40


def explore(proj: Project, steps: List[Step], progress: Progress = None) -> None:
    """Press the buttons of these slides, one at a time on a freshly opened page, and note what each brings up: for the AI that writes
    their narration. Nothing depends on it; if it fails, the buttons simply go without that."""
    deck = load_deck(proj.id)
    todo = [(st, c) for st in steps if st.web is not None for c in st.web.controls[:EXPLORE_PER_SLIDE]][:EXPLORE_TOTAL]
    if deck is None or not todo:
        return
    try:
        cdp.run(_explore(proj.id, deck, todo, progress))
    except (WebDeckError, cdp.CDPError, OSError):
        pass


async def _explore(pid: str, deck: Dict[str, Any], todo: List[Tuple[Step, WebControl]], progress: Progress) -> None:
    browser = cdp.Browser()
    await browser.start()
    try:
        page = await Page.open(browser, web_dir(pid))
        for n, (st, c) in enumerate(todo):
            if progress:
                progress(n / len(todo), i18n.t("试点页面上的按钮 {i}/{n}", i=n + 1, n=len(todo)))
            k = st.web.page
            idx = k if deck.get("kind") not in ("pages", "frames") else -1
            await goto(page, deck, k)
            await page.js(f"__sc.advance({SETTLE_MS})")
            before = (await page.js(f"__sc.slide({idx})") or {}).get("text") or ""
            r = await page.js(f"__sc.rect({idx}, {json.dumps(c.path)})")
            if not r:
                continue
            await page.click(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            await page.net.idle(1.5)
            await page.js("__sc.advance(3000)")
            if idx >= 0 and await page.js("__sc.active()") != k:
                continue                                 # it turned the page: not something shown on this slide
            after = (await page.js(f"__sc.slide({idx})") or {}).get("text") or ""
            c.effect = _new_lines(before, after)
    finally:
        await browser.close()


def _new_lines(before: str, after: str) -> str:
    have = {x.strip() for x in before.splitlines()}
    return " / ".join(x.strip() for x in after.splitlines() if x.strip() and x.strip() not in have)[:300]


def blocks_of(raw: List[Dict[str, Any]]) -> List[WebBlock]:
    out = []
    for b in raw or []:
        try:
            out.append(WebBlock(**{k: b[k] for k in WebBlock.model_fields if k in b}))
        except Exception:  # noqa: BLE001 — idem
            continue
    return out


# ---- recording --------------------------------------------------------------------------------------------------------------

def live(step: Step) -> bool:
    return step.kind == "slide" and step.web is not None and step.web.live


class Pointer:
    """The mouse pointer drawn into the page: travels to a control, clicks (a ring), lingers, fades away."""
    MOVE = 0.55
    FADE = 0.2
    LINGER = 1.6
    RING = 0.55

    def __init__(self, stage: Dict[str, float], show: bool):
        self.stage, self.show = stage, show
        self.size = max(16.0, stage["h"] * 0.042)
        self.x = self.y = None                         # type: Optional[float]
        self.seg: Optional[Tuple[float, float, float, float, float, float]] = None   # t0, t1, x0, y0, x1, y1
        self.visible_from = -1.0
        self.last_click = -1e9
        self.ring_at: Optional[Tuple[float, float, float]] = None
        self.hide_after = math.inf

    def go(self, t0: float, t1: float, x: float, y: float) -> None:
        if self.x is None or t0 > self.hide_after:
            # comes in from below right of the target
            self.x = min(self.stage["x"] + self.stage["w"] * 0.97, x + self.stage["w"] * 0.12)
            self.y = min(self.stage["y"] + self.stage["h"] * 0.97, y + self.stage["h"] * 0.16)
            self.visible_from = t0
        self.seg = (t0, max(t1, t0 + 0.05), self.x, self.y, x, y)
        self.hide_after = math.inf

    def clicked(self, t: float, x: float, y: float, next_t: Optional[float]) -> None:
        self.last_click = t
        self.ring_at = (t, x, y)
        self.hide_after = math.inf if next_t is not None and next_t - self.MOVE - t < self.LINGER else t + self.LINGER

    def at(self, t: float) -> Optional[Dict[str, Any]]:
        if self.x is None or not self.show:
            return None
        if self.seg is not None:
            t0, t1, x0, y0, x1, y1 = self.seg
            u = min(1.0, max(0.0, (t - t0) / (t1 - t0)))
            e = u * u * (3 - 2 * u)                  # smooth start and stop
            self.x, self.y = x0 + (x1 - x0) * e, y0 + (y1 - y0) * e
            if u >= 1.0:
                self.seg = None
        o = min(1.0, max(0.0, (t - self.visible_from) / self.FADE))
        if t > self.hide_after:
            o *= max(0.0, 1.0 - (t - self.hide_after) / self.FADE)
        if o <= 0.001:
            return None
        st: Dict[str, Any] = {"x": round(self.x, 2), "y": round(self.y, 2), "o": round(o, 3), "size": round(self.size, 2)}
        if self.ring_at is not None:
            p = (t - self.ring_at[0]) / self.RING
            if 0 <= p < 1:
                st["ring"] = {"x": round(self.ring_at[1], 2), "y": round(self.ring_at[2], 2), "p": round(p, 3),
                              "d": round(self.size * 1.9, 2)}
        return st

    def hidden(self, t: float) -> bool:
        return t > self.hide_after + self.FADE


class Spotlight:
    """The highlight on the piece of content being talked about: the piece framed in the accent colour, the rest of the slide a little
    darker. It comes in, moves on to the next piece, goes away."""
    FADE = 0.25
    MOVE = 0.32
    DIM = 0.24

    def __init__(self, stage: Dict[str, float], color: str):
        self.stage, self.color = stage, color
        self.pad = max(4.0, stage["h"] * 0.009)
        self.border = max(2.0, stage["h"] * 0.0032)
        self.radius = max(6.0, stage["h"] * 0.012)
        self.glow = max(8.0, stage["h"] * 0.02)
        self.box: Optional[Tuple[float, float, float, float]] = None     # x, y, w, h
        self.seg: Optional[Tuple[float, Tuple[float, float, float, float], Tuple[float, float, float, float]]] = None   # t0, from, to
        self.since = -1.0
        self.until = -1.0

    def go(self, t: float, until: float, r: Dict[str, float]) -> None:
        s = self.stage
        x0, y0 = max(s["x"], r["x"] - self.pad), max(s["y"], r["y"] - self.pad)
        x1, y1 = min(s["x"] + s["w"], r["x"] + r["w"] + self.pad), min(s["y"] + s["h"], r["y"] + r["h"] + self.pad)
        box = (x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))
        if self.box is not None and t <= self.until + self.FADE:
            self.seg = (t, self.box, box)            # still lit: it moves there
        else:
            self.box, self.seg, self.since = box, None, t
        self.until = max(until, t + 2 * self.FADE)

    def lit(self, t: float) -> bool:
        return self.box is not None and t <= self.until

    def track(self, r: Dict[str, float]) -> None:
        """The piece moved (it was still coming in, the page changed around it): the frame stays on it."""
        s = self.stage
        x0, y0 = max(s["x"], r["x"] - self.pad), max(s["y"], r["y"] - self.pad)
        x1, y1 = min(s["x"] + s["w"], r["x"] + r["w"] + self.pad), min(s["y"] + s["h"], r["y"] + r["h"] + self.pad)
        box = (x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))
        cur = self.seg[2] if self.seg is not None else self.box
        if cur is not None and max(abs(p - q) for p, q in zip(cur, box)) > 1.5:
            if self.seg is not None:
                self.seg = (self.seg[0], self.seg[1], box)
            else:
                self.box = box

    def drop(self, t: float) -> None:
        self.until = min(self.until, t)

    def at(self, t: float) -> Optional[Dict[str, Any]]:
        if self.box is None:
            return None
        if self.seg is not None:
            t0, a, b = self.seg
            u = min(1.0, max(0.0, (t - t0) / self.MOVE))
            e = u * u * (3 - 2 * u)
            self.box = (a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e, a[2] + (b[2] - a[2]) * e, a[3] + (b[3] - a[3]) * e)
            if u >= 1.0:
                self.seg = None
        o = min(1.0, max(0.0, (t - self.since) / self.FADE))
        if t > self.until:
            o *= max(0.0, 1.0 - (t - self.until) / self.FADE)
        if o <= 0.001:
            return None
        x, y, w, h = self.box
        return {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2), "o": round(o, 3), "r": round(self.radius, 1),
                "b": round(self.border, 1), "c": self.color, "d": self.DIM, "g": round(self.glow, 1)}


class Encoder:
    """JPEG frames in, an H.264 clip of exactly w×h out (ffmpeg reads the stream; duplicate frames cost nothing to encode)."""

    def __init__(self, path: Path, w: int, h: int, fps: int):
        self.path = path
        self.log = path.with_suffix(".log")
        self._log = open(self.log, "wb")
        # the input is said to be JPEG: guessing it from the first frames fails when they are tiny (a slide still blank before its
        # entrance animation starts)
        self.proc = subprocess.Popen(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps),
             "-i", "pipe:0", "-vf", f"scale={w}:{h}:flags=bicubic,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "15", "-r", str(fps), "-g", str(fps * 2), "-movflags", "+faststart", "-an", str(path)],
            stdin=subprocess.PIPE, stdout=self._log, stderr=self._log, creationflags=CREATE_NO_WINDOW)
        self.frames = 0

    async def write(self, data: bytes) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.proc.stdin.write, data)
        except OSError:                                  # ffmpeg stopped reading: say why
            self.kill()
            err = self.log.read_text(encoding="utf-8", errors="ignore")[-600:] if self.log.exists() else ""
            raise WebDeckError(i18n.t("录制的画面没能编码：{error}", error=err.strip() or "ffmpeg"))
        self.frames += 1

    def finish(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait()
        self._log.close()
        if self.proc.returncode != 0 or not self.path.exists():
            err = self.log.read_text(encoding="utf-8", errors="ignore")[-600:]
            raise WebDeckError(i18n.t("录制的画面没能编码：{error}", error=err))
        self.log.unlink(missing_ok=True)

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass
        try:
            self.proc.stdin.close()                      # (data still buffered for the dead process is dropped quietly)
        except Exception:
            pass
        try:
            self._log.close()
        except Exception:
            pass


class Shot:
    """What one recording needs: the slide, its length, its actions (seconds), the picture size."""

    def __init__(self, step: Step, length: float, actions: List[Tuple[float, Any]], box: Tuple[int, int], key: str,
                 out: Path, cursor: bool, extend: bool, accent: str = "#FF5C39"):
        self.step, self.length, self.actions, self.box, self.key = step, length, actions, box, key
        self.out, self.cursor, self.extend, self.accent = out, cursor, extend, accent


def clip_key(deck: Dict[str, Any], step: Step, length: float, actions: List[Tuple[float, Any]], box: Tuple[int, int],
             fps: int, cursor: bool, accent: str = "#FF5C39") -> str:
    page_js()
    w = step.web

    def path(a: Any) -> str:
        items = w.blocks if a.kind == "focus" else w.controls
        return items[a.control].path if 0 <= a.control < len(items) else ""

    data = {"v": CAPTURE_VERSION, "js": _JS.get("hash"), "deck": deck.get("id"), "nav": deck.get("nav"), "page": w.page,
            "len": round(length, 2), "box": box, "fps": fps, "cursor": cursor, "accent": accent if any(a.kind == "focus" for _, a in actions) else "",
            "acts": [(round(t, 2), a.kind, path(a), round(a.until, 2)) for t, a in actions]}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def cached(pid: str, step: Step, key: str) -> Optional[Path]:
    c = step.web_clip
    if c is None or c.key != key or not c.file:
        return None
    p = storage.project_dir(pid) / "media" / c.file
    return p if p.is_file() else None


def record(proj: Project, shots: List[Shot], fps: int, progress: Progress = None) -> Dict[str, Any]:
    """Record the slides; returns {step id: (clip path, length)} and the ones that failed {step id: error} under "errors"."""
    deck = load_deck(proj.id)
    if deck is None:
        raise WebDeckError(i18n.t("这个项目里没有网页演示稿的文件了"))
    return cdp.run(_record_all(proj.id, deck, shots, fps, progress))


async def _record_all(pid: str, deck: Dict[str, Any], shots: List[Shot], fps: int, progress: Progress) -> Dict[str, Any]:
    out: Dict[str, Any] = {"clips": {}, "errors": {}}
    total = sum(max(1, int(round(s.length * fps))) for s in shots) or 1
    done = [0]
    queue: asyncio.Queue = asyncio.Queue()
    for s in shots:
        queue.put_nowait(s)
    browser = cdp.Browser()
    await browser.start()
    started = time.monotonic()

    def tick(n: int = 1) -> None:
        done[0] += n
        if progress and done[0] % 10 == 0:
            frac = min(0.999, done[0] / total)
            eta = (time.monotonic() - started) / max(0.01, frac) * (1 - frac)
            progress(frac, i18n.t("录制网页动画 {done}/{total} 帧，预计还需 {sec} 秒", done=done[0], total=total, sec=int(eta)))

    async def worker() -> None:
        page = await Page.open(browser, web_dir(pid))
        try:
            while not queue.empty():
                s: Shot = queue.get_nowait()
                try:
                    out["clips"][s.step.id] = await _record_one(page, deck, s, fps, tick)
                except (WebDeckError, cdp.CDPError, OSError) as e:
                    out["errors"][s.step.id] = str(e)[:300]
        finally:
            await page.s.close()

    try:
        n = max(1, min(WORKERS_MAX, len(shots), (os.cpu_count() or 2) // 2))
        await asyncio.gather(*(worker() for _ in range(n)))
    finally:
        await browser.close()
    return out


async def _record_one(page: Page, deck: Dict[str, Any], s: Shot, fps: int, tick: Callable[[int], None]) -> Tuple[Path, float]:
    w = s.step.web
    k = w.page
    idx = k if deck.get("kind") not in ("pages", "frames") else -1
    await goto(page, deck, k)
    await page.net.idle()
    await page.js(f"__sc.prepareMedia({idx})", timeout=180.0)
    rect = await page.js(f"__sc.isolate({idx})")
    if not rect or rect["w"] < 20 or rect["h"] < 20:
        raise WebDeckError(i18n.t("第 {n} 页没有显示出来", n=k + 1))
    bw, bh = s.box
    await page.metrics(min(4.0, bw / rect["w"]))
    rect = await page.js(f"__sc.stageRect({idx})")
    clip = {"x": rect["x"], "y": rect["y"], "width": rect["w"], "height": rect["h"]}
    await page.js(f"__sc.restart({idx})")
    clock0 = float(await page.js("__sc.now") or 0)     # the page's clock now: getting here took a while (keys pressed on the way)
    tmp = s.out.with_name(s.out.stem + ".part.mp4")
    enc = Encoder(tmp, bw, bh, fps)
    pointer = Pointer(rect, s.cursor)
    spot = Spotlight(rect, s.accent)
    spots = [x for x in s.actions if x[1].kind == "focus"]       # (nothing has to travel to these: they go by their own times)
    lit_block: Any = None
    acts = [x for x in s.actions if x[1].kind != "focus"]
    n = max(1, int(round(s.length * fps)))
    last: Optional[bytes] = None
    pending_move: Optional[Tuple[float, Any, Optional[Dict[str, float]]]] = None    # (t, action, target) being walked to
    last_act = -1e9
    mut_hist: deque = deque()
    stop_at: Optional[float] = None
    moved_away = True
    f = 0
    # a deck that turns its pages by itself (auto-play), or a click that turned the page: the rest would be another slide
    watch = idx >= 0 and deck.get("mode") != "scroll"
    away = 0
    try:
        while True:
            t = f / fps
            if watch and f % max(1, fps // 2) == 0:
                now_showing = await page.js("__sc.active()")
                away = away + 1 if now_showing >= 0 and now_showing != k else 0
                if away >= 2:
                    raise WebDeckError(i18n.t("这一页录制时自己翻走了，演示稿可能开了自动播放"))
            acted = False
            while spots and spots[0][0] <= t + 1e-6:
                _, a = spots.pop(0)
                b = w.blocks[a.control]
                r = await page.js(f"__sc.blockRect({idx}, {json.dumps(b.path)}, {json.dumps(b.text)})")
                if r:                                # (not on the page just now, behind a tab say: nothing to light up)
                    spot.go(t, a.until, r)
                    lit_block = b
            if lit_block is not None and spot.lit(t) and f % max(1, fps // 8) == 0:
                r = await page.js(f"__sc.blockRect({idx}, {json.dumps(lit_block.path)}, {json.dumps(lit_block.text)})")
                if r:
                    spot.track(r)
                else:
                    spot.drop(t)
            # the pointer sets off for the next click / hover a moment before it
            if pending_move is None and acts and acts[0][1].kind in ("click", "hover") and t >= acts[0][0] - Pointer.MOVE:
                ta, a = acts[0]
                c = w.controls[a.control]
                r = await page.js(f"__sc.rect({idx}, {json.dumps(c.path)})")
                tgt = None if not r else {"x": r["x"] + r["w"] / 2, "y": r["y"] + r["h"] / 2}
                pending_move = (ta, a, tgt)
                if tgt is not None:
                    pointer.go(t, ta, tgt["x"], tgt["y"])
            while acts and acts[0][0] <= t + 1e-6:
                ta, a = acts.pop(0)
                if a.kind in ("click", "hover"):
                    tgt = pending_move[2] if pending_move is not None and pending_move[1] is a else None
                    if tgt is None:
                        r = await page.js(f"__sc.rect({idx}, {json.dumps(w.controls[a.control].path)})")
                        tgt = None if not r else {"x": r["x"] + r["w"] / 2, "y": r["y"] + r["h"] / 2}
                    pending_move = None
                    if tgt is not None:
                        if a.kind == "click":
                            await page.click(tgt["x"], tgt["y"])
                            pointer.clicked(t, tgt["x"], tgt["y"], acts[0][0] if acts else None)
                        else:
                            await page.mouse("mouseMoved", tgt["x"], tgt["y"])
                            pointer.clicked(t, tgt["x"], tgt["y"], acts[0][0] if acts else None)
                            pointer.ring_at = None
                        moved_away = False
                elif a.kind == "next":                 # the slide's next part: through the deck's API, else its key
                    if not await page.js("__sc.nextBuild()"):
                        await page.press((deck.get("nav") or {}).get("key", "ArrowRight"))
                acted = True
                last_act = t
            ui = pointer.at(t)
            if ui is not None and pointer.seg is not None:
                await page.mouse("mouseMoved", ui["x"], ui["y"])     # hover effects follow the pointer
            if not moved_away and pointer.hidden(t):
                await page.mouse("mouseMoved", VIEW_W - 1, VIEW_H - 1)
                moved_away = True
            lit = spot.at(t)
            state = {**(ui or {}), "f": lit} if lit else ui
            r = await page.js(f"__sc.frame({clock0 + t * 1000:.3f}, {json.dumps(state)})")
            if last is None or acted or r.get("changed"):
                last = await page.shot(clip)
            await enc.write(last)
            tick(1)
            f += 1
            if f < n:
                continue
            # the narration is over: keep going while what the last click started still moves
            t = f / fps
            if stop_at is not None:
                if t >= stop_at:
                    break
                continue
            if not s.extend or s.length - last_act > 3.0 or t >= last_act + EXTEND_MAX:
                break
            b = await page.js("__sc.busy()") or {}
            mut_hist.append((t, int(b.get("mutations", 0))))
            while mut_hist and mut_hist[0][0] < t - 0.5:
                mut_hist.popleft()
            quiet = len(mut_hist) > 1 and mut_hist[0][1] == mut_hist[-1][1] and t - mut_hist[0][0] >= 0.45
            if t >= last_act + 1.0 and not b.get("finite") and not b.get("playing") and quiet:
                stop_at = t + 0.8                    # a moment to look at the result
        enc.finish()
    except BaseException:
        enc.kill()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await page.metrics(1.0)
    os.replace(tmp, s.out)
    return s.out, f / fps
