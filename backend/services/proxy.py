"""The proxy for the free online voice (edge-tts).

edge-tts talks to its service through aiohttp, which uses no proxy unless it is handed one, so a computer that reaches the internet only
through a proxy got Windows' offline voice for every paragraph. Used, in this order, the first that accepts a connection right now:

1. the environment (HTTPS_PROXY / HTTP_PROXY / ALL_PROXY) and the Windows system proxy — both only read, never changed;
2. the proxy of the assistant driving StepCast: an assistant's sandbox (WorkBuddy) lets programs out only through a local proxy of its own,
   on a port that changes every session; the skill's script writes it to <home>/runtime/assistant-proxy.txt each time it runs.

None of them reachable: a direct connection.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .. import config

TTL = 20.0                # seconds a decision is kept (a voice-over asks for every paragraph)
_lock = threading.Lock()
_cache: Dict[str, tuple] = {}     # host -> (time, proxy)


def assistant_file():
    return config.BASE_DIR / "runtime" / "assistant-proxy.txt"


_USERINFO = re.compile(r"(?<=://)[^/\s'\"<>]+@")


def shown(text: Optional[str]) -> Optional[str]:
    """text with the user name and password taken out of every URL in it (a proxy URL may carry them), for answers, messages
    and logs, which an external assistant reads. The connection itself uses the full URL."""
    return _USERINFO.sub("***@", text) if text else text


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url          # the Windows setting is often just host:port
    return url


def candidates() -> List[str]:
    out: List[str] = []
    try:
        found = urllib.request.getproxies()      # the environment first, otherwise the system proxy
    except Exception:
        found = {}
    for key in ("https", "http", "all"):
        if found.get(key):
            out.append(found[key])
    try:
        out.append(assistant_file().read_text(encoding="utf-8").strip())
    except OSError:
        pass
    seen: List[str] = []
    for u in (_normalize(u) for u in out):
        # aiohttp speaks to HTTP proxies only (an https:// proxy URL means the same CONNECT proxy)
        if u and urlparse(u).scheme in ("http", "https") and u not in seen:
            seen.append(u)
    return seen


def _reachable(url: str, timeout: float = 0.5) -> bool:
    p = urlparse(url)
    if not p.hostname:
        return False
    try:
        # the port aiohttp will connect to: without one in the URL, the scheme's own (https:// → 443)
        port = p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((p.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):          # ValueError: a port that isn't a number
        return False


def current(host: str = "") -> Optional[str]:
    """The proxy to reach `host` through, or None for a direct connection."""
    if host:
        try:
            if urllib.request.proxy_bypass(host):     # NO_PROXY / the system's exceptions
                return None
        except Exception:
            pass
    now = time.monotonic()
    with _lock:
        hit = _cache.get(host)
        if hit and now - hit[0] < TTL:
            return hit[1]
    chosen = next((u for u in candidates() if _reachable(u)), None)
    with _lock:
        _cache[host] = (now, chosen)
    return chosen


def forget() -> None:
    with _lock:
        _cache.clear()
