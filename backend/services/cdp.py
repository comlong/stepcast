"""A headless Edge / Chrome driven through the DevTools protocol (CDP), for HTML decks.

Edge comes with every Windows 10 / 11, so nothing has to be installed; Chrome is used when Edge is missing. Each job starts its own
browser with a fresh temporary profile (no cookies, no extensions, nothing of the user's own browsing) and closes it at the end.

Only what StepCast needs is here: start / stop, pages (tabs) as flattened target sessions, commands and events over one WebSocket.
The calls are async (aiohttp, already a dependency of the free online voice); `run()` runs a coroutine on a private event loop for
the synchronous callers (import, render threads).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from .. import i18n
from .ffmpeg_util import CREATE_NO_WINDOW

T = TypeVar("T")

START_TIMEOUT = 30.0          # seconds to wait for the browser's DevTools endpoint
CONNECT_TIMEOUT = 20.0        # the WebSocket handshake with it
FIRST_CALL_TIMEOUT = 20.0     # its first commands: a browser that doesn't answer them is replaced by a new one
CALL_TIMEOUT = 60.0           # one protocol command (a screenshot of a heavy page can take a while)

# Edge first: it is part of Windows, so it is there on locked-down company computers too
_CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)
_NAMES = ("msedge", "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable", "chromium",
          "chromium-browser", "chrome")

# Flags of a quiet, private, throttle-free headless browser. The page is the user's own deck, so software WebGL may be used when there is
# no usable GPU (otherwise WebGL decks would show an empty canvas).
_FLAGS = (
    "--headless=new", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    # Edge restarts itself in another process when its parent runs under a compatibility layer: the one started here would exit
    # at once and the real browser would run on unwatched
    "--edge-skip-compat-layer-relaunch",
    "--disable-extensions", "--disable-component-extensions-with-background-pages", "--disable-background-networking",
    "--disable-sync", "--disable-default-apps", "--disable-client-side-phishing-detection", "--disable-domain-reliability",
    "--mute-audio", "--hide-scrollbars", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-ipc-flooding-protection", "--disable-hang-monitor",
    # CalculateNativeWinOcclusion: a page window counted as covered by another one stops painting (no screenshots)
    "--disable-features=Translate,msEdgeTranslate,MediaRouter,OptimizationHints,AutofillServerCommunication,"
    "msUndersideButton,msEdgeSidebarV2,msShorelineToolbar,PaintHolding,CalculateNativeWinOcclusion",
    "--force-color-profile=srgb", "--enable-unsafe-swiftshader", "--autoplay-policy=no-user-gesture-required",
    "--window-size=1920,1080", "--enable-logging=stderr", "--log-level=2",
)


class CDPError(RuntimeError):
    """The browser could not be started or a protocol command failed."""


class PolicyBlocked(CDPError):
    """The browser refuses to be controlled: an administrator turned remote debugging off (a company policy)."""


def find_browser() -> Optional[Path]:
    """Edge, else Chrome; None when neither is installed. Only read from the usual install places and PATH."""
    for raw in _CANDIDATES:
        p = Path(os.path.expandvars(raw))
        if "%" not in str(p) and p.is_file():
            return p
    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def browser_name(path: Optional[Path]) -> str:
    n = (path.name if path else "").lower()
    return "Edge" if "edge" in n else "Chrome" if "chrome" in n else "Chromium" if path else ""


# ---- Windows: the browsers end with StepCast -------------------------------------------------------------------------------------
# Every browser started here goes into one job object of this process, set to end its members when the last handle to it closes —
# when StepCast ends, however it ends (closed window, crash, killed). So a browser of a job that was still running (an import, a
# recording) is never left behind, unseen, until the user logs off. close() remains the normal way; this is the safety net.
_job_lock = threading.Lock()
_job_state: Dict[str, Any] = {"tried": False, "handle": None}


def _job_object() -> Optional[int]:
    if os.name != "nt":
        return None
    with _job_lock:
        if _job_state["tried"]:
            return _job_state["handle"]
        _job_state["tried"] = True
        try:
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k32.CreateJobObjectW(None, None)
            if not h:
                return None
            info = Extended()
            # KILL_ON_JOB_CLOSE; BREAKAWAY_OK: a process the browser starts on purpose outside any job still starts
            info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):   # JobObjectExtendedLimitInformation
                k32.CloseHandle(h)
                return None
            _job_state.update(handle=h, assign=k32.AssignProcessToJobObject)
        except Exception:  # noqa: BLE001 — without it browsers are still closed by close(); only the safety net is missing
            _job_state["handle"] = None
        return _job_state["handle"]


def _adopt(proc: subprocess.Popen) -> None:
    """Put a just-started browser into the job object (before it has started helper processes of its own)."""
    job = _job_object()
    handle = getattr(proc, "_handle", None)
    if job is None or handle is None:
        return
    try:
        _job_state["assign"](job, int(handle))
    except Exception:  # noqa: BLE001
        pass


def run(coro: Awaitable[T]) -> T:
    """Run a coroutine to completion on a new event loop (callers are plain threads: the import job, render workers)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


class Session:
    """One page (tab), attached with a flattened session: commands carry its sessionId over the browser's WebSocket."""

    def __init__(self, conn: "Connection", session_id: str, target_id: str):
        self.conn, self.id, self.target_id = conn, session_id, target_id
        self.handlers: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}
        self.closed = False

    async def send(self, method: str, timeout: float = CALL_TIMEOUT, **params: Any) -> Dict[str, Any]:
        return await self.conn.send(method, params, self.id, timeout)

    def on(self, event: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def evaluate(self, expression: str, await_promise: bool = True, timeout: float = CALL_TIMEOUT) -> Any:
        """Evaluate JavaScript in the page and return its value (JSON-serialisable). A thrown exception becomes CDPError."""
        r = await self.send("Runtime.evaluate", timeout=timeout, expression=expression, awaitPromise=await_promise,
                            returnByValue=True, userGesture=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = ((d.get("exception") or {}).get("description") or d.get("text") or "").strip()
            raise CDPError(msg.splitlines()[0][:400] if msg else "JavaScript error")
        return (r.get("result") or {}).get("value")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.conn.send("Target.closeTarget", {"targetId": self.target_id}, None, 10)
        except Exception:
            pass


class Connection:
    """The browser-level WebSocket: request / response by id, events dispatched to the page sessions."""

    def __init__(self, ws, http):
        self.ws, self.http = ws, http
        self.next_id = 1
        self.pending: Dict[int, asyncio.Future] = {}
        self.sessions: Dict[str, Session] = {}
        self.reader = asyncio.ensure_future(self._read())
        self.dead: Optional[str] = None

    async def _read(self) -> None:
        import aiohttp
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = self.pending.pop(data["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                    continue
                sess = self.sessions.get(data.get("sessionId", ""))
                if sess is None:
                    continue
                for h in list(sess.handlers.get(data.get("method", ""), [])):
                    try:
                        res = h(data.get("params") or {})
                        if asyncio.iscoroutine(res):
                            asyncio.ensure_future(res)
                    except Exception:
                        pass
        except Exception as e:   # noqa: BLE001 — the browser went away; every waiting call fails below
            self.dead = str(e) or e.__class__.__name__
        finally:
            self.dead = self.dead or "closed"
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(i18n.t("浏览器意外退出了")))
            self.pending.clear()

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None,
                   timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        if self.dead:
            raise CDPError(i18n.t("浏览器意外退出了"))
        cid = self.next_id
        self.next_id += 1
        msg: Dict[str, Any] = {"id": cid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.ws.send_str(json.dumps(msg))
        try:
            data = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(cid, None)
            raise CDPError(i18n.t("浏览器没有响应（{method}）", method=method))
        if "error" in data:
            raise CDPError(f"{method}: {data['error'].get('message', '')}")
        return data.get("result") or {}

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass
        try:
            await self.http.close()
        except Exception:
            pass
        self.reader.cancel()


class Browser:
    """A headless browser of its own (temporary profile, killed with its process tree at the end)."""

    def __init__(self, exe: Optional[Path] = None):
        self.exe = exe or find_browser()
        self.proc: Optional[subprocess.Popen] = None
        self.profile: Optional[Path] = None
        self.conn: Optional[Connection] = None
        self.version = ""
        self._first: Optional[Session] = None     # its first page, opened to see that it works

    @property
    def name(self) -> str:
        return browser_name(self.exe)

    async def start(self) -> "Browser":
        """Start the browser and open a first page. Now and then a freshly started browser doesn't answer: it is replaced by a new one,
        once. A company policy against remote control won't change with a second try."""
        if self.exe is None:
            raise CDPError(i18n.t("这台电脑上没有找到 Microsoft Edge 或 Google Chrome，没法打开网页演示稿。"))
        for attempt in range(2):
            try:
                await self._launch()
                self._first = await self._window(FIRST_CALL_TIMEOUT)
                return self
            except PolicyBlocked:
                raise
            except (CDPError, OSError) as e:
                await self.close()
                if attempt:
                    if isinstance(e, CDPError):
                        raise
                    raise CDPError(i18n.t("{browser} 没能在后台启动：{error}", browser=self.name, error=str(e)[:300])) from e
            except BaseException:               # whatever it was: nothing of this browser stays behind
                await self.close()
                raise
        return self

    async def _launch(self) -> None:
        import aiohttp
        self.profile = Path(tempfile.mkdtemp(prefix="stepcast-browser-"))
        log = self.profile / "browser.log"
        with open(log, "wb") as out:
            self.proc = subprocess.Popen([str(self.exe), *_FLAGS, f"--user-data-dir={self.profile}", "about:blank"],
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=CREATE_NO_WINDOW)
        _adopt(self.proc)
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + START_TIMEOUT
        ws_url = ""
        while time.monotonic() < deadline:
            if port_file.exists():
                try:
                    lines = port_file.read_text(encoding="utf-8", errors="ignore").split()
                except OSError:
                    lines = []                      # the browser is writing it at this very moment (Windows: a sharing violation)
                if len(lines) >= 2 and lines[0].isdigit():
                    ws_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                    break
            code = self.proc.poll()
            if code is not None and code != 0:
                break                               # it failed (exit 0: it may have handed over to another process; wait)
            await asyncio.sleep(0.1)
        if not ws_url:
            text = log.read_text(encoding="utf-8", errors="ignore") if log.exists() else ""
            code = self.proc.poll()
            _kill_profile(self.profile)             # a browser that went on in another process, unreachable
            await self.close()
            low = text.lower()
            if "remote debugging" in low and ("disallow" in low or "policy" in low or "disabled" in low):
                raise PolicyBlocked(i18n.t("这台电脑的 {browser} 被公司策略禁止了自动控制（远程调试），没法录制网页演示稿。"
                                           "可以请 IT 允许，或者装上另一个浏览器（Edge / Chrome）再试。", browser=self.name))
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            detail = lines[-1][:300] if lines else i18n.t("没有输出")
            if code is not None:
                detail += f" (exit {code})"
            raise CDPError(i18n.t("{browser} 没能在后台启动：{error}", browser=self.name, error=detail))
        http = aiohttp.ClientSession()
        try:
            ws = await asyncio.wait_for(http.ws_connect(ws_url, max_msg_size=0, autoping=True, heartbeat=None), CONNECT_TIMEOUT)
        except Exception as e:
            await http.close()
            await self.close()
            raise CDPError(i18n.t("{browser} 没能在后台启动：{error}", browser=self.name, error=str(e)[:300] or e.__class__.__name__))
        self.conn = Connection(ws, http)
        self.version = (await self.conn.send("Browser.getVersion", None, None, FIRST_CALL_TIMEOUT)).get("product", "")

    async def _window(self, timeout: float) -> Session:
        assert self.conn is not None
        # a window of its own: a background tab of a shared window produces no frames, and its screenshots would hang
        r = await self.conn.send("Target.createTarget", {"url": "about:blank", "newWindow": True}, None, timeout)
        tid = r["targetId"]
        a = await self.conn.send("Target.attachToTarget", {"targetId": tid, "flatten": True}, None, timeout)
        sess = Session(self.conn, a["sessionId"], tid)
        self.conn.sessions[sess.id] = sess
        return sess

    async def new_page(self) -> Session:
        if self._first is not None:
            first, self._first = self._first, None
            return first
        return await self._window(CALL_TIMEOUT)

    async def close(self) -> None:
        self._first = None
        if self.conn is not None:
            try:
                await self.conn.send("Browser.close", None, None, 5)
            except Exception:
                pass
            await self.conn.close()
            self.conn = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                _kill_tree(self.proc.pid)
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
            self.proc = None
        if self.profile is not None:
            for attempt in range(12):       # the browser's helper processes may still hold files for a moment
                shutil.rmtree(self.profile, ignore_errors=True)
                if not self.profile.exists():
                    break
                if attempt == 8:
                    _kill_profile(self.profile)          # something of it is still running: end it
                await asyncio.sleep(0.3)
            self.profile = None

    async def __aenter__(self) -> "Browser":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()


def _kill_profile(profile: Path) -> None:
    """End every browser process using this temporary profile (found by its command line; Windows only)."""
    if os.name != "nt":
        return
    needle = str(profile).replace("'", "''")
    script = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_tree(pid: int) -> None:
    """End the browser and its helper processes (renderers, GPU process)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
