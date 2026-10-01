"""接口级回归测试：每条后台任务链路走一遍 HTTP，确认「快照 + 合并」改造没有破坏正常流程。"""
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "regress_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import script_gen, tts, video  # noqa: E402
import selftest  # noqa: E402
from backend import config as _cfg  # noqa: E402
# 不读你真实的 config.json（里面的默认音色可能不是中文，念不了测试里的中文解说）
_cfg.CONFIG_PATH = DATA / 'config.json'
_cfg.CONFIG_PATH.write_text('{"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural"}', encoding='utf-8')
_cfg._cache = None

ORIGIN = {"Origin": "http://127.0.0.1:8756"}
c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers=ORIGIN)
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def wait(j, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.25)
    raise TimeoutError


class FakeLLM:
    """根据提示词里的内容造回复：解说按 i 生成，翻译按 k 生成。"""

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        prompt = messages[-1]["content"]
        if '"k":' in prompt:
            items = re.findall(r'"k": "([^"]+)",\s*"t": "([^"]*)"', prompt)
            return {"items": [{"k": k_, "t": "EN " + t[:20]} for k_, t in items]}
        idx = [int(x) for x in re.findall(r'"i": (\d+)', prompt)]
        return {"title": "AI 标题", "subtitle": "AI 副标题", "intro": "AI 片头旁白。",
                "outro": "AI 片尾。", "summary": "摘要",
                "steps": [{"i": i, "title": f"步骤{i}", "narration": f"AI 解说第 {i} 步。",
                           "caption": f"AI 解说第 {i} 步。"} for i in idx]}

    def chat(self, messages, **k):
        return "改写结果。"


script_gen.get_client = FakeLLM

print("\n== 生成解说 /script ==")
pid = selftest.build_project("regress")
p0 = storage.load(pid)
r = wait(c.post(f"/api/projects/{pid}/script", json={"overwrite": True}).json())
p = storage.load(pid)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("每步解说被写入", all(s.narration.startswith("AI 解说") for s in p.steps),
      [s.narration for s in p.steps])
check("项目标题 / 片头被写入", p.title == "AI 标题" and p.intro == "AI 片头旁白。")

print("\n== 合成语音 /tts ==")
r = wait(c.post(f"/api/projects/{pid}/tts", json={}).json())
p = storage.load(pid)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("每步都有配音", all(s.audio and s.audio_duration > 0 and s.voice_source == "tts" for s in p.steps))
check("片头配音与文案一致", tts.card_audio(storage.audio_dir(pid), "intro", p.intro) is not None)

print("\n== 渲染 /render ==")
r = wait(c.post(f"/api/projects/{pid}/render", json={}).json())
p = storage.load(pid)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("项目记录了成片文件", bool(p.output) and (storage.output_dir(pid) / p.output).exists(), p.output)
fr = c.get(f"/api/projects/{pid}/file/output/{p.output}")
check("成片可下载且不强缓存", fr.status_code == 200 and fr.headers["cache-control"] == "no-cache"
      and len(fr.content) > 100000, f"{fr.status_code} {fr.headers.get('cache-control')} {len(fr.content)}B")
check("渲染临时目录已清理", not any((storage.work_dir(pid)).glob("render_*")))

print("\n== 合成语音期间改一步解说（并发） ==")
pid2 = selftest.build_project("regress-concurrent")
real_synth = tts.synth


def slow_synth(*a, **k):
    time.sleep(0.6)
    return real_synth(*a, **k)
tts.synth = slow_synth
try:
    j = c.post(f"/api/projects/{pid2}/tts", json={}).json()
    time.sleep(1.0)
    sid = storage.load(pid2).steps[2].id
    c.patch(f"/api/projects/{pid2}/steps/{sid}", json={"narration": "我在合成期间改的", "caption": "我在合成期间改的"})
    r = wait(j)
finally:
    tts.synth = real_synth
p = storage.load(pid2)
s2 = p.steps[2]
check("任务成功", r["status"] == "done", r.get("error", ""))
check("我改的解说保留", s2.narration == "我在合成期间改的")
check("没有把旧文字的配音塞给新文字", not s2.audio, f"audio={s2.audio!r}")
check("其他步骤正常拿到配音", all(s.audio for s in p.steps[:2]))

print("\n== 翻译 /translate ==")
r = wait(c.post(f"/api/projects/{pid}/translate", json={"target_language": "en-US", "apply": True}).json())
p = storage.load(pid)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("语言和音色切换", p.language == "en-US" and p.voice.startswith("en-US"), f"{p.language} {p.voice}")
check("解说变成译文且旧配音作废", all(s.narration.startswith("EN ") and not s.audio for s in p.steps),
      [(s.narration[:12], s.audio) for s in p.steps])
check("片头旧配音不再被使用", tts.card_audio(storage.audio_dir(pid), "intro", p.intro) is None)

print("\n== 任务互斥 ==")
gate = threading.Event()
real_render = video.render_project
video.render_project = lambda proj, **k: (gate.wait(10), {"file": ""})[1]
try:
    a = c.post(f"/api/projects/{pid}/render", json={}).json()
    b = c.post(f"/api/projects/{pid}/render", json={}).json()
    t = c.post(f"/api/projects/{pid}/tts", json={})
    check("连点渲染返回同一个任务", a["id"] == b["id"])
    check("渲染中再点合成语音 -> 409 并提示原因", t.status_code == 409, t.json().get("detail", ""))
    gate.set()
    wait(a)
finally:
    video.render_project = real_render

print("\n== 单步录音上传 /voice ==")
sid0 = storage.load(pid2).steps[0].id
with open(SP / "own_voice.mp3", "rb") as f:
    r = wait(c.post(f"/api/projects/{pid2}/steps/{sid0}/voice", files={"file": ("me.mp3", f)},
                    data={"transcribe": "true", "replace_text": "true", "language": "zh-CN"}).json())
s0 = storage.load(pid2).steps[0]
check("任务成功", r["status"] == "done", r.get("error", ""))
check("原声写入 + 识别文字替换解说", s0.voice_source == "own" and s0.audio.endswith("_own.mp3")
      and "平台管理" in s0.narration, f"{s0.voice_source} {s0.audio} {s0.narration}")
check("没有残留临时文件", not list(storage.audio_dir(pid2).glob("*.part.*")))

print("\n== 边录边讲 /magic-mic ==")
pid3 = storage.create("regress-mic").id
rec_start = time.time() * 1000
for off, label in [(3.2, "平台管理"), (9.0, "组织管理"), (14.5, "机构管理")]:
    c.post("/api/capture/step", json={"project_id": pid3, "kind": "click", "page_title": "后台",
                                       "target": {"text": label, "rect": {"x": 1, "y": 1, "w": 9, "h": 9}},
                                       "client_ts": rec_start + off * 1000})
with open(SP / "session.webm", "rb") as f:
    r = wait(c.post(f"/api/projects/{pid3}/magic-mic", files={"file": ("s.webm", f)},
                    data={"rec_start": str(rec_start), "mode": "ai", "language": "zh-CN"}).json())
p = storage.load(pid3)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("三句话分到三步", [("平台" in p.steps[0].narration), ("组织" in p.steps[1].narration),
                          ("机构" in p.steps[2].narration)] == [True, True, True],
      [s.narration for s in p.steps])
check("AI 配音已生成", all(s.audio and s.voice_source == "tts" for s in p.steps))
check("整段录音临时文件已清理", not list(storage.audio_dir(pid3).glob("_session*")))

print("\n== 一键生成 /auto ==")
pid4 = selftest.build_project("regress-auto")
r = wait(c.post(f"/api/projects/{pid4}/auto", json={"overwrite": True}).json(), timeout=400)
p = storage.load(pid4)
check("任务成功", r["status"] == "done", r.get("error", ""))
check("出片", bool(p.output) and (storage.output_dir(pid4) / p.output).exists())

print("\n== 预览 / 试听 ==")
pr = c.get(f"/api/projects/{pid4}/steps/{p.steps[0].id}/preview?scale=0.3")
check("步骤预览是 JPEG", pr.status_code == 200 and pr.content[:2] == b"\xff\xd8")
cr = c.get(f"/api/projects/{pid4}/card/intro/preview?scale=0.3")
check("片头预览是 JPEG", cr.status_code == 200 and cr.content[:2] == b"\xff\xd8")
before_files = set(storage.audio_dir(pid4).iterdir())
tp = c.post("/api/tts/preview", json={"voice": "zh-CN-YunxiNeural"})
check("试听返回音频", tp.status_code == 200 and tp.headers["content-type"] == "audio/mpeg" and len(tp.content) > 1000)
check("试听不改项目文件", set(storage.audio_dir(pid4).iterdir()) == before_files)
time.sleep(0.3)
check("试听临时文件已删除", not list((DATA / "_tmp").glob("preview_*")) if (DATA / "_tmp").exists() else True)

print("\n== 录制接口（扩展来源）==")
ext = TestClient(main.app, base_url="http://127.0.0.1:8756",
                 headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"})
st = ext.post("/api/capture/start", json={"name": "ext"}).json()
rs = ext.post("/api/capture/step", json={"project_id": st["project_id"], "kind": "click"}).json()
check("扩展能开始录制并上报步骤", rs.get("ok") and rs.get("steps") == 1, rs)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
