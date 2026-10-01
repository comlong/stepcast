"""Fixes from this review: lines and narration out of sync, intro voice after a voice change, global voice not changing Q&A projects, AI rewrite without notes, retries / parsing / voice-list cache of paid voice services."""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "review3_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
for k in ("MODEL_SPEECH_API_KEY", "VOLC_SPEECH_API_KEY", "MINIMAX_API_KEY", "DASHSCOPE_API_KEY"):
    os.environ.pop(k, None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh", "tts_services": {"doubao": {"api_key": "volc-k"}, "minimax": {"api_key": "mm-k"}}})
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.models import DialogueLine, Step, join_lines  # noqa: E402
from backend.services import dialogue, script_gen, tts, tts_cloud, voice  # noqa: E402

fails = []
FF = shutil.which("ffmpeg")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def mp3(sec=1.0):
    f = DATA / f"s{sec}.mp3"
    if not f.exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000", "-t", str(sec),
                        "-ac", "1", str(f)], check=True)
    return f.read_bytes()


c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}


def dlg_project():
    p = storage.create("问答", "zh-CN")
    p.source = "slides"
    p.settings = {"dialogue": True, "slides_notes_mode": "ignore"}
    p.speakers = dialogue.default_speakers("zh-CN")
    p.voice = p.speakers[0].voice
    st = Step(kind="slide", lines=[DialogueLine(who="host", text="问一句？"), DialogueLine(who="expert", text="答一句。")],
              slide_text="页面文字", slide_notes="机密备注")
    st.narration = st.caption = join_lines(st.lines)
    p.steps = [st]
    storage.save(p)
    return p


print("\n== 1. 整段解说被改掉：旧台词作废 ==")
p = dlg_project()
sid = p.steps[0].id
c.patch(f"/api/projects/{p.id}/steps/{sid}", headers=H, json={"narration": "一整段新解说。", "caption": "一整段新解说。"})
st = storage.load(p.id).steps[0]
check("直接改解说：台词清掉（配音不会再念旧台词）", st.lines == [] and st.narration == "一整段新解说。", st.lines)
p = dlg_project()
sid = p.steps[0].id
c.patch(f"/api/projects/{p.id}/steps/{sid}", headers=H, json={"narration": "问一句？\n答一句。", "title": "x"})
check("解说没变（就是台词全文）：台词保留", len(storage.load(p.id).steps[0].lines) == 2)
p = dlg_project()
js = c.get(f"/api/projects/{p.id}/export/script").json()
js["steps"][0].pop("lines")
js["steps"][0]["narration"] = "导入的整段解说。"
c.post(f"/api/projects/{p.id}/import/script", headers=H, json=js)
st = storage.load(p.id).steps[0]
check("导入只有整段解说的脚本：台词清掉", st.lines == [] and st.narration == "导入的整段解说。", st.lines)
p = dlg_project()
src = DATA / "own.mp3"
src.write_bytes(mp3(1.5))
voice.asr.transcribe = lambda *a, **k: {"text": "我自己录的一段话。", "segments": [], "language": "zh"}
try:
    voice.set_step_voice(p, p.steps[0], src, transcribe=True, replace_text=True, language="zh-CN")
    ok = p.steps[0].lines == [] and p.steps[0].narration.startswith("我自己录")
except Exception as e:  # noqa: BLE001
    ok, p.steps[0].narration = False, str(e)
check("录自己的声音、用转写替换文字：台词清掉", ok, p.steps[0].narration)

print("\n== 2. 片头片尾：换了主持人音色要重新合成 ==")
USED = []


def fake_synth(text, v, out, rate="", volume="", pitch=""):
    USED.append(v)
    out.write_bytes(mp3(1.0))
    return 1.0, []


tts.synth = fake_synth
p = dlg_project()
p.intro = "欢迎。"
storage.save(p)
p = storage.load(p.id)
tts.synth_project(p)
storage.save(p)
n1 = USED.count("zh-CN-XiaoxiaoNeural")
USED.clear()
tts.synth_project(storage.load(p.id))
check("音色没变：片头不重复合成", "zh-CN-XiaoxiaoNeural" not in USED, USED)
pj = storage.load(p.id)
pj.speakers[0].voice = "zh-CN-XiaoyiNeural"
storage.save(pj)
USED.clear()
tts.synth_project(storage.load(p.id))
check("主持人换了音色：片头用新音色重新合成", "zh-CN-XiaoyiNeural" in USED and n1 >= 1, USED)
side = storage.audio_dir(p.id) / "__intro__.txt"
check("片头记下了用的音色", json.loads(side.read_text(encoding="utf-8"))["voice"] == "zh-CN-XiaoyiNeural")
side.write_text("欢迎。", encoding="utf-8")         # format of the previous version: only the text
check("老格式的记录照样认", tts.card_audio(storage.audio_dir(p.id), "intro", "欢迎。", "zh-CN-XiaoyiNeural") is not None)

print("\n== 3. 全局默认音色不改问答项目；改语言时讲者跟着换 ==")
p = dlg_project()
c.patch(f"/api/projects/{p.id}", headers=H, json={"voice": "en-US-GuyNeural"})
pj = storage.load(p.id)
check("问答项目忽略全局音色", pj.voice == "zh-CN-XiaoxiaoNeural" and pj.speakers[0].voice == "zh-CN-XiaoxiaoNeural", pj.voice)
c.patch(f"/api/projects/{p.id}", headers=H, json={"language": "en-US"})
pj = storage.load(p.id)
check("改语言：两位讲者换成英文音色", [s.voice for s in pj.speakers] == ["en-US-AriaNeural", "en-US-GuyNeural"],
      [s.voice for s in pj.speakers])
plain = storage.create("单人", "zh-CN")
c.patch(f"/api/projects/{plain.id}", headers=H, json={"voice": "en-US-GuyNeural"})
check("单人项目照旧跟着改", storage.load(plain.id).voice == "en-US-GuyNeural")

print("\n== 4. AI 改写对话：项目选了「不用备注」就不发备注 ==")


class Rec:
    last = ""

    def chat_json(self, messages, **k):
        Rec.last = messages[-1]["content"]
        return {"lines": [{"who": "host", "text": "新问题？"}, {"who": "expert", "text": "新回答。"}]}


p = dlg_project()
script_gen.rewrite_dialogue(p, p.steps[0], "更短", client=Rec())
check("不用备注：不发备注", "机密备注" not in Rec.last and "页面文字" in Rec.last, Rec.last[:200])
p.settings["slides_notes_mode"] = "verbatim"
script_gen.rewrite_dialogue(p, p.steps[0], "更短", client=Rec())
check("用备注：带上备注", "机密备注" in Rec.last)

print("\n== 5. 商用配音：什么错重试、什么错不重试、字节流、看不懂的返回 ==")


class Resp:
    def __init__(self, status=200, body=None, lines=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})
        self._lines = lines or []
        self.content = b""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def iter_lines(self, decode_unicode=False):
        yield from self._lines


CALLS = []
PLAN = []


def fake_post(url, headers=None, json=None, timeout=None, stream=False):
    CALLS.append(url)
    return PLAN.pop(0) if PLAN else Resp(500, {"x": 1})


tts_cloud.requests.post = fake_post
tts_cloud.time.sleep = lambda s: None
real_synth = tts_cloud.synth
b64 = base64.b64encode(mp3(1.0)).decode()
PLAN[:] = [Resp(lines=[b"event: 352", ('data: {"code":0,"data":"%s"}' % b64).encode(), b'data: {"code":20000000}'])]
dur, _ = real_synth("你好。", "doubao:zh_female_vv_uranus_bigtts", DATA / "d.mp3")
check("豆包：按字节返回的流也能解析", dur > 0.8, dur)
CALLS.clear()
PLAN[:] = [Resp(lines=[b'data: {"code":45000000,"message":"quota"}'])]
try:
    real_synth("你好。", "doubao:zh_female_vv_uranus_bigtts", DATA / "d2.mp3")
except tts_cloud.CloudTTSError as e:
    check("业务错误（额度不够）不白白重试", len(CALLS) == 1 and "quota" in str(e), (len(CALLS), str(e)))
CALLS.clear()
PLAN[:] = [Resp(429, {"e": "rate"}), Resp(503, {"e": "busy"}),
           Resp(body={"data": {"audio": mp3(1.0).hex()}, "base_resp": {"status_code": 0}})]
dur, _ = real_synth("你好。", "minimax:English_Graceful_Lady", DATA / "m.mp3")
check("限流、服务端临时出错：重试后成功", len(CALLS) == 3 and dur > 0.8, len(CALLS))
CALLS.clear()
PLAN[:] = [Resp(200, None, text="<html>gateway</html>")] * 3
try:
    real_synth("你好。", "minimax:English_Graceful_Lady", DATA / "m2.mp3")
except tts_cloud.CloudTTSError as e:
    check("返回的不是 JSON：说清楚", "看不懂" in str(e) and "gateway" in str(e), str(e))
CALLS.clear()
PLAN[:] = [Resp(401, {"error": "bad key"})]
try:
    real_synth("你好。", "minimax:English_Graceful_Lady", DATA / "m3.mp3")
except tts_cloud.CloudTTSError as e:
    check("Key 不对：不重试", len(CALLS) == 1 and "401" in str(e), len(CALLS))

print("\n== 6. MiniMax 音色表取不到：缓存几分钟，不会每次都等超时 ==")
CALLS.clear()
tts_cloud._voice_cache.clear()
PLAN[:] = [Resp(500, {"e": 1})]
t0 = time.time()
v1 = tts_cloud.voices()
v2 = tts_cloud.voices()
check("取失败用内置音色，第二次不再请求", len([u for u in CALLS if u.endswith("/get_voice")]) == 1
      and any(v["name"].startswith("minimax:") for v in v2), CALLS)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
