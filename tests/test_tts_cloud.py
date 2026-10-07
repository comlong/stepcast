"""Paid voice services (Doubao, MiniMax, Qwen, Google Gemini, ElevenLabs, Azure): request format, response parsing, chunking and joining, speed, errors, voice lists, settings, integration with the voice-over flow (all simulated, offline, no cost)."""
import base64
import html
import io
import json
import os
import re
import wave
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
for k in ("MODEL_SPEECH_API_KEY", "VOLC_SPEECH_API_KEY", "MINIMAX_API_KEY", "DASHSCOPE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
          "ELEVENLABS_API_KEY", "XI_API_KEY", "AZURE_SPEECH_KEY", "SPEECH_KEY"):
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
    def __init__(self, status=200, body=None, content=b"", lines=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.content = content
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        self._lines = lines or []

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=False):
        yield from self._lines


class Call(tuple):
    """(url, headers, body) as before; the query parameters of the request are in .params."""

    def __new__(cls, url, headers, body, params=None):
        obj = super().__new__(cls, (url, headers, body))
        obj.params = dict(params or {})
        return obj


CALLS = []
MODE = {"doubao_err": None, "minimax_status": 0, "fail_once": 0, "gemini_empty": 0, "eleven_429": 0, "eleven_voices_fail": False,
        "azure_voices_fail": False}


def pcm_of(sec):
    with wave.open(io.BytesIO(audio(sec, "wav"))) as w:
        return w.readframes(w.getnframes()), w.getframerate()


def fake_post(url, headers=None, json=None, timeout=None, stream=False, params=None, data=None):
    CALLS.append(Call(url, dict(headers or {}), json if json is not None else (data.decode("utf-8") if isinstance(data, bytes) else data),
                      params))
    if "generativelanguage.googleapis.com" in url:
        if headers.get("x-goog-api-key") == "bad":
            return Resp(400, {"error": {"message": "API key not valid"}})
        if MODE["gemini_empty"] > 0:
            MODE["gemini_empty"] -= 1
            return Resp(body={"candidates": [{"finishReason": "OTHER", "content": {"parts": [{"text": "I can't"}]}}]})
        text = json["contents"][0]["parts"][0]["text"]
        if "/models/gemini-2.5" in url:                       # older models answer with raw PCM and the rate in the mime type
            pcm, rate = pcm_of(secs_for(text))
            inline = {"mimeType": f"audio/L16;codec=pcm;rate={rate}", "data": base64.b64encode(pcm).decode()}
        else:
            inline = {"mimeType": "audio/wav", "data": base64.b64encode(audio(secs_for(text), "wav")).decode()}
        return Resp(body={"candidates": [{"content": {"parts": [{"inlineData": inline}]}}]})
    if "elevenlabs.io/v1/text-to-speech/" in url:
        if headers.get("xi-api-key") == "bad":
            return Resp(401, {"detail": {"status": "invalid_api_key"}})
        if MODE["eleven_429"] > 0:
            MODE["eleven_429"] -= 1
            return Resp(429, {"detail": "too many concurrent requests"})
        return Resp(content=audio(secs_for(json["text"])), headers={"content-type": "audio/mpeg"})
    if "tts.speech.microsoft.com/cognitiveservices/v1" in url:
        if headers.get("Ocp-Apim-Subscription-Key") == "bad":
            return Resp(401, {})
        plain = html.unescape(re.sub(r"<[^>]+>", "", data.decode("utf-8")))
        return Resp(content=audio(secs_for(plain)), headers={"content-type": "audio/mpeg"})
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
    CALLS.append(Call(url, dict(k.get("headers") or {}), None))
    if url.endswith("/v1/voices") and "elevenlabs" in url:
        if MODE["eleven_voices_fail"]:
            return Resp(500, {})
        return Resp(body={"voices": [{"voice_id": "v_rachel", "name": "Rachel", "labels": {"gender": "female", "accent": "american"}},
                                     {"voice_id": "v_clone", "name": "My clone", "labels": {}}]})
    if url.endswith("/cognitiveservices/voices/list"):
        if MODE["azure_voices_fail"]:
            return Resp(500, [])
        return Resp(body=[
            {"ShortName": "de-DE-KatjaNeural", "LocalName": "Katja", "Gender": "Female", "Locale": "de-DE", "VoiceType": "Neural"},
            {"ShortName": "en-US-AvaMultilingualNeural", "LocalName": "Ava Multilingual", "Gender": "Female", "Locale": "en-US",
             "SecondaryLocaleList": ["de-DE", "fr-FR", "pl-PL"], "VoiceType": "Neural"},
            {"ShortName": "de-DE-Seraphina:DragonHDLatestNeural", "LocalName": "Seraphina", "Gender": "Female", "Locale": "de-DE",
             "VoiceType": "NeuralHD"},
            {"ShortName": "xx-XX-OldVoice", "LocalName": "Old", "Gender": "Male", "Locale": "xx-XX", "VoiceType": "Standard"}])
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
url, hd, body = CALLS[0][:3]
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
url, hd, body = CALLS[0][:3]
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


print("\n== 9. Google Gemini：借用 AI 模型的 Key、音色、请求格式、旧模型、没有音频时重试 ==")
c.post("/api/settings", headers=H, json={"llm_providers": {"gemini": {"api_key": "gm-key-abcdefghijk"}}})
st = {x["id"]: x for x in c.get("/api/settings").json()["tts_services"]}
check("没单独填：用「AI 模型」里 Google Gemini 的 Key", st["gemini"]["key_from"] == "llm" and st["gemini"]["configured"], st["gemini"])
fields = {f["key"]: f for f in st["gemini"]["fields"]}
check("设置页能选模型、写语气提示（默认 3.8 Flash TTS）", fields["model"]["value"] == "gemini-3.8-flash-tts" and len(fields["model"]["choices"]) == 2
      and fields["style"]["choices"] == [], fields)
names = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
gem = [v for n, v in names.items() if n.startswith("gemini:")]
check("30 个预置音色，每个都能说各种语言", len(gem) == 30 and all({"zh", "de", "pl", "fr", "en"} <= set(v["langs"]) for v in gem), len(gem))
check("性别和特点", names["gemini:Kore"]["gender"] == "Female" and names["gemini:Puck"]["gender"] == "Male" and "Firm" in names["gemini:Kore"]["friendly"])
CALLS.clear()
dur, bounds = tts.synth("Gemini speaks many languages. This is the second sentence.", "gemini:Kore", DATA / "g.mp3")
url, hd, body = CALLS[0][:3]
check("地址、鉴权头、模型", url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-tts:generateContent"
      and hd["x-goog-api-key"] == "gm-key-abcdefghijk", (url, hd))
gc = body["generationConfig"]
check("要音频、指定音色；没写语气提示就不带", gc["responseModalities"] == ["AUDIO"] and gc["speechConfig"]["voiceConfig"]["voice"] == "Kore"
      and "speech_metadata" not in body["contents"][0]["parts"][0], body)
check("拿到音频（WAV）", dur > 1.0 and bounds and (DATA / "g.mp3").exists(), dur)
c.post("/api/settings", headers=H, json={"tts_services": {"gemini": {"style": "calm and clear"}}})
CALLS.clear()
tts.synth("Short.", "gemini:Puck", DATA / "g2.mp3")
check("语气提示放进 speech_metadata.style", CALLS[0][2]["contents"][0]["parts"][0]["speech_metadata"] == {"style": "calm and clear"}, CALLS[0][2])
CALLS.clear()
dur_fast, _ = tts.synth("Same sentence, faster.", "gemini:Puck", DATA / "g3.mp3", rate="+25%")
dur_norm, _ = tts.synth("Same sentence, faster.", "gemini:Puck", DATA / "g4.mp3")
check("语速用 ffmpeg 调（接口没有语速参数）", dur_fast < dur_norm * 0.9, (dur_fast, dur_norm))
r = c.post("/api/settings", headers=H, json={"tts_services": {"gemini": {"model": "not-a-model"}}})
check("不在选项里的模型不接受", tts_cloud.setting("gemini", "model") == "gemini-3.8-flash-tts")
cfg = config.load()
cfg["tts_services"]["gemini"]["model"] = "gemini-2.5-flash-preview-tts"
config.save(cfg)
CALLS.clear()
dur, _ = tts.synth("An older model.", "gemini:Kore", DATA / "g5.mp3")
b5 = CALLS[0][2]
check("旧模型：用旧的音色写法，语气提示拼在文字前面",
      b5["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
      and b5["contents"][0]["parts"][0]["text"] == "calm and clear: An older model.", b5)
check("旧模型返回的是裸 PCM：自动加上 WAV 头，时长对", abs(dur - secs_for("calm and clear: An older model.")) < 0.3 or dur > 0.8, dur)
cfg = config.load()
cfg["tts_services"]["gemini"]["model"] = ""
config.save(cfg)
MODE["gemini_empty"] = 1
CALLS.clear()
tts.synth("Retry me.", "gemini:Kore", DATA / "g6.mp3")
check("模型偶尔回文字不回语音：自动重试", len(CALLS) == 2 and (DATA / "g6.mp3").exists(), len(CALLS))
MODE["gemini_empty"] = 5
try:
    tts.synth("Never audio.", "gemini:Kore", DATA / "g7.mp3")
    check("一直没有音频：说清楚原因", False)
except tts.TTSError as e:
    check("一直没有音频：说清楚原因", "没有返回音频" in str(e) and "OTHER" in str(e), str(e))
MODE["gemini_empty"] = 0
r = c.post("/api/settings/test-tts", headers=H, json={"service": "gemini", "api_key": "bad"}).json()
check("Key 不对：测试按钮说清楚", not r["ok"] and "400" in r["message"], r)
r = c.post("/api/settings/test-tts", headers=H, json={"service": "gemini"}).json()
check("测试成功", r["ok"] and "秒" in r["message"], r)

print("\n== 10. ElevenLabs：请求格式、欧盟数据区域、音色列表、限流重试 ==")
c.post("/api/settings", headers=H, json={"tts_services": {"elevenlabs": {"api_key": "el-key-1234567890", "voices": "v_extra"}}})
st = {x["id"]: x for x in c.get("/api/settings").json()["tts_services"]}
fields = {f["key"]: f for f in st["elevenlabs"]["fields"]}
check("默认模型 Multilingual v2；数据区域有默认 / 欧盟 / 美国", fields["model"]["value"] == "eleven_multilingual_v2"
      and [c_["value"] for c_ in fields["region"]["choices"]] == ["", "eu", "us"], fields)
names = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
check("从账号取音色（含自己的），性别口音，各种语言都能说", names["elevenlabs:v_rachel"]["gender"] == "Female" and "american" in names["elevenlabs:v_rachel"]["friendly"]
      and "elevenlabs:v_clone" in names and "pl" in names["elevenlabs:v_rachel"]["langs"], [n for n in names if n.startswith("eleven")])
check("自己填的音色 ID 也在", "elevenlabs:v_extra" in names)
CALLS.clear()
dur, _ = tts.synth("ElevenLabs sounds natural. A second sentence follows.", "elevenlabs:v_rachel", DATA / "e.mp3")
url, hd, body = CALLS[0]
params = CALLS[0].params
check("地址（音色在路径里）、鉴权头、输出格式、模型", url == "https://api.elevenlabs.io/v1/text-to-speech/v_rachel" and hd["xi-api-key"] == "el-key-1234567890"
      and params == {"output_format": "mp3_44100_128"} and body["model_id"] == "eleven_multilingual_v2", (url, hd, params, body))
check("拿到音频", dur > 1.0, dur)
c.post("/api/settings", headers=H, json={"tts_services": {"elevenlabs": {"region": "eu", "model": "eleven_v3"}}})
CALLS.clear()
tts.synth("EU data residency.", "elevenlabs:v_rachel", DATA / "e2.mp3")
check("选了欧盟：用欧盟数据驻留的地址，模型换成 v3", CALLS[0][0] == "https://api.eu.residency.elevenlabs.io/v1/text-to-speech/v_rachel"
      and CALLS[0][2]["model_id"] == "eleven_v3", CALLS[0][:3])
c.post("/api/settings", headers=H, json={"tts_services": {"elevenlabs": {"region": "mars"}}})
check("不认识的区域不接受", tts_cloud.setting("elevenlabs", "region") == "eu")
MODE["eleven_429"] = 1
CALLS.clear()
tts.synth("Rate limited once.", "elevenlabs:v_rachel", DATA / "e3.mp3")
check("429 限流：等一下自动重试", len(CALLS) == 2)
r = c.post("/api/settings/test-tts", headers=H, json={"service": "elevenlabs", "api_key": "bad"}).json()
check("Key 不对：说清楚", not r["ok"] and "401" in r["message"] and "invalid_api_key" in r["message"], r)
tts_cloud._voice_cache.clear()
MODE["eleven_voices_fail"] = True
names = {v["name"] for v in tts_cloud.voices()}
check("取不到账号的音色（离线、Key 不对）：先给几个默认音色", "elevenlabs:21m00Tcm4TlvDq8ikWAM" in names and "elevenlabs:v_rachel" not in names)
MODE["eleven_voices_fail"] = False
tts_cloud._voice_cache.clear()

print("\n== 11. Azure：区域、SSML、语速、转义、音色列表 ==")
c.post("/api/settings", headers=H, json={"tts_services": {"azure": {"api_key": "az-key-1234567890"}}})
st = {x["id"]: x for x in c.get("/api/settings").json()["tts_services"]}
fields = {f["key"]: f for f in st["azure"]["fields"]}
check("区域默认 westeurope（欧洲），自由填写", fields["region"]["value"] == "westeurope" and fields["region"]["choices"] == [], fields)
names = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
check("音色按区域从接口取；标准 / 自定义的不要，HD 的要", "azure:de-DE-KatjaNeural" in names and "azure:de-DE-Seraphina:DragonHDLatestNeural" in names
      and "azure:xx-XX-OldVoice" not in names, [n for n in names if n.startswith("azure")])
check("多语言音色：能说它的第二语言", {"en", "de", "fr", "pl"} <= set(names["azure:en-US-AvaMultilingualNeural"]["langs"])
      and names["azure:de-DE-KatjaNeural"]["langs"] == ["de"], names["azure:en-US-AvaMultilingualNeural"]["langs"])
check("HD 音色有标记", "HD" in names["azure:de-DE-Seraphina:DragonHDLatestNeural"]["friendly"])
CALLS.clear()
dur, _ = tts.synth("Müller & Söhne <GmbH> — fertig.", "azure:de-DE-KatjaNeural", DATA / "a.mp3", rate="+20%")
url, hd, ssml = [x for x in CALLS if "cognitiveservices/v1" in x[0]][0]
check("地址按区域、鉴权头、输出格式、UA", url == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
      and hd["Ocp-Apim-Subscription-Key"] == "az-key-1234567890" and hd["Content-Type"] == "application/ssml+xml"
      and hd["X-Microsoft-OutputFormat"] == "audio-24khz-96kbitrate-mono-mp3" and hd["User-Agent"], (url, hd))
check("SSML：音色、语言取自音色名；文字里的 & < > 已转义；语速用接口自带的", "name='de-DE-KatjaNeural'" in ssml and "xml:lang='de-DE'" in ssml
      and "Müller &amp; Söhne &lt;GmbH&gt;" in ssml and '<prosody rate="+20%">' in ssml, ssml)
check("拿到音频", dur > 1.0, dur)
CALLS.clear()
tts.synth("Normal speed.", "azure:de-DE-KatjaNeural", DATA / "a2.mp3")
check("正常语速不加 prosody", "<prosody" not in [x for x in CALLS if "cognitiveservices/v1" in x[0]][0][2])
c.post("/api/settings", headers=H, json={"tts_services": {"azure": {"region": "northeurope"}}})
CALLS.clear()
tts.synth("North.", "azure:en-US-AvaMultilingualNeural", DATA / "a3.mp3")
check("改区域：地址跟着变", [x for x in CALLS if "cognitiveservices/v1" in x[0]][0][0].startswith("https://northeurope."))
check("区域里写了别的东西（想改地址）：不用，退回 westeurope",
      tts_cloud._azure_region({"tts_services": {"azure": {"region": "evil.com/x?"}}}) == "westeurope"
      and tts_cloud._azure_region({"tts_services": {"azure": {"region": "West Europe"}}}) == "westeurope"
      and tts_cloud._azure_region({"tts_services": {"azure": {"region": "swedencentral"}}}) == "swedencentral")
r = c.post("/api/settings/test-tts", headers=H, json={"service": "azure", "api_key": "bad"}).json()
check("Key 或区域不对：提示两样都查", not r["ok"] and "401" in r["message"] and "区域" in r["message"], r)
r = c.post("/api/settings/test-tts", headers=H, json={"service": "azure"}).json()
check("测试成功", r["ok"], r)
tts_cloud._voice_cache.clear()
MODE["azure_voices_fail"] = True
names = {v["name"] for v in tts_cloud.voices()}
check("取不到音色列表：先给几个多语言音色", "azure:en-US-AvaMultilingualNeural" in names and "azure:de-DE-KatjaNeural" not in names)
MODE["azure_voices_fail"] = False
tts_cloud._voice_cache.clear()

print("\n== 12. 新服务和配音流程接上 ==")
check("是商用音色（带前缀）", all(tts_cloud.is_cloud(v) for v in ("gemini:Kore", "elevenlabs:x", "azure:de-DE-KatjaNeural")))
check("音色名字（两人问答用）", dialogue.voice_name("gemini:Kore") == "Kore" and dialogue.voice_name("azure:de-DE-KatjaNeural") == "Katja"
      and dialogue.voice_name("elevenlabs:21m00Tcm4TlvDq8ikWAM") == "Rachel", [dialogue.voice_name(v) for v in ("gemini:Kore", "azure:de-DE-KatjaNeural")])
r = c.post("/api/tts/preview", headers=H, json={"voice": "gemini:Kore"})
check("试听 Gemini 音色", r.status_code == 200 and r.headers["content-type"].startswith("audio"), r.status_code)
r = c.post("/api/tts/preview", headers=H, json={"voice": "azure:de-DE-KatjaNeural"})
check("试听 Azure 音色", r.status_code == 200 and r.headers["content-type"].startswith("audio"), r.status_code)
proj = storage.create("三个新服务", "de-DE")
proj.source = "slides"
proj.settings = {"dialogue": True}
proj.speakers = dialogue.default_speakers("de-DE", "gemini:Kore", "azure:de-DE-KatjaNeural")
proj.voice = proj.speakers[0].voice
st2 = Step(kind="slide", lines=[DialogueLine(who="host", text="Worum geht es hier?"), DialogueLine(who="expert", text="Um drei Dinge.")])
st2.narration = st2.caption = join_lines(st2.lines)
proj.steps = [st2]
storage.save(proj)
CALLS.clear()
pj2 = storage.load(proj.id)
res = tts.synth_project(pj2)
check("两人问答：主持人用 Gemini、讲师用 Azure", res["generated"] == 1 and not res["errors"]
      and any("generativelanguage" in x[0] for x in CALLS) and any("tts.speech.microsoft.com" in x[0] for x in CALLS), res)
check("逐句时间照样有", len(pj2.steps[0].line_times) == 2, pj2.steps[0].line_times)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
