"""Voice-over for a whole project is made a few paragraphs at a time: how many at once, that every result lands on its own step, what a failure or a
Stop does, and that Q&A projects (whose steps already voice their lines in parallel) stay one step at a time. Fake synthesis, no network."""
import os
import shutil
import sys
import threading
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "tts_parallel_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from backend import storage  # noqa: E402
from backend.models import Project, Step  # noqa: E402
from backend.services import tts  # noqa: E402

fails = []
N, DELAY = 9, 0.3
LOCK = threading.Lock()
LIVE, PEAK, CALLS, THREADS = [0], [0], [], set()
FAIL_ON = set()


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def fake_synth(text, voice, out_path, rate="", volume="", pitch=""):
    with LOCK:
        LIVE[0] += 1
        PEAK[0] = max(PEAK[0], LIVE[0])
        CALLS.append(text)
        THREADS.add(threading.current_thread().name)
    try:
        time.sleep(DELAY)
        if any(f in text for f in FAIL_ON):
            raise tts.TTSError("service said no")
        dur = 1.0 + len(text) / 100
        out_path.write_bytes(f"new {dur:.3f}".encode().ljust(1000, b"x"))     # the file says which duration it belongs to
        return dur, [{"t": 0.0, "d": 1.0, "text": text}]
    finally:
        with LOCK:
            LIVE[0] -= 1


tts.synth = fake_synth


def reset():
    LIVE[0] = PEAK[0] = 0
    CALLS.clear()
    THREADS.clear()
    FAIL_ON.clear()


def project(n=N):
    p = storage.create("并行配音", "zh-CN")
    p.steps = [Step(kind="slide", screenshot="x.png", narration=f"第{i}段解说。", caption=f"第{i}段解说。") for i in range(1, n + 1)]
    storage.save(p)
    return storage.load(p.id)


def run(p, **kw):
    msgs = []
    t0 = time.perf_counter()
    res = tts.synth_project(p, progress=lambda f, m: msgs.append((round(f, 3), m)), **kw)
    return res, msgs, time.perf_counter() - t0


print("== 1. 自动：免费的 Edge 音色一次做 3 段 ==")
reset()
p = project()
res, msgs, wall = run(p)
check("9 段都配好了", res["generated"] == N and not res["errors"] and all(s.audio for s in p.steps), res)
check("同时最多 3 段在做", PEAK[0] == 3, PEAK[0])
check(f"比一段一段快（{N} 段 × {DELAY} 秒，串行要 {N * DELAY:.1f} 秒）", wall < N * DELAY * 0.6, f"{wall:.2f} 秒")
check("合成在后台线程里做，不占调用方的线程", THREADS and all(t.startswith("tts") for t in THREADS), THREADS)
check("每一段的结果落在自己的步骤上（不串位）",
      all(s.boundaries and s.boundaries[0]["text"] == s.narration and abs(s.audio_duration - (1.0 + len(s.narration) / 100)) < 1e-9
          and s.voice_source == "tts" and s.audio == f"{s.id}.mp3" for s in p.steps),
      [(s.narration, s.boundaries[:1]) for s in p.steps[:2]])
check("第一条进度是「合成语音 1/9」，序号一路不减，最后一条是完成", msgs[0][1].startswith("合成语音 1/9") and msgs[-1][0] == 1.0
      and [int(m.split("/")[0].split()[-1]) for _, m in msgs[:-1]] == sorted(int(m.split("/")[0].split()[-1]) for _, m in msgs[:-1]), msgs[:3])

print("\n== 2. 设置里可以改并发数 ==")
for workers, want in ((1, 1), (5, 5)):
    reset()
    config.save({"tts_workers": workers})
    p = project()
    res, _, wall = run(p)
    check(f"tts_workers={workers}：同时 {want} 段", PEAK[0] == want and res["generated"] == N, PEAK[0])
    if workers == 1:
        check("一段一段做时顺序不变，用时和原来一样", CALLS == [s.narration for s in p.steps] and wall > N * DELAY * 0.9, f"{wall:.2f} 秒")
config.save({"tts_workers": 0})
reset()
p = project()
res, _, _ = run(p, voice="minimax:Some_Voice")
check("付费服务的音色：同时 2 段（它们按套餐限制并发）", PEAK[0] == 2 and res["generated"] == N, PEAK[0])

print("\n== 3. 有一段失败：只影响它自己 ==")
reset()
FAIL_ON.add("第4段")
p = project()
res, _, _ = run(p)
check("报出是哪一段、什么原因，其余 8 段照常配好", res["generated"] == N - 1 and len(res["errors"]) == 1 and p.steps[3].id in res["errors"][0]
      and "service said no" in res["errors"][0] and not p.steps[3].audio and all(s.audio for i, s in enumerate(p.steps) if i != 3), res["errors"])

print("\n== 4. 中途停止：已经做完和正在做的都保留，不再开始新的 ==")


class Stop(BaseException):
    pass


reset()
p = project()
seen = []


def progress_then_stop(f, m):
    seen.append(m)
    if len(seen) == 3:
        raise Stop()


try:
    tts.synth_project(p, progress=progress_then_stop)
    stopped = False
except Stop:
    stopped = True
n_at_stop = len(CALLS)
time.sleep(DELAY * 3)
check("Stop 传了出来", stopped)
check("停下之后没有再开始新的一段", len(CALLS) == n_at_stop and n_at_stop < N, (n_at_stop, len(CALLS)))
check("停下时已经在做的几段，做完后一起保留（和一段一段做时在做的那一段一样）", all(s.audio for s in p.steps[:n_at_stop]), [bool(s.audio) for s in p.steps])
check("还没开始的步骤保持空", not any(s.audio for s in p.steps[n_at_stop:]), [bool(s.audio) for s in p.steps])

print("\n== 4b. 重新配音时停止：每一页的音频文件和它的记录始终是一对（不会新音频配旧时长）==")
adir = None


def has_old_audio(n=8):
    """A project whose steps all already have voice-over (old file + old record)."""
    global adir
    pj = project(n)
    adir = storage.audio_dir(pj.id)
    adir.mkdir(parents=True, exist_ok=True)
    for st in pj.steps:
        (adir / f"{st.id}.mp3").write_bytes(b"old 99.000".ljust(1000, b"x"))
        st.audio, st.audio_duration, st.boundaries, st.voice_source = f"{st.id}.mp3", 99.0, [{"t": 0.0, "d": 1.0, "text": "old"}], "tts"
    return pj


def pair_state(pj):
    """For every step: 'old' (old file with the old record), 'new' (new file with its own record) or 'MIXED'."""
    out = []
    for st in pj.steps:
        head = (adir / st.audio).read_bytes()[:12].rstrip(b"x").decode()
        if head.startswith("old") and st.audio_duration == 99.0 and st.boundaries[0]["text"] == "old":
            out.append("old")
        elif head.startswith("new") and abs(float(head.split()[1]) - st.audio_duration) < 1e-6 and st.boundaries[0]["text"] == st.narration:
            out.append("new")
        else:
            out.append("MIXED")
    return out


def leftovers(folder):
    return [f.name for f in folder.iterdir() if ".new." in f.name or ".part" in f.name]


reset()
p = has_old_audio()
seen = []


def stop_on_third(f, m):
    seen.append(m)
    if len(seen) == 3:
        raise Stop()


try:
    tts.synth_project(p, only_missing=False, progress=stop_on_third)
except Stop:
    pass
st = pair_state(p)
check("停下后每一页都是一对：旧文件配旧记录，或新文件配新记录", "MIXED" not in st and "new" in st and "old" in st, st)
check("没有留下临时文件", not leftovers(adir), leftovers(adir))

reset()
p = has_old_audio()
FAIL_ON.update({"第2段", "第3段"})
seen = []
try:
    tts.synth_project(p, only_missing=False, progress=stop_on_third)
except Stop:
    pass
st = pair_state(p)
check("在飞的有失败的：失败的那几页保持原来的旧音频和旧记录，其余照常", "MIXED" not in st and st[1] == "old" and st[2] == "old", st)
check("失败的也没有留下临时文件", not leftovers(adir), leftovers(adir))

reset()
p = has_old_audio()
res, _, _ = run(p, only_missing=False)
st = pair_state(p)
check("不停止、全部重新配：每一页都换成了新的一对", st == ["new"] * 8 and res["generated"] == 8, st)

print("\n== 5. 已经有配音的跳过，序号仍是它在整个列表里的位置 ==")
reset()
p = project(6)
s2 = p.steps[1]
tts.synth(s2.narration, "v", storage.audio_dir(p.id) / f"{s2.id}.mp3")
s2.audio, s2.audio_duration, s2.voice_source = f"{s2.id}.mp3", 3.0, "tts"
reset()
res, msgs, _ = run(p)
nums = [m.split("：")[0] for _, m in msgs[:-1]]
check("跳过的那段没有重做，也没有占序号", res["generated"] == 5 and p.steps[1].audio_duration == 3.0 and s2.narration not in CALLS, CALLS)
check("进度里的序号是真实位置（没有 2/6）", nums[0] == "合成语音 1/6" and "合成语音 2/6" not in nums and "合成语音 3/6" in nums, nums)

print("\n== 6. 问答项目每页内部已经并行，页与页之间一页一页做 ==")
reset()
p = project(6)
orig = Project.is_dialogue
Project.is_dialogue = lambda self: True
try:
    res, _, _ = run(p)
finally:
    Project.is_dialogue = orig
check("问答项目：同时只有 1 段（不会 3 页 × 每页 4 句同时打出去）", PEAK[0] == 1 and res["generated"] == 6, PEAK[0])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
