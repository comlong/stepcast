"""停止按钮：一键生成三个阶段分别中途停止。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "cancel_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, main, storage  # noqa: E402
from backend.services import llm_openai, tts  # noqa: E402
import selftest  # noqa: E402
from backend import config as _cfg  # noqa: E402
# 不读你真实的 config.json（里面的默认音色可能不是中文，念不了测试里的中文解说）
_cfg.CONFIG_PATH = DATA / 'config.json'
_cfg.CONFIG_PATH.write_text('{"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural"}', encoding='utf-8')
_cfg._cache = None

c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers={"Origin": "http://127.0.0.1:8756"})
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def job(jid):
    return c.get(f"/api/jobs/{jid}").json()


def wait_until(pred, timeout=120, every=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError


def wait_done(jid, timeout=300):
    return wait_until(lambda: (lambda j: j if j["status"] not in ("pending", "running") else None)(job(jid)),
                      timeout, 0.2)


def stop_and_time(jid):
    t = time.time()
    r = c.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    j = wait_done(jid, timeout=60)
    return j, time.time() - t


def ffmpeg_count():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe"], capture_output=True, text=True).stdout
    return out.lower().count("ffmpeg.exe")


# ---------------------------------------------------------------- 1. 生成解说阶段
print("\n== 1. 停在「生成解说」（DeepSeek 请求进行中）==")
if not config.get("deepseek_api_key"):
    os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-used"
    config._cache = None
calls = []


def hanging_post(*a, **k):
    calls.append(time.time())
    time.sleep(30)          # 模拟一个很慢的生成请求
    raise RuntimeError("不应该等到这里")
llm_openai.requests.post = hanging_post

pid = storage.create("停止测试-解说").id
p = storage.load(pid)
base = selftest.build_project("tmp")
for s in storage.load(base).steps:
    s.narration = ""
    s.caption = ""
    p.steps.append(s)
p.title = ""
storage.save(p)
shutil.copytree(storage.screenshots_dir(base), storage.screenshots_dir(pid), dirs_exist_ok=True)

j = c.post(f"/api/projects/{pid}/auto", json={}).json()
wait_until(lambda: calls, timeout=20)
time.sleep(1.0)
jj, secs = stop_and_time(j["id"])
p = storage.load(pid)
check("状态变成已停止", jj["status"] == "cancelled", jj["status"] + " " + jj.get("error", ""))
check("不用等 DeepSeek 返回，1.5 秒内停下", secs < 1.5, f"{secs:.2f} 秒")
check("解说没被写入半截内容", all(not s.narration for s in p.steps))
check("没有出片", not p.output)

# ---------------------------------------------------------------- 2. 配音阶段
print("\n== 2. 停在「合成语音」==")


def must_not_call(*a, **k):
    calls.append("unexpected")
    raise AssertionError("解说都已存在，不该再调用 DeepSeek")
llm_openai.requests.post = must_not_call
calls.clear()

pid = selftest.build_project("停止测试-配音")          # 自带标题和每步解说
real_synth = tts.synth


def slow_synth(*a, **k):
    time.sleep(0.8)
    return real_synth(*a, **k)
tts.synth = slow_synth
j = c.post(f"/api/projects/{pid}/auto", json={}).json()
wait_until(lambda: sum(1 for s in storage.load(pid).steps if s.audio) >= 0
           and "合成语音 3/" in job(j["id"])["message"], timeout=90)
jj, secs = stop_and_time(j["id"])
tts.synth = real_synth
p = storage.load(pid)
voiced = [bool(s.audio) for s in p.steps]
check("状态变成已停止", jj["status"] == "cancelled", jj["status"] + " " + jj.get("error", ""))
check("停止在 2 秒内生效", secs < 2.0, f"{secs:.2f} 秒")
check("解说已存在时跳过了 DeepSeek", not calls, calls)
check("已经配好音的步骤保留了配音", voiced[0] is True, f"每步是否有配音：{voiced}")
check("还没配到的步骤保持空", voiced[-1] is False, f"每步是否有配音：{voiced}")
check("没有出片", not p.output)

print("\n== 3. 改一步解说，再点一键生成：接着做 ==")
sid = p.steps[0].id
c.patch(f"/api/projects/{pid}/steps/{sid}", json={"narration": "停下后我改的第一步解说。",
                                                  "caption": "停下后我改的第一步解说。"})
kept_before = p.steps[1].audio
j = c.post(f"/api/projects/{pid}/auto", json={}).json()
jj = wait_done(j["id"], timeout=400)
p = storage.load(pid)
check("任务完成", jj["status"] == "done", jj.get("error", ""))
check("出片", bool(p.output) and (storage.output_dir(pid) / p.output).exists())
check("改过的那步用新文字重新配音", p.steps[0].audio and p.steps[0].narration.startswith("停下后"))
check("之前配好的步骤没有重做", not kept_before or p.steps[1].audio == kept_before)
check("仍然没有调用 DeepSeek", not calls, calls)

# ---------------------------------------------------------------- 4. 渲染阶段
print("\n== 4. 停在「渲染视频」==")
old_output = p.output
old_mtime = (storage.output_dir(pid) / old_output).stat().st_mtime
ff_before = ffmpeg_count()
c.patch(f"/api/projects/{pid}/steps/{p.steps[1].id}", json={"title": "改个标题触发重渲"})
j = c.post(f"/api/projects/{pid}/auto", json={}).json()
wait_until(lambda: "渲染中" in job(j["id"])["message"], timeout=90)
time.sleep(1.5)
jj, secs = stop_and_time(j["id"])
time.sleep(1.0)
p = storage.load(pid)
check("状态变成已停止", jj["status"] == "cancelled", jj["status"] + " " + jj.get("error", ""))
check("停止在 2 秒内生效", secs < 2.0, f"{secs:.2f} 秒")
check("没有残留 ffmpeg 进程", ffmpeg_count() <= ff_before, f"之前 {ff_before} 个，之后 {ffmpeg_count()} 个")
check("渲染临时目录已清理", not list(storage.work_dir(pid).glob("render_*")))
check("旧成片完好（没被半截文件覆盖）",
      (storage.output_dir(pid) / old_output).stat().st_mtime == old_mtime)

r = c.post(f"/api/projects/{pid}/render", json={})
check("停止后能立刻重新开始渲染（没有卡在「正在渲染」）", r.status_code == 200, r.text[:80])
jj = wait_done(r.json()["id"], timeout=300)
check("重新渲染成功", jj["status"] == "done", jj.get("error", ""))

# ---------------------------------------------------------------- 5. 边角
print("\n== 5. 边角情况 ==")
check("停止不存在的任务 -> 404", c.post("/api/jobs/j_nope/cancel").status_code == 404)
done = c.post(f"/api/jobs/{jj['id']}/cancel").json()
check("停止已完成的任务不改变结果", done["status"] == "done")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
