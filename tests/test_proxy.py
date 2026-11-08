"""The proxy for the free online voice: edge-tts uses none by itself, so on a computer that reaches the internet only through a proxy
(e.g. an assistant's sandbox) every paragraph fell back to Windows' voice, without saying why. Checked offline: a local listening socket
stands in for the proxy, edge-tts and the Windows voice are faked."""
import asyncio
import os
import shutil
import socket
import sys
import wave
from pathlib import Path

SP = Path(sys.argv[1]).resolve()
DATA = SP / "proxy_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
    os.environ.pop(k, None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config.BASE_DIR = DATA                  # where runtime/assistant-proxy.txt is looked for
config._cache = None
config.save({"ui_language": "zh"})
import urllib.request  # noqa: E402

import edge_tts  # noqa: E402

from backend import storage  # noqa: E402
from backend.models import Step  # noqa: E402
from backend.services import proxy, tts  # noqa: E402

# this computer's own Windows proxy settings must not decide the test: only the environment is read
proxy.urllib.request.getproxies = urllib.request.getproxies_environment
proxy.urllib.request.proxy_bypass = urllib.request.proxy_bypass_environment
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(8)
OPEN = f"http://127.0.0.1:{listener.getsockname()[1]}"
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    CLOSED = f"http://127.0.0.1:{s.getsockname()[1]}"         # nothing listens there once this is closed
AFILE = DATA / "runtime" / "assistant-proxy.txt"
AFILE.parent.mkdir(parents=True)


def fresh(**env):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        os.environ.pop(k, None)
    os.environ.update(env)
    proxy.forget()


print("\n== 1. 用哪个代理 ==")
fresh()
check("什么都没有：直连", proxy.current(tts.EDGE_HOST) is None)
fresh(HTTPS_PROXY=OPEN)
check("环境变量里的代理（连得上）：用它", proxy.current(tts.EDGE_HOST) == OPEN)
fresh(HTTPS_PROXY=CLOSED)
check("环境变量里的代理连不上：不用，直连", proxy.current(tts.EDGE_HOST) is None)
fresh()
AFILE.write_text(OPEN, encoding="utf-8")
check("助手（沙箱）的代理文件：环境里没有时用它", proxy.current(tts.EDGE_HOST) == OPEN)
AFILE.write_text(CLOSED, encoding="utf-8")
proxy.forget()
check("代理文件里是旧端口（WorkBuddy 重启过）：不用", proxy.current(tts.EDGE_HOST) is None)
fresh(HTTPS_PROXY=OPEN)
check("环境变量优先于代理文件", proxy.candidates()[0] == OPEN and proxy.current(tts.EDGE_HOST) == OPEN, proxy.candidates())
fresh(HTTP_PROXY=OPEN.replace("http://", ""))
check("只有 host:port：当作 http:// 代理", proxy.current(tts.EDGE_HOST) == OPEN)
fresh(HTTPS_PROXY="socks5://127.0.0.1:1080")
check("SOCKS 代理（edge-tts 用不了）：跳过", "socks5://127.0.0.1:1080" not in proxy.candidates(), proxy.candidates())
fresh(HTTPS_PROXY=OPEN, NO_PROXY="speech.platform.bing.com")
check("NO_PROXY 里写了语音服务：直连", proxy.current(tts.EDGE_HOST) is None)
AFILE.unlink()

print("\n== 2. 在线语音和语音列表都走这个代理 ==")
seen = {}


class FakeCommunicate:
    def __init__(self, text, voice, **kw):
        seen["kw"] = kw

    async def stream(self):
        yield {"type": "audio", "data": b"\xff\xf3" * 600}
        yield {"type": "WordBoundary", "offset": 0, "duration": 5_000_000, "text": "x"}


real_comm, real_list = edge_tts.Communicate, edge_tts.list_voices
edge_tts.Communicate = FakeCommunicate


async def fake_list(**kw):
    seen["list"] = kw
    return []
edge_tts.list_voices = fake_list
try:
    fresh(HTTPS_PROXY=OPEN)
    asyncio.run(tts._edge_synth("你好。", "zh-CN-XiaoxiaoNeural", DATA / "a.mp3", "+0%", "+0%"))
    check("有代理：交给 edge-tts", seen["kw"].get("proxy") == OPEN, seen["kw"])
    fresh()
    asyncio.run(tts._edge_synth("你好。", "zh-CN-XiaoxiaoNeural", DATA / "b.mp3", "+0%", "+0%"))
    check("没有代理：不传（直连）", "proxy" not in seen["kw"], seen["kw"])
    fresh(HTTPS_PROXY=OPEN)
    tts._voices_cache.update({"ts": 0.0, "data": []})
    tts.list_voices(force=True)
    check("语音列表也走代理", seen.get("list", {}).get("proxy") == OPEN, seen.get("list"))
finally:
    edge_tts.Communicate, edge_tts.list_voices = real_comm, real_list

print("\n== 3. 退回 Windows 语音时说出原因 ==")
REASON = "Cannot connect to host speech.platform.bing.com:443 ssl:default [Access is denied]"


async def blocked_edge(text, voice, out, rate, volume, pitch="+0Hz"):
    raise OSError(REASON)


def fake_sapi(text, out, lang):
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000)
    return "Microsoft Huihui"


real_edge, real_sapi = tts._edge_synth, tts._sapi_run
tts._edge_synth, tts._sapi_run = blocked_edge, fake_sapi
tts.EDGE_RETRY_WAIT = 0.01
try:
    fresh()
    proj = storage.create("proxy", "zh-CN")
    for i in range(3):
        st = Step(kind="slide", narration=f"第 {i + 1} 页的解说。")
        st.index = i
        proj.steps.append(st)
    storage.save(proj)
    r = tts.synth_project(proj, voice="zh-CN-XiaoxiaoNeural", only_missing=False)
    check("三段都退回 Windows 语音", r["fallback"] == 3 and r["generated"] == 3, r)
    check("结果里有原因", REASON in r["fallback_reason"], r["fallback_reason"])
    check("提示里也说了原因", "连不上的原因" in r["warning"] and REASON in r["warning"], r["warning"])
    check("说明当时没走代理", r["proxy"] == "", r["proxy"])
finally:
    tts._edge_synth, tts._sapi_run = real_edge, real_sapi

print("\n== 4. 接口 /api/network ==")
from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
fresh(HTTPS_PROXY=OPEN)
n = c.get("/api/network").json()
check("报出语音用的代理和候选", n["voice_proxy"] == OPEN and OPEN in n["candidates"], n)

print("\n== 5. 代理地址里的用户名和密码不外露 ==")
WITH_PW = OPEN.replace("http://", "http://user:secret@")
check("shown() 去掉用户名和密码", proxy.shown(WITH_PW) == OPEN.replace("http://", "http://***@"), proxy.shown(WITH_PW))
check("不带密码的地址、普通文字原样", proxy.shown(OPEN) == OPEN and proxy.shown("a@b, see http://h/x?y=a@b") == "a@b, see http://h/x?y=a@b")
check("报错文字里的也去掉", "secret" not in proxy.shown(f"407, message='Proxy Authentication Required', url=URL('{WITH_PW}')"))
fresh(HTTPS_PROXY=WITH_PW)
check("连接时仍用完整地址（带密码）", proxy.current(tts.EDGE_HOST) == WITH_PW)
n = c.get("/api/network").json()
check("/api/network 里看不到密码", "secret" not in str(n) and "127.0.0.1" in n["voice_proxy"], n)


async def blocked_pw(text, voice, out, rate, volume, pitch="+0Hz"):
    raise OSError(f"proxy said no: {WITH_PW}")
real_edge, real_sapi = tts._edge_synth, tts._sapi_run
tts._edge_synth, tts._sapi_run = blocked_pw, fake_sapi
try:
    proj = storage.create("proxy-pw", "zh-CN")
    proj.steps.append(Step(kind="slide", narration="一段解说。"))
    storage.save(proj)
    r = tts.synth_project(proj, voice="zh-CN-XiaoxiaoNeural", only_missing=False)
    check("配音结果（助手会读到）里也看不到密码", r["fallback"] == 1 and "secret" not in str(r) and "***@" in r["fallback_reason"], r)
finally:
    tts._edge_synth, tts._sapi_run = real_edge, real_sapi

print("\n== 6. 没写端口的代理：按协议的默认端口试连 ==")
tried = []


def fake_connect(addr, timeout=None):
    tried.append(addr)
    raise OSError("refused")
real_connect = proxy.socket.create_connection
proxy.socket.create_connection = fake_connect
try:
    proxy._reachable("https://proxy.example.test")
    proxy._reachable("http://proxy.example.test")
    proxy._reachable("http://proxy.example.test:3128")
    check("https:// 是 443，http:// 是 80，写了端口用写的", [p for _, p in tried] == [443, 80, 3128], tried)
    check("端口不是数字：当作连不上，不报错", proxy._reachable("http://proxy.example.test:abc") is False)
finally:
    proxy.socket.create_connection = real_connect
fresh()
listener.close()

print(f"\n结果: {'全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'}")
