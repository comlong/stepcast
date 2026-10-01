"""商用配音服务（豆包、MiniMax、通义）：请求格式、返回解析、切段拼接、语速、报错、音色列表、设置、和配音流程接上（全部模拟，不联网、不花钱）。"""
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "tts_cloud_data"
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
config.save({"ui_language": "zh"})
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.models import DialogueLine, Step, join_lines  # noqa: E402
from backend.services import dialogue, ffmpeg_util, tts, tts_cloud  # noqa: E402

fails = []
FF = shutil.which("ffmpeg")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def audio(sec, fmt="mp3"):
    f = DATA / f"a_{sec}.{fmt}"
    if not f.exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:sample_rate=24000", "-t", str(sec),
                        "-ac", "1", str(f)], check=True)
    return f.read_bytes()


def secs_for(text):
    return round(0.5 + 0.1 * len(text), 1)


class Resp:
    def __init__(self, status=200, body=None, content=b"", lines=None):
        self.status_code = status
        self._body = body
        self.content = content
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        self._lines = lines or []

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=False):
        yield from self._lines


CALLS = []
MODE = {"doubao_err": None, "minimax_status": 0, "fail_once": 0}


def fake_post(url, headers=None, json=None, timeout=None, stream=False):
    CALLS.append((url, dict(headers or {}), json))
    if MODE["fail_once"] > 0:
        MODE["fail_once"] -= 1
        raise tts_cloud.requests.ConnectionError("boom")
    if "openspeech.bytedance.com" in url:
        if MODE["doubao_err"]:
            return Resp(lines=['data: {"code": 45000000, "message": "' + MODE["doubao_err"] + '"}'])
        if headers.get("X-Api-Key") == "bad":
            return Resp(401, {"error": "invalid api key"})
        b = audio(secs_for(json["req_params"]["text"]))
        half = len(b) // 2
        return Resp(lines=["event: 352", "data: " + '{"code": 0, "data": "%s"}' % base64.b64encode(b[:half]).decode(),
                           "data: " + '{"code": 0, "data": "%s"}' % base64.b64encode(b[half:]).decode(),
                           'data: {"code": 20000000, "message": "OK"}'])
    if url.endswith("/t2a_v2"):
        if MODE["minimax_status"]:
            return Resp(body={"base_resp": {"status_code": MODE["minimax_status"], "status_msg": "insufficient balance"}})
        return Resp(body={"data": {"audio": audio(secs_for(json["text"])).hex()}, "base_resp": {"status_code": 0}})
    if url.endswith("/get_voice"):
        return Resp(body={"system_voice": [
            {"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "沉稳高管", "description": ["一位沉稳可靠的男性高管"]},
            {"voice_id": "English_Graceful_Lady", "voice_name": "Graceful Lady", "description": ["A graceful lady"]}],
            "voice_cloning": [{"voice_id": "my_clone_01", "description": []}], "base_resp": {"status_code": 0}})
    if "multimodal-generation" in url:
        return Resp(body={"output": {"audio": {"url": "https://oss.example/x.wav?t=" + json["input"]["text"][:4]}},
                          "request_id": "r"})
    raise AssertionError("unexpected url " + url)


def fake_get(url, timeout=None, **k):
    CALLS.append((url, {}, None))
    return Resp(content=audio(1.2, "wav"))


tts_cloud.requests.post = fake_post
tts_cloud.requests.get = fake_get

print("\n== 1. 没配 Key：不列音色，合成时说清楚 ==")
check("没配 Key 不列商用音色", tts_cloud.voices() == [])
try:
    tts.synth("你好。", "doubao:zh_female_vv_uranus_bigtts", DATA / "x.mp3")
    check("没配 Key 报错", False)
except tts.TTSError as e:
    check("没配 Key 报错（不会悄悄换成 Windows 语音）", "API Key" in str(e) and "豆包" in str(e), str(e))

print("\n== 2. 设置：保存、掩码、借用 AI 模型的 Key ==")
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
r = c.post("/api/settings", headers=H, json={"tts_services": {"doubao": {"api_key": "volc-key-1234567890", "voices": "S_abc123, S_def"}}})
st = {x["id"]: x for x in r.json()["tts_services"]}
check("保存豆包 Key，只回掩码", st["doubao"]["configured"] and st["doubao"]["key_masked"] == "volc-k…7890"
      and "volc-key-1234567890" not in r.text, st["doubao"])
c.post("/api/settings", headers=H, json={"llm_providers": {"minimax": {"api_key": "mm-key-abcdefghijk"},
                                                           "qwen": {"api_key": "sk-dash-abcdefghij"}}})
st = {x["id"]: x for x in c.get("/api/settings").json()["tts_services"]}
check("MiniMax / 通义没单独填：用「AI 模型」里同一家的 Key", st["minimax"]["key_from"] == "llm" and st["qwen"]["key_from"] == "llm", st)
c.post("/api/settings", headers=H, json={"tts_services": {"doubao": {"api_key": ""}}})
check("Key 留空保存：保持原来的", tts_cloud.api_key("doubao") == "volc-key-1234567890")

print("\n== 3. 音色列表 ==")
vs = c.get("/api/voices").json()["voices"]
names = {v["name"]: v for v in vs}
check("豆包官方音色 + 自己加的音色", "doubao:zh_female_vv_uranus_bigtts" in names and "doubao:S_abc123" in names
      and "doubao:S_def" in names)
check("MiniMax 从接口取音色（含复刻的），语言、性别猜得出", names.get("minimax:English_Graceful_Lady", {}).get("locale") == "en-US"
      and names.get("minimax:Chinese (Mandarin)_Reliable_Executive", {}).get("gender") == "Male"
      and "minimax:my_clone_01" in names, [n for n in names if n.startswith("minimax")])
check("通义音色能说多种语言", "en" in names["qwen:Cherry"]["langs"] and "zh" in names["qwen:Cherry"]["langs"])
check("Edge 免费音色照样在（能联网时）或者至少不报错", r.status_code == 200)
check("音色名字（两人问答用）", dialogue.voice_name("doubao:zh_male_m191_uranus_bigtts") == "云舟"
      and dialogue.voice_name("qwen:Ethan") == "Ethan")

print("\n== 4. 豆包：请求格式、流式返回、切段、语速 ==")
CALLS.clear()
text = "第一句话讲的是背景。" * 20 + "最后一句。"
dur, bounds = tts.synth(text, "doubao:zh_female_vv_uranus_bigtts", DATA / "d.mp3", rate="+20%")
url, hd, body = CALLS[0]
check("地址、鉴权头、资源 ID", url.endswith("/api/v3/tts/unidirectional/sse") and hd["X-Api-Key"] == "volc-key-1234567890"
      and hd["X-Api-Resource-Id"] == "seed-tts-2.0" and hd.get("X-Api-Request-Id"), hd)
check("音色、格式、语速（+20% → 20）", body["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"
      and body["req_params"]["audio_params"]["format"] == "mp3" and body["req_params"]["audio_params"]["speech_rate"] == 20,
      body["req_params"]["audio_params"])
check("长文字按句子切成几段（每段 ≤150 字）", len(CALLS) >= 2 and all(len(b["req_params"]["text"]) <= 150 for _, _, b in CALLS),
      [len(b["req_params"]["text"]) for _, _, b in CALLS])
check("拼成一段 mp3，时长 ≈ 各段之和", abs(dur - (sum(secs_for(b["req_params"]["text"]) for _, _, b in CALLS)
                                                + tts_cloud.GAP * (len(CALLS) - 1))) < 0.3, dur)
check("每段的时间当作词边界（字幕能对上）", len(bounds) == len(CALLS) and bounds[1]["t"] > bounds[0]["d"]
      and "".join(b["text"] for b in bounds).replace(" ", "") == text, [(b["t"], b["d"]) for b in bounds])
CALLS.clear()
tts.synth("复刻的声音。", "doubao:S_abc123", DATA / "d2.mp3")
check("自己复刻的音色用 seed-icl-2.0", CALLS[0][1]["X-Api-Resource-Id"] == "seed-icl-2.0")
MODE["doubao_err"] = "quota exceeded"
try:
    tts.synth("你好。", "doubao:zh_female_vv_uranus_bigtts", DATA / "d3.mp3")
    check("接口报错时说清楚", False)
except tts.TTSError as e:
    check("接口报错时说清楚", "45000000" in str(e) and "quota exceeded" in str(e), str(e))
MODE["doubao_err"] = None
MODE["fail_once"] = 1
tts.synth("网络抖了一下。", "doubao:zh_female_vv_uranus_bigtts", DATA / "d4.mp3")
check("网络抖动自动重试", (DATA / "d4.mp3").exists())

print("\n== 5. MiniMax：hex 音频、语速、余额不足 ==")
CALLS.clear()
dur, bounds = tts.synth("MiniMax 的声音很自然。", "minimax:English_Graceful_Lady", DATA / "m.mp3", rate="-15%")
url, hd, body = CALLS[0]
check("地址、鉴权、模型、音色、语速", url == "https://api.minimax.cn/v1/t2a_v2" and hd["Authorization"] == "Bearer mm-key-abcdefghijk"
      and body["model"] == "speech-2.8-hd" and body["voice_setting"]["voice_id"] == "English_Graceful_Lady"
      and body["voice_setting"]["speed"] == 0.85 and body["output_format"] == "hex", body["voice_setting"])
check("拿到音频", dur > 1.0 and (DATA / "m.mp3").stat().st_size > 1000, dur)
MODE["minimax_status"] = 1008
try:
    tts.synth("你好。", "minimax:English_Graceful_Lady", DATA / "m2.mp3")
    check("余额不足说清楚", False)
except tts.TTSError as e:
    check("余额不足说清楚", "1008" in str(e) and "insufficient balance" in str(e), str(e))
MODE["minimax_status"] = 0

print("\n== 6. 通义：下载音频、语速用 ffmpeg 调 ==")
CALLS.clear()
dur, bounds = tts.synth("通义的声音。第二句。", "qwen:Cherry", DATA / "q.mp3", rate="+20%")
post = [x for x in CALLS if x[2] is not None][0]
check("地址、模型、音色", "multimodal-generation/generation" in post[0] and post[2]["model"] == "qwen3-tts-flash"
      and post[2]["input"]["voice"] == "Cherry" and post[1]["Authorization"] == "Bearer sk-dash-abcdefghij", post[2])
check("按返回的地址下载音频", any(x[2] is None and x[0].startswith("https://oss.example/") for x in CALLS))
check("语速 +20%：时长变短", dur < 1.2 * 0.9, dur)
check("时间也按语速换算", abs(bounds[-1]["t"] + bounds[-1]["d"] - dur) < 0.25, (bounds, dur))

print("\n== 7. 和配音流程接上：项目音色、两人问答各用各的服务 ==")
proj = storage.create("商用配音", "zh-CN")
proj.source = "slides"
proj.settings = {"dialogue": True}
proj.speakers = dialogue.default_speakers("zh-CN", "doubao:zh_female_vv_uranus_bigtts", "qwen:Ethan")
proj.voice = proj.speakers[0].voice
st = Step(kind="slide", lines=[DialogueLine(who="host", text="这一页讲什么？"), DialogueLine(who="expert", text="讲三件事。")])
st.narration = st.caption = join_lines(st.lines)
proj.steps = [st]
storage.save(proj)
CALLS.clear()
pj = storage.load(proj.id)
res = tts.synth_project(pj)
check("两人问答：主持人用豆包、讲师用通义", res["generated"] == 1 and not res["errors"]
      and any("openspeech" in u for u, _, _ in CALLS) and any("multimodal" in u for u, _, _ in CALLS), res)
check("讲者名字取音色名", proj.speakers[0].name == "Vivi" and proj.speakers[1].name == "Ethan")
lt = pj.steps[0].line_times
check("逐句时间照样有", len(lt) == 2 and lt[1][0] > lt[0][1], lt)
r = c.post("/api/tts/preview", headers=H, json={"voice": "qwen:Cherry"})
check("试听商用音色", r.status_code == 200 and r.headers["content-type"].startswith("audio"), r.status_code)
dialogue.retarget(pj, "en-US")
check("换成英文：能说英文的通义音色保留，豆包中文音色换成英文默认音色",
      pj.speakers[1].voice == "qwen:Ethan" and pj.speakers[0].voice == "en-US-AriaNeural", [x.voice for x in pj.speakers])

print("\n== 8. 测试按钮 ==")
r = c.post("/api/settings/test-tts", headers=H, json={"service": "minimax"}).json()
check("测试成功", r["ok"] and "秒" in r["message"], r)
r = c.post("/api/settings/test-tts", headers=H, json={"service": "doubao", "api_key": "bad"}).json()
check("测试失败说清楚（用这次填的 Key，不保存）", not r["ok"] and "401" in r["message"] and tts_cloud.api_key("doubao") == "volc-key-1234567890", r)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
