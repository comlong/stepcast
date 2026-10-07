"""Upload size limits (refused while the body arrives, nothing written to disk) and deleting a project while its jobs are running."""
import io
import os
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "upload_delete_data"
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
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import jobs, slides  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
TMP = config.DATA_DIR / "_tmp"
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def leftovers():
    return sorted(p.name for p in TMP.glob("*")) if TMP.exists() else []


print("\n== 1. 限量复制 ==")
dst = DATA / "copy.bin"
n = storage.copy_limited(io.BytesIO(b"x" * 3000), dst, 4096)
check("没超过：照常写完", n == 3000 and dst.stat().st_size == 3000)
try:
    storage.copy_limited(io.BytesIO(b"x" * 5000), dst, 4096, chunk=1000)
    raised = False
except storage.UploadTooLarge as e:
    raised = e.max_bytes == 4096 and "文件太大" in str(e)
check("超过：马上停下，并把写了一半的文件删掉", raised and not dst.exists())
check("大小的写法", [storage.size_label(x) for x in (2 * 1024 ** 3, 80 * 1024 ** 2, 200 * 1024 ** 2)] == ["2 GB", "80 MB", "200 MB"])
check("各类上传都有上限", len(main.UPLOAD_LIMITS) == 7 and all(m > 0 for _, m in main.UPLOAD_LIMITS))

print("\n== 2. 超大上传：不落盘就拒绝（413） ==")
real_limits = list(main.UPLOAD_LIMITS)
real_overhead = main.UPLOAD_OVERHEAD
main.UPLOAD_LIMITS[:] = [(rx, 4096) for rx, _ in real_limits]
main.UPLOAD_OVERHEAD = 1024
analyzed = []
real_analyze = slides.analyze
slides.analyze = lambda *a, **k: analyzed.append(1) or {}

r = c.post("/api/import/slides", files={"file": ("big.pptx", b"x" * 20000)}, headers=H)
check("幻灯片：声明的大小超限，直接 413", r.status_code == 413 and "文件太大" in r.json()["detail"], r.text[:80])
check("没有留下临时文件，也没开始处理", leftovers() == [] and not analyzed, leftovers())

r = c.post("/api/transcribe", files={"file": ("a.webm", b"x" * 20000)}, headers=H)
check("听写录音也一样", r.status_code == 413 and leftovers() == [], r.status_code)

proj = storage.create("上传限制", "zh-CN")
storage.save(proj)
r = c.post(f"/api/projects/{proj.id}/magic-mic", files={"file": ("s.webm", b"x" * 20000)}, data={"rec_start": "0"}, headers=H)
check("边录边讲的录音也一样", r.status_code == 413, r.status_code)
r = c.post(f"/api/projects/{proj.id}/card/intro/background", files={"file": ("b.png", b"x" * 20000)}, headers=H)
check("片头背景也一样", r.status_code == 413 and leftovers() == [], r.status_code)


def chunks():
    yield b'--xx\r\nContent-Disposition: form-data; name="file"; filename="a.pptx"\r\n\r\n'
    for _ in range(40):
        yield b"x" * 1024


r = c.post("/api/import/slides", content=chunks(), headers={**H, "Content-Type": "multipart/form-data; boundary=xx"})
check("没有声明大小（分块上传）：收到超过上限的数据就拒绝", r.status_code == 413 and leftovers() == [] and not analyzed,
      (r.status_code, r.text[:60]))

with open(ROOT / "tests" / "fixtures" / "notes_deck.pptx", "rb") as f:
    deck = f.read()
main.UPLOAD_LIMITS[:] = [(rx, len(deck) + 100) for rx, _ in real_limits]
slides.analyze = real_analyze
r = c.post("/api/import/slides", files={"file": ("ok.pptx", deck)}, headers=H)
check("没超限的上传照常处理", r.status_code == 200 and r.json()["kind"] == "slides_analyze", r.text[:80])
for _ in range(100):
    j = c.get(f"/api/jobs/{r.json()['id']}").json()
    if j["status"] not in ("pending", "running"):
        break
    time.sleep(0.2)
check("处理完成", j["status"] == "done", j.get("error"))

main.UPLOAD_LIMITS[:] = real_limits
main.UPLOAD_OVERHEAD = real_overhead

print("\n== 3. 删除项目：先停掉它的任务 ==")


def wait_done(jid, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = jobs.get(jid)
        if j["status"] not in ("pending", "running"):
            return j
        time.sleep(0.05)
    return jobs.get(jid)


def make_project(name):
    p = storage.create(name, "zh-CN")
    storage.save(p)
    return p.id


pid = make_project("删除时在渲染")


def render_like(job):
    try:
        while True:                                   # like a render: reports progress (the stop signal is checked there)
            job.progress(0.5, "渲染中")
            time.sleep(0.05)
    finally:
        (storage.project_dir(pid) / "output").mkdir(parents=True, exist_ok=True)      # what a render does as it ends


started = jobs.submit("render", render_like, pid, exclusive=jobs.HEAVY)
time.sleep(0.3)
r = c.delete(f"/api/projects/{pid}", headers=H)
check("删除成功", r.status_code == 200 and r.json() == {"ok": True}, r.text[:80])
j = wait_done(started["id"])
check("渲染任务已被停止", j["status"] == "cancelled", j["status"])
time.sleep(0.5)
check("项目文件夹没有被任务重新建出来", not storage.project_dir(pid).exists())
check("项目列表里也没有", all(p["id"] != pid for p in storage.list_projects()))

pid2 = make_project("停不下来的任务")
release = []


def stubborn(job):
    t0 = time.time()
    while time.time() - t0 < 1.5:                      # never looks at the stop signal
        time.sleep(0.05)
    release.append(1)


jobs.STOP_WAIT = 0.3
started = jobs.submit("render", stubborn, pid2, exclusive=jobs.HEAVY)
time.sleep(0.2)
r = c.delete(f"/api/projects/{pid2}", headers=H)
check("任务来不及停：不删，提示稍后再试（409）", r.status_code == 409 and "渲染" in r.json()["detail"], r.text[:100])
check("项目还在", storage.project_dir(pid2).exists())
wait_done(started["id"], 5)
r = c.delete(f"/api/projects/{pid2}", headers=H)
check("任务结束后可以删除", r.status_code == 200 and not storage.project_dir(pid2).exists(), r.text[:80])
jobs.STOP_WAIT = 20.0

pid3 = make_project("删除中不接新任务")
with jobs.closing(pid3):
    try:
        jobs.submit("render", lambda job: None, pid3)
        blocked = False
    except jobs.ProjectClosing as e:
        blocked = "正在删除" in str(e)
    try:
        main._submit("render", lambda job: None, pid3)
        mapped = None
    except main.HTTPException as e:
        mapped = e.status_code
check("正在删除的项目不再接受新任务（409）", blocked and mapped == 409, (blocked, mapped))
j = jobs.submit("render", lambda job: "ok", pid3)
check("删除没发生后又能正常提交", wait_done(j["id"])["status"] == "done")

r = c.delete("/api/projects/p_doesnotexist0", headers=H)
check("删除不存在的项目：照常返回", r.status_code == 200 and r.json() == {"ok": False}, r.text[:60])

print("\n== 4. 删除和上传提交撞车：项目已经删完，再提交的任务一律拒绝 ==")
ran = []
pid4 = make_project("删完再提交")
storage.delete(pid4)
try:
    main._submit("render", lambda job: ran.append(1), pid4)
    code = None
except main.HTTPException as e:
    code = e.status_code
check("项目已经不在（删除已结束，标记也清了）：新任务被拒绝（404），不会跑", code == 404 and not ran, (code, ran))

# the real sequence: the upload is still arriving (the project is checked at its start), the delete finishes meanwhile, then the job is submitted
pid5 = make_project("上传中被删")
real_copy = storage.copy_limited


def copy_then_delete(src, dst, limit, **kw):
    n = real_copy(src, dst, limit, **kw)
    with jobs.closing(pid5):                           # what DELETE does, finished before the upload gets to submit its job
        jobs.stop_project(pid5)
        storage.delete(pid5)
    return n


storage.copy_limited = copy_then_delete
try:
    png = io.BytesIO()
    from PIL import Image  # noqa: E402
    Image.new("RGB", (16, 16), (200, 30, 30)).save(png, "PNG")
    r = c.post(f"/api/projects/{pid5}/card/intro/background", files={"file": ("bg.png", png.getvalue(), "image/png")}, headers=H)
finally:
    storage.copy_limited = real_copy
time.sleep(0.3)
check("上传时项目被删了：返回 404，任务没有启动", r.status_code == 404, (r.status_code, r.text[:80]))
check("没有给已删除的项目留下目录", not storage.project_dir(pid5).exists())
check("上传的临时文件也删掉了", not leftovers(), leftovers())

# a step's voice recording: it used to be written into the project's own folder first, which brought the folder back
from backend.models import Step  # noqa: E402
p6 = storage.create("录音上传中被删", "zh-CN")
p6.steps = [Step(kind="slide", narration="一句话。")]
storage.save(p6)
pid5, sid6 = p6.id, p6.steps[0].id
from backend.services import voice as voice_mod  # noqa: E402
real_save = voice_mod.save_upload


def delete_then_save(*a, **k):
    with jobs.closing(pid5):                           # the delete finishes after the request checked the project, before the upload is written
        jobs.stop_project(pid5)
        storage.delete(pid5)
    return real_save(*a, **k)


voice_mod.save_upload = delete_then_save
try:
    r = c.post(f"/api/projects/{pid5}/steps/{sid6}/voice", files={"file": ("rec.webm", b"x" * 2000, "audio/webm")},
               data={"transcribe": "false"}, headers=H)
finally:
    voice_mod.save_upload = real_save
check("录音上传时项目被删了：返回 404", r.status_code == 404, (r.status_code, r.text[:80]))
check("录音也没有把已删除项目的目录建回来", not storage.project_dir(pid5).exists(), [p.name for p in storage.project_dir(pid5).rglob("*")][:5]
      if storage.project_dir(pid5).exists() else "")
check("录音的临时文件也删掉了", not leftovers(), leftovers())

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
