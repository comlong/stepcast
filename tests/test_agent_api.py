"""Local API for external assistants: copy steps between projects (with cropping), crop in place, image slides,
the "built-in AI off" switch, rendering without sidecar subtitle files, and second-language subtitles translated elsewhere."""
import base64
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "agent_api_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import llm, second_subs, step_tools  # noqa: E402
from fixtures_build import DPR, IMG_H, IMG_W, make_capture_project  # noqa: E402

fails = []
c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def wait(jid):
    while c.get(f"/api/jobs/{jid}").json()["status"] in ("pending", "running"):
        time.sleep(0.2)
    return c.get(f"/api/jobs/{jid}").json()


def png_b64(w=800, h=450, color=(30, 60, 90)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


src_pid = make_capture_project(config.DATA_DIR)
src = storage.load(src_pid)

print("\n== 1. 跨项目复制步骤（带裁边）==")
dst = c.post("/api/projects", headers=H, json={"name": "agent 拼装", "language": "zh-CN"}).json()
pid = dst["id"]
s0, s3 = src.steps[0], src.steps[3]
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["0", s3.id], "crop": 20})
check("复制成功", r.status_code == 200, r.text[:200])
p = storage.load(pid)
check("两步按顺序加进来，编号重排", [s.index for s in p.steps] == [0, 1] and p.steps[0].narration == s0.narration
      and p.steps[1].narration == s3.narration)
a = p.steps[0]
check("新 id，截图是这个项目自己的文件", a.id != s0.id and (storage.screenshots_dir(pid) / a.screenshot).exists())
with Image.open(storage.screenshots_dir(pid) / a.screenshot) as im:
    size = im.size
check("截图四边各裁掉 20 像素", size == (IMG_W - 40, IMG_H - 40) and (a.img_w, a.img_h) == size, size)
off = 20 / DPR
check("高亮框和点击位置跟着平移（CSS 像素）",
      abs(a.target.rect.x - (s0.target.rect.x - off)) < 1e-6 and abs(a.target.rect.y - (s0.target.rect.y - off)) < 1e-6
      and abs(a.point["x"] - (s0.point["x"] - off)) < 1e-6, (a.target.rect, a.point))
check("视口尺寸按比例变小", (a.viewport_w, a.viewport_h) == (round(size[0] / DPR), round(size[1] / DPR)))
check("配音不带过来（属于原项目）", a.audio == "" and a.audio_duration == 0 and a.boundaries == [])
check("原项目没被改动", storage.load(src_pid).steps[0].target.rect.x == s0.target.rect.x
      and Image.open(storage.screenshots_dir(src_pid) / s0.screenshot).size == (IMG_W, IMG_H))
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["5"], "position": 1})
check("指定位置插入、不裁边", r.status_code == 200 and storage.load(pid).steps[1].narration == src.steps[5].narration
      and storage.load(pid).steps[1].img_w == IMG_W)
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["s_nope"]})
check("不存在的步骤：400", r.status_code == 400 and "s_nope" in r.json()["detail"], r.text[:120])
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["0"], "crop": 9999})
check("裁边超范围：400", r.status_code == 400, r.text[:120])
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": "p_missing00000", "steps": ["0"]})
check("源项目不存在：404", r.status_code == 404)
shots = sorted(x.name for x in storage.screenshots_dir(pid).iterdir())
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["0", "1", "s_nope"]})
check("复制到一半出错：已经写进来的截图删掉，不留孤儿文件", r.status_code == 400
      and sorted(x.name for x in storage.screenshots_dir(pid).iterdir()) == shots, r.text[:120])
r = c.post(f"/api/projects/{pid}/steps/copy", headers=H, json={"source": src_pid, "steps": ["0"], "position": "first"})
check("位置不是数字：400（不是服务器错误）", r.status_code == 400 and "position" in r.json()["detail"], r.text[:120])
check("裁边不是数字：400", c.post(f"/api/projects/{pid}/steps/crop", headers=H, json={"crop": "a"}).status_code == 400)

print("\n== 2. 原地裁边 ==")
before = storage.load(pid)
first = before.steps[1]
r = c.post(f"/api/projects/{pid}/steps/crop", headers=H, json={"steps": [first.id], "crop": 10})
after = storage.load(pid).steps[1]
check("只裁指定的步骤", r.status_code == 200 and r.json()["cropped"] == [first.id]
      and (after.img_w, after.img_h) == (first.img_w - 20, first.img_h - 20), r.text[:120])
check("坐标跟着平移", abs(after.target.rect.x - (first.target.rect.x - 10 / DPR)) < 1e-6)
# all or nothing: one picture that can't be cropped must not leave the others cut with their old sizes and coordinates
aon = c.post("/api/projects", headers=H, json={"name": "裁边全有或全无", "language": "zh-CN"}).json()["id"]
c.post(f"/api/projects/{aon}/steps/copy", headers=H, json={"source": src_pid, "steps": ["0", "1"]})
good, bad = storage.load(aon).steps
good_file = storage.screenshots_dir(aon) / good.screenshot
good_bytes = good_file.read_bytes()
Image.new("RGB", (100, 100), (200, 200, 200)).save(storage.screenshots_dir(aon) / bad.screenshot, format="PNG")
r = c.post(f"/api/projects/{aon}/steps/crop", headers=H, json={"crop": 60})
now = storage.load(aon).steps[0]
check("有一张裁不了：400，前面那张截图原样没动", r.status_code == 400 and good_file.read_bytes() == good_bytes, r.text[:120])
check("步骤的尺寸和坐标也没动", (now.img_w, now.img_h, now.target.rect.x) == (good.img_w, good.img_h, good.target.rect.x))
check("没留下临时文件", not list(storage.screenshots_dir(aon).glob("*tmp*")),
      sorted(x.name for x in storage.screenshots_dir(aon).iterdir()))
(storage.screenshots_dir(aon) / bad.screenshot).unlink()
r = c.post(f"/api/projects/{aon}/steps/crop", headers=H, json={"crop": 10})
check("截图文件丢了：400，别的照样不动", r.status_code == 400 and bad.screenshot in r.json()["detail"]
      and good_file.read_bytes() == good_bytes, r.text[:120])

print("\n== 3. 图片幻灯片 ==")
r = c.post(f"/api/projects/{pid}/steps/image", headers=H,
           json={"image": "data:image/png;base64," + png_b64(), "title": "背景", "narration": "先说背景。",
                 "slide_text": "第一点\n第二点", "position": 0})
check("插入成功", r.status_code == 200, r.text[:200])
p = storage.load(pid)
sl = p.steps[0]
check("放在最前面，是不缩放、不高亮的幻灯片", sl.kind == "slide" and not sl.zoom and not sl.highlight and sl.index == 0
      and sl.title == "背景" and sl.narration == "先说背景。" and sl.caption == "先说背景。", sl.model_dump(include={"kind", "zoom"}))
check("图片尺寸记下来", (sl.img_w, sl.img_h, sl.viewport_w) == (800, 450, 800))
r = c.post(f"/api/projects/{pid}/steps/image", headers=H, json={"image": "not-an-image"})
check("不是图片：400", r.status_code == 400, r.text[:120])
buf = io.BytesIO()
Image.new("1", (8000, 5001)).save(buf, format="PNG")     # a small file that would unpack into 40 million pixels
r = c.post(f"/api/projects/{pid}/steps/image", headers=H, json={"image": base64.b64encode(buf.getvalue()).decode("ascii")})
check(f"文件不大、像素太多：解码之前就拒绝（{len(buf.getvalue()) // 1024} KB）", r.status_code == 400 and "8000" in r.json()["detail"],
      r.text[:120])
r = c.post(f"/api/projects/{pid}/steps/crop", headers=H, json={"steps": [sl.id], "crop": 10})
check("幻灯片不参与裁边", r.json()["cropped"] == [] and storage.load(pid).steps[0].img_w == 800)
check("上传上限里有图片幻灯片", any(rx.match(f"/api/projects/{pid}/steps/image") for rx, _ in main.UPLOAD_LIMITS))

print("\n== 4. 关闭内置 AI ==")
check("默认开着", not llm.ai_disabled() and c.get("/api/health").json()["ai_disabled"] is False)
c.post("/api/settings", headers=H, json={"ai_disabled": True})
check("设置里能关", c.get("/api/settings").json()["ai_disabled"] is True and c.get("/api/health").json()["ai_disabled"] is True)
try:
    llm.get_client()
    blocked = ""
except llm.LLMError as e:
    blocked = str(e)
check("所有 AI 功能在请求之前就停下", "内置 AI 已关闭" in blocked, blocked)
t = c.post("/api/settings/test-key", headers=H, json={"provider": "deepseek", "api_key": "sk-fake"}).json()
check("测试连接也不发请求", t["ok"] is False and "内置 AI 已关闭" in t["message"], t)
j = wait(c.post(f"/api/projects/{pid}/script", headers=H, json={}).json()["id"])
check("「AI 写解说」任务报错，不出网", j["status"] == "error" and "内置 AI 已关闭" in (j.get("error") or ""), j.get("error"))
c.post("/api/settings", headers=H, json={"ai_disabled": False})
check("能再打开", not llm.ai_disabled())

print("\n== 5. 渲染：不另存字幕文件、不留第二字幕位置 ==")
c.post("/api/settings", headers=H, json={"ai_disabled": True})          # rendering needs no AI
j = wait(c.post(f"/api/projects/{src_pid}/render", headers=H,
                json={"width": 640, "height": 360, "sidecar_subtitles": False, "second_sub_space": False}).json()["id"])
check("渲染成功", j["status"] == "done", j.get("error"))
proj = storage.load(src_pid)
out = storage.output_dir(src_pid)
stem = Path(proj.output).stem
check("视频旁没有 .srt / .vtt", not (out / f"{stem}.srt").exists() and not (out / f"{stem}.vtt").exists(),
      sorted(x.name for x in out.iterdir()))
cues = json.loads((out / f"{stem}{second_subs.CUES_SUFFIX}").read_text(encoding="utf-8"))
check("主字幕时间线照样保存，没留第二字幕位置", cues["cues"] and cues["burned"] and cues["space"] is False)
check("返回结果里也没有 srt", j["result"].get("srt") == "")
r = c.get(f"/api/projects/{src_pid}/file/output/{stem}.srt?download=true")
check("「下载字幕」现场生成 SRT", r.status_code == 200 and "-->" in r.text and "attachment" in r.headers.get("content-disposition", ""),
      r.status_code)
check("生成的临时文件用完就删", not list(storage.work_dir(src_pid).glob("sub_*")))
r = c.get(f"/api/projects/{src_pid}/file/output/{stem}.vtt")
check("VTT 也行", r.status_code == 200 and r.text.startswith("WEBVTT"))
r = c.get(f"/api/projects/{src_pid}/file/output/other.srt")
check("别的文件名照样 404", r.status_code == 404)

print("\n== 6. 第二字幕：外部翻译后导入 ==")
s = c.get(f"/api/projects/{src_pid}/subtitles2/sentences").json()
n = len(s["sentences"])
check("取到句子和时间", n > 0 and all(x["end"] > x["start"] for x in s["sentences"]) and s["key"], n)
r = c.post(f"/api/projects/{src_pid}/subtitles2/en-US/import", headers=H, json={"texts": ["only one"], "key": s["key"]})
check("句数不对：400", r.status_code == 400 and str(n) in r.json()["detail"], r.text[:120])
r = c.post(f"/api/projects/{src_pid}/subtitles2/en-US/import", headers=H, json={"texts": ["x"] * n, "key": "old"})
check("视频重渲染过（key 不对）：400", r.status_code == 400, r.text[:120])
r = c.post(f"/api/projects/{src_pid}/subtitles2/en-US/import", headers=H, json={"texts": ["x"] * n})
check("没带 key（看不出视频是否重渲染过）：400", r.status_code == 400 and "key" in r.json()["detail"], r.text[:120])
r = c.post(f"/api/projects/{src_pid}/subtitles2/zh-CN/import", headers=H, json={"texts": ["x"] * n})
check("和视频同语言：400", r.status_code == 400)
r = c.post(f"/api/projects/{src_pid}/subtitles2/xx-XX/import", headers=H, json={"texts": ["x"] * n})
check("不支持的语言：400", r.status_code == 400)
texts = [f"Sentence {i + 1}." for i in range(n)]
r = c.post(f"/api/projects/{src_pid}/subtitles2/en-US/import", headers=H, json={"texts": texts, "key": s["key"]})
st = r.json()
check("导入成功，轨道没过期", r.status_code == 200 and [(t["lang"], t["stale"]) for t in st["tracks"]] == [("en-US", False)],
      r.text[:200])
vtt = (out / f"{stem}.en-US.vtt").read_text(encoding="utf-8")
check("VTT 里是导入的译文", vtt.startswith("WEBVTT") and "Sentence 1." in vtt and f"Sentence {n}." in vtt)
check("编辑器预览也能读到", len(c.get(f"/api/projects/{src_pid}/subtitles2/en-US").json()["cues"]) >= n)
c.post("/api/settings", headers=H, json={"ai_disabled": False})

print("\n== 7. 拼装的项目能直接渲染 ==")
c.post("/api/settings", headers=H, json={"ai_disabled": True})
j = wait(c.post(f"/api/projects/{pid}/render", headers=H, json={"width": 640, "height": 360}).json()["id"])
check("没有配音也能渲染（按最短时长）", j["status"] == "done", j.get("error"))

print(f"\n结果: {'全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'}")
