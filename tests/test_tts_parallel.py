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
FALLBACK_ON = set()             # paragraphs the fake "reads with Windows' own voice" (the online voice failed)


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def fake_synth(text, voice, out_path, rate="", volume="", pitch="", used=None):
    with LOCK:
        LIVE[0] += 1
        PEAK[0] = max(PEAK[0], LIVE[0])
        CALLS.append(text)
        THREADS.add(threading.current_thread().name)
    try:
        time.sleep(DELAY)
        if any(f in text for f in FAIL_ON):
            raise tts.TTSError("service said no")
        if used is not None and any(f in text for f in FALLBACK_ON):
            used["fallback"] = "Microsoft Huihui"
        dur = 1.0 + len(text) / 100
        out_path.write_bytes(f"new {dur:.3f}".encode().ljust(1000, b"x"))     # the file says which duration it belongs to
        return dur, [{"t": 0.0, "d": 1.0, "text": text}]
    finally:
        with LOCK:
            LIVE[0] -= 1


REAL_SYNTH = tts.synth                # the real one, for section 7
tts.synth = fake_synth


def reset():
    LIVE[0] = PEAK[0] = 0
    CALLS.clear()
    THREADS.clear()
    FAIL_ON.clear()
    FALLBACK_ON.clear()


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

print("\n== 7. 免费的 Edge 语音偶尔拒绝连接；Windows 离线语音一次只能做一句，绝不能卡住整个任务 ==")
import subprocess  # noqa: E402
import wave  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
MP3 = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=f=300:sample_rate=24000", "-t", "1.0", "-f", "mp3", "pipe:1"],
                     capture_output=True, check=True).stdout
tts.EDGE_RETRY_WAIT = 0.02
edge_calls = []


def flaky_edge(fails):
    async def edge(text, voice, out, rate, volume, pitch="+0Hz"):
        edge_calls.append(text)
        if len(edge_calls) <= fails:
            raise RuntimeError("403, message='Invalid response status'")       # what the service answers now and then
        out.write_bytes(MP3)
        return [{"t": 0.0, "d": 1.0, "text": text}]
    return edge


def fake_wav(path, sec=0.5):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * sec))


offline = []
real_edge, real_sapi = tts._edge_synth, tts._sapi_run
tts._sapi_run = lambda text, out, lang: (offline.append(text), fake_wav(out), "Microsoft Huihui")[-1]
OUTD = DATA / "edge"
OUTD.mkdir(exist_ok=True)
try:
    tts._edge_synth = flaky_edge(2)
    dur, bounds = REAL_SYNTH("被拒两次的一句。", "zh-CN-XiaoxiaoNeural", OUTD / "a.mp3")
    check("Edge 被拒两次：重试后用 Edge 的声音做好了（不退回离线语音）", len(edge_calls) == 3 and not offline and bounds and dur > 0.5,
          (len(edge_calls), offline, bounds))
    edge_calls.clear()
    tts._edge_synth = flaky_edge(99)
    dur, bounds = REAL_SYNTH("一直被拒的一句。", "zh-CN-XiaoxiaoNeural", OUTD / "b.mp3")
    check("Edge 一直不行：试了 4 次之后才用离线语音", len(edge_calls) == tts.EDGE_TRIES and offline == ["一直被拒的一句。"] and dur > 0.2 and bounds == [],
          (len(edge_calls), offline))

    # several voice-over threads need the offline voice at the same time (Q&A lines are made 4 at once)
    live, peak, names = [0], [0], set()

    def slow_sapi(text, out, lang):
        with LOCK:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            names.add(threading.current_thread().name)
        time.sleep(0.15)
        fake_wav(out)
        with LOCK:
            live[0] -= 1
        return "Microsoft Huihui"
    tts._sapi_run = slow_sapi
    errs = []

    def offline_one(i):
        try:
            tts._pyttsx3_synth(f"第{i}句", OUTD / f"o{i}.wav")
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=offline_one, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    check("4 个线程同时要离线语音：一句一句做，都在同一个专门的线程里", peak[0] == 1 and names == {"sapi5"} and not errs
          and all((OUTD / f"o{i}.wav").exists() for i in range(4)), (peak[0], names, errs))

    # the offline voice hangs: it must not hang the voice-over with it
    gate = threading.Event()
    tts._sapi_run = lambda text, out, lang: gate.wait(30)
    tts.SAPI_TIMEOUT = 0.5
    t0 = time.perf_counter()
    try:
        tts._pyttsx3_synth("卡住的一句", OUTD / "stuck.wav")
        raised = False
    except tts.TTSError:
        raised = True
    waited = time.perf_counter() - t0
    check("离线语音卡住：最多等 SAPI_TIMEOUT 就报错，不会永远等下去", raised and waited < 2.0, f"{waited:.2f} 秒")
    t0 = time.perf_counter()
    try:
        tts._pyttsx3_synth("之后的一句", OUTD / "after.wav")
        fast = False
    except tts.TTSError:
        fast = time.perf_counter() - t0 < 0.1
    check("卡住过一次以后直接报错，不再排队等它", fast)
    edge_calls.clear()
    tts._edge_synth = flaky_edge(99)
    t0 = time.perf_counter()
    try:
        REAL_SYNTH("这一段 Edge 和离线语音都不行。", "zh-CN-XiaoxiaoNeural", OUTD / "c.mp3")
        failed = None
    except tts.TTSError as e:
        failed = str(e)
    check("这时整段配音报出失败（配音任务把它记成失败的一段，接着做下一段），而不是卡住", failed and time.perf_counter() - t0 < 5.0, failed)
finally:
    gate.set() if "gate" in dir() else None
    tts._edge_synth, tts._sapi_run = real_edge, real_sapi
    tts._sapi_state["stuck"] = False
    tts.SAPI_TIMEOUT = 90.0

edge_lists = [0]


async def flaky_list():
    edge_lists[0] += 1
    if edge_lists[0] <= 2:
        raise RuntimeError("403")
    return [{"ShortName": "zh-CN-XiaoxiaoNeural", "Locale": "zh-CN", "Gender": "Female", "FriendlyName": "Microsoft Xiaoxiao Online (Natural)"}]


import edge_tts  # noqa: E402
real_list = edge_tts.list_voices
edge_tts.list_voices = flaky_list
try:
    vs = tts.list_voices(force=True)
finally:
    edge_tts.list_voices = real_list
check("音色列表也一样：被拒两次后照样取到", edge_lists[0] == 3 and vs and vs[0]["name"] == "zh-CN-XiaoxiaoNeural", (edge_lists[0], vs[:1]))

print("\n== 8. Edge 连不上时：用这台电脑上同一种语言的 Windows 语音，结果里提醒，下次合成语音时自动重配 ==")
import types  # noqa: E402


class FakeVoice:
    def __init__(self, vid, name, langs):
        self.id, self.name, self.languages = vid, name, langs


# what a Windows with the English and the Chinese voice installed lists (the default voice is the English one)
VOICES = [FakeVoice("david", "Microsoft David Desktop", ["en-US"]), FakeVoice("zira", "Microsoft Zira Desktop", ["en-US"]),
          FakeVoice("huihui", "Microsoft Huihui Desktop", ["zh-CN"])]
check("音色的语言：zh-CN-XiaoxiaoNeural 是 zh-CN；付费服务的音色 id 说不出", tts.voice_lang("zh-CN-XiaoxiaoNeural") == "zh-CN"
      and tts.voice_lang("zh-CN-liaoning-XiaobeiNeural") == "zh-CN" and tts.voice_lang("doubao:zh_female_x") == "")


def pick(lang):
    return getattr(tts._sapi_pick(VOICES, lang), "id", None)


check("中文找到中文的 Windows 语音，而不是默认的英文 David（它会跳过所有汉字，只念英文单词）", pick("zh-CN") == "huihui", pick("zh-CN"))
check("同一种语言的其他地区也行（en-GB 用美式英语的声音）", pick("en-GB") == "david", pick("en-GB"))
check("这台电脑没有这种语言的 Windows 语音：None，不拿别的语言凑合", pick("de-DE") is None)


class FakeEngine:
    def __init__(self):
        self.props, self.saved = {}, []

    def getProperty(self, key):
        return VOICES if key == "voices" else self.props.get(key)

    def setProperty(self, key, value):
        self.props[key] = value

    def save_to_file(self, text, path):
        self.saved.append(text)
        fake_wav(Path(path))

    def runAndWait(self):
        pass

    def stop(self):
        pass


ENGINE = FakeEngine()
real_pyttsx3 = sys.modules.get("pyttsx3")
sys.modules["pyttsx3"] = types.SimpleNamespace(init=lambda: ENGINE)
try:
    name = tts._sapi_run("中文这一句。", OUTD / "zh.wav", "zh-CN")
    check("Windows 语音用中文的声音读中文", ENGINE.props.get("voice") == "huihui" and name == "Microsoft Huihui Desktop"
          and ENGINE.saved == ["中文这一句。"], (ENGINE.props, name))
    try:
        tts._sapi_run("Guten Tag.", OUTD / "de.wav", "de-DE")
        err = None
    except tts.TTSError as e:
        err = str(e)
    check("没有德语的 Windows 语音：报错说明是哪种语言，不生成音频", err and "de-DE" in err and not (OUTD / "de.wav").exists(), err)
finally:
    if real_pyttsx3 is None:
        sys.modules.pop("pyttsx3", None)
    else:
        sys.modules["pyttsx3"] = real_pyttsx3

real_edge, real_sapi = tts._edge_synth, tts._sapi_run
langs = []
edge_calls.clear()
tts._edge_synth = flaky_edge(99)
tts._sapi_state["stuck"] = False
tts._sapi_run = lambda text, out, lang: (langs.append(lang), fake_wav(out), "Microsoft Huihui Desktop")[-1]
try:
    used = {}
    REAL_SYNTH("在线语音连不上的一句。", "zh-CN-XiaoxiaoNeural", OUTD / "d.mp3", used=used)
    check("Edge 一直不行：退回时交给 Windows 的是解说的语言，并告诉调用方是哪个声音读的", langs == ["zh-CN"]
          and used.get("fallback") == "Microsoft Huihui Desktop", (langs, used))

    def no_voice(text, out, lang):
        raise tts.TTSError(f"这台电脑没有 {lang} 的 Windows 语音可以代替")
    tts._sapi_run = no_voice
    try:
        REAL_SYNTH("又一句。", "zh-CN-XiaoxiaoNeural", OUTD / "e.mp3")
        err = None
    except tts.TTSError as e:
        err = str(e)
    check("连同一种语言的 Windows 语音也没有：这一段报失败（不再生成只有英文单词的音频）", err and "zh-CN" in err and not (OUTD / "e.mp3").exists(), err)
finally:
    tts._edge_synth, tts._sapi_run = real_edge, real_sapi

reset()
config.save({"tts_workers": 0})
p = project(4)
p.intro = "片头这一句。"
FALLBACK_ON.update({"第2段", "片头"})
res, msgs, wall = run(p)
check("片头和第 2 段用了 Windows 语音：结果里有提醒（编辑器会醒目地弹出来），说了几段", res["fallback"] == 2 and "2 段" in res["warning"]
      and "② 合成语音" in res["warning"], res)
check("步骤记下读它的声音：第 2 段是 sapi:…，其余是项目的音色", p.steps[1].audio_voice == "sapi:Microsoft Huihui"
      and all(s.audio_voice == res["voice"] for i, s in enumerate(p.steps) if i != 1), [s.audio_voice for s in p.steps])
reset()
res2, _, _ = run(p)
check("再点「合成语音」：只重配 Windows 语音读过的那两段（片头、第 2 段），别的跳过；这次正常，就没有提醒",
      sorted(CALLS) == sorted(["第2段解说。", "片头这一句。"]) and res2["fallback"] == 0 and not res2["warning"]
      and p.steps[1].audio_voice == res2["voice"], (CALLS, res2))
reset()
FAIL_ON.add("第3段")
res3, _, _ = run(project(3))
check("有一段没能配音：也写进提醒（不再只在进度条上一闪而过）", res3["errors"] and "1 段没能配音" in res3["warning"]
      and "service said no" in res3["warning"], res3["warning"])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
