"""Playback of single pages and of several pages in a row: a few pages are rendered as a small H.264 video for the editor (so pages with videos in any
format can be watched), without touching the real video or its subtitle files. Synthetic project; no AI, no network."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "playback_data"
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
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.models import Rect, Step, VideoClip  # noqa: E402
from backend.services import clips, slide_sequence, video  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def wait(jid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{jid}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.1)
    raise TimeoutError


def play(pid, ids):
    r = c.post(f"/api/projects/{pid}/playback", json={"steps": ids}, headers=H)
    assert r.status_code == 200, r.text
    return wait(r.json()["id"])


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt,width,height,r_frame_rate",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


print("\n== 1. 准备：三页幻灯片，第二页带一个浏览器放不了的 AVI 视频 ==")
proj = storage.create("回放", "zh-CN")
proj.source = "slides"
proj.settings = {"intro_enabled": False, "outro_enabled": False, "burn_subtitles": True}
proj.title, proj.intro = "回放测试", "欢迎。"
shots, auds, media = storage.screenshots_dir(proj.id), storage.audio_dir(proj.id), clips.media_dir(proj.id)
for d in (auds, media):
    d.mkdir(parents=True, exist_ok=True)
steps = []
for k, col in enumerate([(200, 60, 60), (60, 160, 60), (60, 60, 200)]):
    Image.new("RGB", (1920, 1080), col).save(shots / f"s{k}.png")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:sample_rate=24000", "-t", "2.0", str(auds / f"s{k}.mp3")], check=True)
    steps.append(Step(kind="slide", screenshot=f"s{k}.png", img_w=1920, img_h=1080, viewport_w=1920, viewport_h=1080, zoom=False,
                      highlight=False, narration=f"第 {k + 1} 页。", caption=f"第 {k + 1} 页。", audio=f"s{k}.mp3", audio_duration=2.0,
                      index=k, page_title=f"页 {k + 1}"))
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=1.5", "-c:v", "mpeg4", "-f", "avi", str(media / "v.avi")], check=True)
vid = Step(kind="video", screenshot="s1.png", img_w=1920, img_h=1080, viewport_w=1920, viewport_h=1080, zoom=False, highlight=False, index=2,
           clip=VideoClip(file="v.avi", source="v.avi", duration=1.5, has_audio=False, mode="inset", rect=Rect(x=0.5, y=0.3, w=0.4, h=0.4), audio="mute"))
proj.steps = [steps[0], steps[1], vid, steps[2]]
s1, s2, v2, s3 = proj.steps
proj.output = "final.mp4"
storage.save(proj)
out_dir = storage.output_dir(proj.id)
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "final.srt").write_text("REAL SUBTITLES", encoding="utf-8")
(out_dir / "final.mp4").write_bytes(b"REAL VIDEO")
before = {f.name: (f.stat().st_size, f.read_bytes()) for f in out_dir.iterdir()}
codec = probe(media / "v.avi")["codec_name"]
check("这个视频的编码浏览器放不了（mpeg4 / avi）", codec == "mpeg4", codec)
check("页的划分：幻灯片带它后面的视频；点视频步骤也是这一页",
      [s.id for s in slide_sequence.page_steps(proj.steps, [s2.id])] == [s2.id, v2.id]
      and [s.id for s in slide_sequence.page_steps(proj.steps, [v2.id])] == [s2.id, v2.id]
      and [s.id for s in slide_sequence.page_steps(proj.steps, [s3.id, s1.id])] == [s1.id, s3.id])

print("\n== 2. 单页回放：带视频的页 ==")
r = play(proj.id, [s2.id])
check("生成成功", r["status"] == "done", r.get("error"))
res = r["result"]
f2 = storage.preview_dir(proj.id) / res["file"]
check("文件在回放文件夹里", f2.exists() and f2.parent.name == "preview", str(f2))
pr = probe(f2)
check("是浏览器放得了的 H.264 / yuv420p", pr["codec_name"] == "h264" and pr["pix_fmt"] == "yuv420p", pr)
check("尺寸 1280 宽、24 帧（回放用小一点的画质）", pr["width"] == 1280 and pr["height"] == 720 and pr["r_frame_rate"] == "24/1", pr)
seg = res["segments"]
check("这一页 = 幻灯片 + 它的视频，按顺序", [x["id"] for x in seg] == [s2.id, v2.id], seg)
check("时长 = 幻灯片（配音 2 秒 + 停顿）+ 视频 1.5 秒；只有这一页，不是整个项目",
      1.5 + 2.0 < res["duration"] < 1.5 + 2.0 + 2.5 and seg[0]["start"] == 0 and abs(seg[1]["end"] - res["duration"]) < 0.05, res["duration"])
rg = c.get(f"/api/projects/{proj.id}/file/preview/{res['file']}", headers={**H, "Range": "bytes=0-99"})
check("播放器可以拖动进度条（支持分段读取）", rg.status_code == 206 and len(rg.content) == 100, rg.status_code)
check("选视频步骤：同样是这一页", [x["id"] for x in play(proj.id, [v2.id])["result"]["segments"]] == [s2.id, v2.id])

print("\n== 3. 连播：几页连续放 ==")
r = play(proj.id, [s3.id, s1.id])
seg = r["result"]["segments"]
check("按项目里的顺序，不按选的顺序", [x["id"] for x in seg] == [s1.id, s3.id], seg)
check("首尾相接，没有空隙", abs(seg[0]["end"] - seg[1]["start"]) < 0.01 and abs(seg[1]["end"] - r["result"]["duration"]) < 0.05, seg)
r = play(proj.id, [s1.id, s2.id, s3.id])
check("全选：三页和中间的视频", [x["id"] for x in r["result"]["segments"]] == [s1.id, s2.id, v2.id, s3.id])

print("\n== 4. 不动正式的视频和字幕 ==")
after = {f.name: (f.stat().st_size, f.read_bytes()) for f in out_dir.iterdir()}
check("正式视频和字幕文件原样不变，也没有多出文件", after == before, sorted(after))
check("项目里记的成片没变", storage.load(proj.id).output == "final.mp4")

print("\n== 5. 片头片尾、被排除的页、选择为空 ==")
r = play(proj.id, ["__intro__"])
check("只选片头：只放片头", r["status"] == "done" and [x["id"] for x in r["result"]["segments"]] == ["__intro__"], r.get("error") or r["result"]["segments"])
r = play(proj.id, [s1.id])
check("没选片头就不放（即使项目设了片头）", [x["id"] for x in r["result"]["segments"]] == [s1.id])
pj = storage.load(proj.id)
pj.steps[3].include = False
storage.save(pj)
r = play(proj.id, [s3.id])
check("被排除出成片的页，特意选了也能放", r["status"] == "done" and [x["id"] for x in r["result"]["segments"]] == [s3.id], r.get("error"))
bad = c.post(f"/api/projects/{proj.id}/playback", json={"steps": []}, headers=H)
check("什么都没选：提示先选", bad.status_code == 400 and "选择" in bad.json()["detail"], bad.text[:60])

print("\n== 6. 只留最近几个回放文件 ==")
for _ in range(5):
    play(proj.id, [s1.id])
files = list(storage.preview_dir(proj.id).glob("play_*.mp4"))
check("最多留 %d 个" % main.PREVIEW_KEEP, len(files) == main.PREVIEW_KEEP, len(files))

print("\n== 7. 停止，新的请求顶掉还没做完的 ==")
real = video.render_project
runs = []


def slow_render(pv, progress=None, overrides=None, preview_out=None):
    runs.append(1)
    for i in range(400):                       # like a render: reports progress (the stop signal is checked there)
        progress(i / 400, "渲染中")
        time.sleep(0.05)
    return {"file": "x.mp4", "duration": 1.0, "segments": []}


video.render_project = slow_render
j1 = c.post(f"/api/projects/{proj.id}/playback", json={"steps": [s1.id]}, headers=H).json()
time.sleep(0.4)
t0 = time.time()
c.post(f"/api/jobs/{j1['id']}/cancel", headers=H)
r1 = wait(j1["id"], 10)
check("点停止：很快停下（不用等渲染做完）", r1["status"] == "cancelled" and time.time() - t0 < 3, (r1["status"], round(time.time() - t0, 1)))
j2 = c.post(f"/api/projects/{proj.id}/playback", json={"steps": [s1.id]}, headers=H).json()
time.sleep(0.3)
j3 = c.post(f"/api/projects/{proj.id}/playback", json={"steps": [s3.id]}, headers=H).json()
r2 = wait(j2["id"], 10)
check("再点另一页：前一个被顶掉，不会同时做两个", r2["status"] == "cancelled" and j3["id"] != j2["id"], r2["status"])
c.post(f"/api/jobs/{j3['id']}/cancel", headers=H)
wait(j3["id"], 10)
video.render_project = real
check("回放的任务不挡正式渲染（不在互斥的任务里）", "playback" not in __import__("backend.services.jobs", fromlist=["HEAVY"]).HEAVY)

print("\n== 8. 录制的项目：只放中间几步（光标、放大照样有） ==")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures_build import make_capture_project  # noqa: E402

rid = make_capture_project(config.DATA_DIR, pid="p_beef00000002")
rp = storage.load(rid)
mid = [rp.steps[2].id, rp.steps[3].id]
r = play(rid, mid)
check("录制的步骤可以只放其中几步", r["status"] == "done" and [x["id"] for x in r["result"]["segments"]] == mid, r.get("error"))
fr = storage.preview_dir(rid) / r["result"]["file"]
whole = sum(video.step_duration(rp.steps[k], video.effective_config(rp)) for k in (2, 3))
check("时长 = 这两步，不是整个项目（约 70 秒）", fr.exists() and abs(r["result"]["duration"] - whole) < 0.2 and r["result"]["duration"] < 30,
      (r["result"]["duration"], whole))
check("也是浏览器放得了的 H.264", probe(fr)["codec_name"] == "h264")

print("\n== 9. 回放前先把页面内容和解说对齐（和渲染成片一样）：从没渲染过的页动画才不会出现得太早 ==")
import json as _json  # noqa: E402

from backend.models import RevealItem, SlideReveal  # noqa: E402
from backend.services import llm as llm_mod, slide_reveal  # noqa: E402

NAR = "先看第一条。再看第二条。最后看第三条。"
rproj = storage.create("对齐回放", "zh-CN")
rproj.source = "slides"
rproj.settings = {"intro_enabled": False, "outro_enabled": False, "burn_subtitles": True, "slides_reveal": True}
rshots = storage.screenshots_dir(rproj.id)
Image.new("RGB", (1920, 1080), (30, 30, 40)).save(rshots / "clean.png")
rsteps = []
for k in range(2):
    Image.new("RGB", (1920, 1080), (30, 30, 40)).save(rshots / f"p{k}.png")
    its = []
    for j in range(3):
        Image.new("RGBA", (300, 120), (200, 200, 60, 255)).save(rshots / f"p{k}_{j}.png")
        its.append(RevealItem(file=f"p{k}_{j}.png", x=200 + 500 * j, y=400, text=f"第{'一二三'[j]}条"))
    rsteps.append(Step(kind="slide", screenshot=f"p{k}.png", img_w=1920, img_h=1080, viewport_w=1920, viewport_h=1080, zoom=False, highlight=False,
                       index=k, narration=NAR, caption=NAR, reveal=SlideReveal(clean="clean.png", items=its)))
rproj.steps = rsteps
storage.save(rproj)
ra, rb = rsteps[0].id, rsteps[1].id


class FakeAlign:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        prompt = messages[-1]["content"]
        FakeAlign.calls.append(prompt)
        pages = _json.loads(prompt[prompt.rindex("\n\n[") + 2:])
        return {"pages": [{"page": pg["page"], "map": [min(i, len(pg["sentences"])) for i in range(1, len(pg["items"]) + 1)]} for pg in pages]}


orig_get = llm_mod.get_client
llm_mod.get_client = FakeAlign
try:
    r = play(rproj.id, [ra])
    st = storage.load(rproj.id).steps
    sents = slide_reveal.sentences(NAR)
    check("没渲染过的页回放前对齐了：每一条对上了它在解说里的句子", r["status"] == "done" and st[0].reveal.align == [x[0] for x in sents]
          and st[0].reveal.align_key, (r["status"], r.get("error"), st[0].reveal.align))
    asked = [len(_json.loads(p[p.rindex("\n\n[") + 2:])) for p in FakeAlign.calls]
    check("只问了一次 AI，而且只问了选中的那一页", asked == [1], asked)
    check("没选的页没有被对齐", st[1].reveal.align == [] and st[1].reveal.align_key == "")
    play(rproj.id, [ra])
    check("再放一次：解说没变，不再问 AI", len(FakeAlign.calls) == 1, len(FakeAlign.calls))
    storage.update(rproj.id, lambda p: setattr(p.steps[0], "narration", NAR + "补一句。"))
    play(rproj.id, [ra])
    check("改了解说：下次回放重新对齐", len(FakeAlign.calls) == 2, len(FakeAlign.calls))
    play(rproj.id, [ra, rb])
    check("同时放两页：只补对齐还没对齐的那一页", len(FakeAlign.calls) == 3 and storage.load(rproj.id).steps[1].reveal.align_key, len(FakeAlign.calls))

    def no_ai(*a, **k):
        raise llm_mod.LLMError("no key")
    storage.update(rproj.id, lambda p: setattr(p.steps[1], "narration", NAR + "再补一句。"))
    llm_mod.get_client = no_ai
    r = play(rproj.id, [rb])
    check("没有配 AI（或出错）：照样放，只是不对齐", r["status"] == "done" and (storage.preview_dir(rproj.id) / r["result"]["file"]).exists(),
          (r["status"], r.get("error")))
finally:
    llm_mod.get_client = orig_get

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
