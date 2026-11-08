"""HTML decks in a headless browser (Edge / Chrome): reading different kinds of decks, the still of each slide without the presenter's
chrome, the requests a deck may make, recording slides with their clicks, reusing recordings, the editor's API and a browser that
can't be used. Synthetic decks only (web_fixtures.py); skipped on a computer without Edge or Chrome. No AI and no voice-over calls."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "web_deck_data"
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
config.save({"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural"})
from PIL import Image  # noqa: E402

from backend import storage  # noqa: E402
from backend.models import SlidesCreateReq  # noqa: E402
from backend.services import cdp, clips, slides, video, webdeck  # noqa: E402
import web_fixtures as fx  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


if cdp.find_browser() is None:
    print("这台电脑没有 Edge / Chrome，跳过")
    print("全部通过")
    sys.exit(0)

UP = DATA / "up"
UP.mkdir()


def analyze(name: str, content) -> dict:
    p = UP / name
    if isinstance(content, Path):
        shutil.copyfile(content, p)
    else:
        p.write_text(content, encoding="utf-8")
    return slides.analyze(p, name)


def color_near(px, rgb, tol=12):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], rgb))


def count_color(img: Image.Image, rgb, tol=30) -> int:
    raw = img.convert("RGB").resize((img.width // 4, img.height // 4)).tobytes()
    return sum(1 for k in range(0, len(raw), 3) if color_near(raw[k:k + 3], rgb, tol))


print("== 1. 常见的 AI 演示稿（section.slide + data-notes + #slide-N） ==")
t0 = time.time()
m1 = analyze("demo.html", fx.custom_deck())
print(f"  （识别用了 {time.time() - t0:.1f} 秒）")
check("4 页，按网页地址翻页", m1["count"] == 4 and m1["web"]["nav"] == "hash" and m1["web"]["kind"] == "generic", m1.get("web"))
check("标题来自每页的大标题", [s["title"] for s in m1["slides"]] == ["示例产品", "三种用法", "动手看看", "认识界面"],
      [s["title"] for s in m1["slides"]])
check("讲稿来自 data-notes", all(s["notes"] for s in m1["slides"]) and m1["with_notes"] == 4)
check("两页有操作提示", m1["web"]["cues"] == 2, m1["web"]["cues"])
check("每页的按钮（页外的翻页箭头、圆点、顶栏不算）", [len(s["web"]["controls"]) for s in m1["slides"]] == [0, 3, 1, 3],
      [len(s["web"]["controls"]) for s in m1["slides"]])
d1 = slides.import_dir(m1["id"])
with Image.open(d1 / "slide_004.png") as im:
    still = im.convert("RGB")
check("静态图 1920×1080（只截舞台）", still.size == (1920, 1080), still.size)
check("舞台的圆角去掉了：角上是幻灯片底色", color_near(still.getpixel((1, 1)), (247, 248, 251)), still.getpixel((1, 1)))
check("压在舞台上的翻页圆点藏起来了", all(color_near(still.getpixel((x, 1077)), (247, 248, 251)) for x in range(900, 1020, 6)))
check("入场动画已经播完（标题看得见）", count_color(still.crop((0, 0, 960, 200)), (20, 33, 61), 40) > 50)

print("\n== 2. 用演示框架接口的演示稿（Reveal 式：aside.notes、分步出现） ==")
m2 = analyze("reveal.html", fx.reveal_like())
check("3 页，用框架的接口翻页", m2["count"] == 3 and m2["web"]["kind"] == "reveal" and m2["web"]["nav"] == "api", m2.get("web"))
check("讲稿来自 aside.notes", m2["slides"][1]["notes"].startswith("Here are the points"), m2["slides"][1]["notes"][:40])
check("第 2 页分两步出现，记下了每步的文字", m2["slides"][1]["web"]["builds"] == 2
      and m2["slides"][1]["web"]["build_texts"] == ["Alpha arrives first", "Beta follows next"], m2["slides"][1]["web"])
check("静态图和页面文字是整页（分步的内容都在）", "Alpha arrives first" in m2["slides"][1]["text"]
      and "Beta follows next" in m2["slides"][1]["text"], m2["slides"][1]["text"])
with Image.open(slides.import_dir(m2["id"]) / "slide_001.png") as im:
    r2 = im.convert("RGB")
check("框架自己的控制按钮不在画面里", count_color(r2.crop((1700, 950, 1920, 1080)), (238, 51, 51), 25) == 0)

print("\n== 3. 只能按键翻页、还有分步出现的演示稿 ==")
m3 = analyze("keys.html", fx.keys_only())
check("3 页，按键翻页", m3["count"] == 3 and m3["web"]["nav"] == "key", m3.get("web"))
check("第 2 页按 → 先出两部分", [s["web"]["builds"] for s in m3["slides"]] == [0, 2, 0], [s["web"]["builds"] for s in m3["slides"]])
check("分步之后的那页也找得到（多按的两下算进去）", [s["title"] for s in m3["slides"]] == ["One", "Two", "Three"],
      [s["title"] for s in m3["slides"]])
check("第 2 页的静态图是整页（两部分都显示了）", "Part A" in m3["slides"][1]["text"] and "Part B" in m3["slides"][1]["text"],
      m3["slides"][1]["text"])
check("讲稿可以藏在页里的 .notes", m3["slides"][0]["notes"] == "First page notes.", m3["slides"][0]["notes"])

print("\n== 4. 一页一页往下滚的长网页 ==")
m4 = analyze("scroll.html", fx.scroll_deck())
check("3 页，滚动", m4["count"] == 3 and m4["web"]["nav"] == "scroll", m4.get("web"))
check("每页的讲稿", [s["notes"] for s in m4["slides"]] == ["Scroll one.", "Scroll two.", "Scroll three."])
check("第 3 页的静态图是第 3 屏", m4["slides"][2]["title"] == "Third screen")

print("\n== 5. 一页一个文件的 .zip（图片在子文件夹里） ==")
m5 = analyze("pages.zip", fx.pages_zip(DATA / "zip"))
check("3 页，按文件名的自然顺序（1、2、10）", [s["title"] for s in m5["slides"]] == ["Page 1", "Page 2", "Page 10"],
      [s["title"] for s in m5["slides"]])
check("没有缺文件", not m5["web"]["missing"], m5["web"]["missing"])
with Image.open(slides.import_dir(m5["id"]) / "slide_002.png") as im:
    p2 = im.convert("RGB")
check("只截 1280×720 的那块（16:9），不是整个窗口", abs(p2.width / p2.height - 16 / 9) < 0.02, p2.size)
check("子文件夹里的图片显示出来了", count_color(p2, (40, 160, 90), 20) > 100)

print("\n== 6. 网页发出的请求 ==")
hits = []


class Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Probe)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
probe_deck = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<section class="slide" data-notes="One." style="width:1280px;height:720px"><h1>One</h1><img src="missing/pic.png">
<img src="http://127.0.0.1:{port}/img.png"></section>
<section class="slide" data-notes="Two." style="display:none;width:1280px;height:720px"><h1>Two</h1></section>
<script>fetch('http://127.0.0.1:{port}/api').catch(() => 0); fetch('http://localhost:{port}/x').catch(() => 0);</script>
</body></html>"""
m6 = analyze("probe.html", probe_deck)
srv.shutdown()
check("本机的服务一个请求也收不到（网页碰不到 StepCast 和其他本机服务）", hits == [], hits)
check("没一起上传的文件记下来了", "missing/pic.png" in m6["web"]["missing"], m6["web"]["missing"])

print("\n== 7. 建项目、录制（带点击）、渲染 ==")
res = slides.create_project(m1["id"], SlidesCreateReq(name="web", notes_mode="verbatim", language="zh-CN", auto_voice=False,
                                                       web_mode="demo", web_cursor=True))
proj = storage.load(res["project_id"])
check("项目里带着演示稿的文件", (webdeck.web_dir(proj.id) / "deck.json").exists() and (webdeck.web_dir(proj.id) / "site").is_dir())
check("念的解说里没有操作提示", all("（点" not in s.narration and "（依次" not in s.narration for s in proj.steps),
      [s.narration for s in proj.steps])
acts = [[(a.kind, a.control) for a in s.web.actions] for s in proj.steps]
check("按讲稿安排的点击：第 2 页两种模式被说到，第 3 页的按钮，第 4 页依次三个热点",
      acts == [[], [("click", 0), ("click", 1), ("click", 2)], [("click", 0)], [("click", 0), ("click", 1), ("click", 2)]], acts)
check("鼠标指针的设置跟着项目", proj.settings.get("web_cursor") is True)
small = {"video_width": 640, "video_height": 360, "video_fps": 24, "intro_enabled": False, "outro_enabled": False}
t0 = time.time()
out = video.render_project(proj, overrides=small)
first_render = time.time() - t0
storage.save(proj)
print(f"  （第一次渲染 {first_render:.1f} 秒）")
check("渲染完成，没有提醒", (storage.output_dir(proj.id) / out["file"]).exists() and not out["warning"], out["warning"])
check("每页都录了一段", all(s.web_clip is not None and s.web_clip.file for s in proj.steps), [s.web_clip for s in proj.steps])
clip3 = clips.media_dir(proj.id) / proj.steps[2].web_clip.file
info = clips.probe(clip3)
f_start = clips.grab_frame(clip3, 0.25, info["width"], info["height"])
f_end = clips.grab_frame(clip3, max(0.0, proj.steps[2].web_clip.length - 0.2), info["width"], info["height"])
check("第 3 页：点按钮之前没有进度条，之后进度条满了", count_color(f_start, (228, 87, 46)) == 0 and count_color(f_end, (228, 87, 46)) > 80,
      (count_color(f_start, (228, 87, 46)), count_color(f_end, (228, 87, 46))))
check("第 3 页的入场动画是录下来的（开头标题还淡）", count_color(f_start, (20, 33, 61), 40) < count_color(f_end, (20, 33, 61), 40))
clip4 = clips.media_dir(proj.id) / proj.steps[3].web_clip.file
i4 = clips.probe(clip4)
end4 = clips.grab_frame(clip4, max(0.0, proj.steps[3].web_clip.length - 0.2), i4["width"], i4["height"])
check("第 4 页：最后一个点的热点变成了蓝色", count_color(end4, (47, 107, 255), 40) > 3, count_color(end4, (47, 107, 255), 40))
mtimes = {s.id: (clips.media_dir(proj.id) / s.web_clip.file).stat().st_mtime for s in proj.steps}
t0 = time.time()
video.render_project(proj, overrides=small)
again = time.time() - t0
check("什么都没改：录好的直接用，不再录", all((clips.media_dir(proj.id) / s.web_clip.file).stat().st_mtime == mtimes[s.id]
                                   for s in proj.steps) and again < first_render, (round(again, 1), round(first_render, 1)))
proj.steps[1].narration = "它有三种用法。先说精确模式，结果更严谨。"
video.render_project(proj, overrides=small)
check("改了第 2 页的解说：只重录这一页，点击跟着新解说", [(a.kind, a.control) for a in proj.steps[1].web.actions] == [("click", 1)]
      and all((clips.media_dir(proj.id) / s.web_clip.file).stat().st_mtime == mtimes[s.id] for s in proj.steps[2:]),
      [(a.kind, a.control) for a in proj.steps[1].web.actions])
check("每页最多留几段旧录制", all(len(list(clips.media_dir(proj.id).glob(f"web_{s.id}_*.mp4"))) <= video.WEB_KEEP for s in proj.steps))
storage.save(proj)

print("\n== 8. 编辑器用的接口 ==")
from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
check("健康检查里有浏览器", c.get("/api/health").json()["web_browser"] in ("Edge", "Chrome", "Chromium"))
pd = c.get(f"/api/projects/{proj.id}").json()
st3 = pd["steps"][3]
check("项目数据里有解说的句子（选「讲到哪一句」用）", [u["text"][:2] for u in st3["web"]["units"]][:2] == ["界面", "第一"]
      and st3["web"]["units_for"] == proj.steps[3].narration, st3["web"]["units"][:2])
sid = st3["id"]
r = c.put(f"/api/projects/{proj.id}/steps/{sid}/web", headers=H,
          json={"actions": [{"kind": "click", "control": 2, "unit": 1}, {"kind": "hover", "control": 0, "unit": 9}]})
w = r.json()["steps"][3]["web"]
check("手动设置：保存下来，按句子定位，「讲完以后」在最后", r.status_code == 200 and w["manual"]
      and [(a["kind"], a["control"]) for a in w["actions"]] == [("click", 2), ("hover", 0)]
      and w["actions"][1]["pos"] == len(proj.steps[3].narration), w["actions"])
check("动作种类不对：400", c.put(f"/api/projects/{proj.id}/steps/{sid}/web", headers=H,
                          json={"actions": [{"kind": "drag", "control": 0, "unit": 0}]}).status_code == 400)
check("没有这个按钮：400", c.put(f"/api/projects/{proj.id}/steps/{sid}/web", headers=H,
                         json={"actions": [{"kind": "click", "control": 99, "unit": 0}]}).status_code == 400)
w = c.put(f"/api/projects/{proj.id}/steps/{sid}/web", headers=H, json={"auto": True}).json()["steps"][3]["web"]
check("按讲稿重新安排：回到自动", not w["manual"] and [a["control"] for a in w["actions"]] == [0, 1, 2], w["actions"])
w = c.put(f"/api/projects/{proj.id}/steps/{sid}/web", headers=H, json={"live": False}).json()["steps"][3]["web"]
check("关掉录制：静态画面", w["live"] is False)
check("不是网页的步骤：404", c.put(f"/api/projects/{proj.id}/steps/s_nope/web", headers=H, json={"live": True}).status_code == 404)
up = c.post("/api/import/slides", headers=H, files={"file": ("other.html", fx.scroll_deck().encode("utf-8"), "text/html")})
check("上传接口收 .html", up.status_code == 200, up.text[:120])
job = up.json() if up.status_code == 200 else {}
for _ in range(240):                     # the import runs as a job: wait for it (its browser must be closed before the end)
    if job.get("status") not in ("pending", "running"):
        break
    time.sleep(0.5)
    job = c.get(f"/api/jobs/{job['id']}").json()
check("上传后的识别任务做完了", job.get("status") == "done" and (job.get("result") or {}).get("count") == 3,
      {k: job.get(k) for k in ("status", "message", "error")})
check("不收别的格式", c.post("/api/import/slides", headers=H, files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400)

print("\n== 9. 各种写法的网页 ==")
(UP / "gbk.html").write_bytes(fx.gbk_deck())
m9 = slides.analyze(UP / "gbk.html", "gbk.html")
check("GBK 编码、没写 charset：标题不乱码", [s["title"] for s in m9["slides"]] == ["第一页标题", "第二页标题"],
      [s["title"] for s in m9["slides"]])
check("讲稿写在页里的元素上（data-notes）", m9["slides"][0]["notes"] == "第一页的讲稿写在里面的元素上。", m9["slides"][0]["notes"])
check("讲稿写在页里的注释里（短注释不算）", m9["slides"][1]["notes"].startswith("这是第二页写在注释里的讲稿"), m9["slides"][1]["notes"])
m10 = analyze("app.zip", fx.app_zip(DATA / "app"))
with Image.open(slides.import_dir(m10["id"]) / "slide_001.png") as im:
    a1 = im.convert("RGB")
check("压缩包外面有一层文件夹：/assets/… 的样式照样找到", count_color(a1, (200, 30, 30), 30) > 200 and not m10["web"]["missing"],
      (count_color(a1, (200, 30, 30), 30), m10["web"]["missing"]))
m11 = analyze("video.zip", fx.video_zip(DATA / "video"))
r11 = slides.create_project(m11["id"], SlidesCreateReq(name="video", notes_mode="verbatim", language="en-US", auto_voice=False))
pv = storage.load(r11["project_id"])
video.render_project(pv, overrides=small)
storage.save(pv)
cv = clips.media_dir(pv.id) / pv.steps[0].web_clip.file
iv = clips.probe(cv)
fa = clips.grab_frame(cv, 0.6, iv["width"], iv["height"])
fb = clips.grab_frame(cv, 1.8, iv["width"], iv["height"])
check("页里的视频在录制里是播放着的（前后两帧不一样）", fa is not None and fb is not None and fa.tobytes() != fb.tobytes())

print("\n== 10. 打码也作用到录制的画面 ==")
from backend.models import Redaction  # noqa: E402
from backend.services.renderer import StepRenderer, Theme  # noqa: E402

s3 = proj.steps[2]
s3.redactions = [Redaction(x=0, y=0, w=s3.viewport_w // 2, h=s3.viewport_h // 2, mode="solid")]
theme = Theme.from_config(video.effective_config(proj, small))
rend = StepRenderer(step=s3, screenshot_path=storage.screenshots_dir(proj.id) / s3.screenshot, theme=theme)
raw = clips.grab_frame(clips.media_dir(proj.id) / s3.web_clip.file, 0.2, 400, 225)
red = video._web_redact(rend, raw)
corner = red.crop((20, 20, 180, 92)).convert("L")             # (inside: the solid box has rounded corners)
check("录制画面里打码的那块是一整块颜色", corner.getextrema()[1] - corner.getextrema()[0] <= 2, corner.getextrema())
check("没打码的地方不变", red.crop((210, 120, 400, 225)).tobytes() == raw.crop((210, 120, 400, 225)).tobytes())
s3.redactions = []

print("\n== 11. 浏览器用不了的时候 ==")
fake = DATA / "fake_browser.cmd"
fake.write_text("@echo DevTools remote debugging is disallowed by the system admin. 1>&2\r\n@exit /b 1\r\n", encoding="ascii")
try:
    cdp.run(cdp.Browser(fake).start())
    blocked = None
except cdp.PolicyBlocked as e:
    blocked = str(e)
except cdp.CDPError as e:
    blocked = "other: " + str(e)
check("公司策略禁止远程调试：说清楚原因", blocked is not None and "策略" in blocked, blocked)
broken = DATA / "broken_browser.cmd"
broken.write_text("@echo cannot start: profile locked 1>&2\r\n@exit /b 3\r\n", encoding="ascii")
try:
    cdp.run(cdp.Browser(broken).start())
    err = ""
except cdp.CDPError as e:
    err = str(e)
check("别的启动失败：带上浏览器的报错", "profile locked" in err and "exit 3" in err, err)
if os.name == "nt":
    # StepCast ended the hard way (killed, crashed) while a job had a browser open: Windows ends the browser too
    code = ("import asyncio, sys\nsys.path.insert(0, %r)\nfrom backend.services import cdp\n"
            "async def main():\n    b = cdp.Browser()\n    await b.start()\n    print(b.profile, flush=True)\n"
            "    await asyncio.sleep(120)\ncdp.run(main())\n") % str(ROOT)
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
    profile = (child.stdout.readline() or "").strip()
    child.kill()
    child.wait(timeout=10)
    alive = "?"
    for _ in range(20):
        alive = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
                                f"Where-Object {{ $_.CommandLine -like '*{Path(profile).name}*' }}).Count"],
                               capture_output=True, text=True).stdout.strip() if profile else "?"
        if alive == "0":
            break
        time.sleep(0.5)
    check("StepCast 被强行结束：它开的后台浏览器也跟着结束", bool(profile) and alive == "0", (profile, alive))
    if profile:
        shutil.rmtree(profile, ignore_errors=True)

print("\n== 12. 演示工具的各种做法 ==")
m13 = analyze("shower.html", fx.shower_like())
check("先列出全部幻灯片的演示稿：进入一页一页的模式，用它的接口翻页",
      m13["count"] == 3 and m13["web"]["kind"] == "shower" and m13["web"]["nav"] == "api", m13.get("web"))
check("页内分步（.next）记下来了，静态图里都显示",
      [s["web"]["builds"] for s in m13["slides"]] == [0, 2, 0] and "Step two" in m13["slides"][1]["text"],
      [(s["web"]["builds"], s["text"]) for s in m13["slides"]])
with Image.open(slides.import_dir(m13["id"]) / "slide_001.png") as im:
    sh = im.convert("RGB")
check("是整页，不是缩略图的列表；0 宽的进度条也藏起来了",
      sh.size == (1920, 1080) and count_color(sh.crop((0, 1040, 1920, 1080)), (238, 51, 51), 25) == 0
      and color_near(sh.getpixel((1900, 20)), (255, 255, 255)), (sh.size, sh.getpixel((1900, 20))))
m14 = analyze("bits.html", fx.framework_bits())
check("用浏览器的翻页过渡（View Transitions）：照样一页一页找到，没有多算分步",
      [s["title"] for s in m14["slides"]] == ["Mixed Case Title", "Second Page", "Third Page"]
      and [s["web"]["builds"] for s in m14["slides"]] == [0, 0, 0] and m14["web"]["nav"] == "key",
      ([s["title"] for s in m14["slides"]], [s["web"]["builds"] for s in m14["slides"]], m14.get("web")))
check("标题按原文（CSS 显示成大写也一样）", m14["slides"][0]["title"] == "Mixed Case Title", m14["slides"][0]["title"])
check("藏在自己类名的盒子里的备注（…-slide-notes）",
      [s["notes"] for s in m14["slides"]] == ["Notes kept in a hidden box of their own.", "Second page notes.", ""],
      [s["notes"] for s in m14["slides"]])

m15 = analyze("track.html", fx.sliding_track())
check("滑动翻页的演示稿：3 页按键翻", m15["count"] == 3 and m15["web"]["nav"] == "key"
      and [s["title"] for s in m15["slides"]] == ["Red", "Green", "Blue"], ([s["title"] for s in m15["slides"]], m15.get("web")))
with Image.open(slides.import_dir(m15["id"]) / "slide_002.png") as im:
    tr2 = im.convert("RGB")
check("带 transition: all 的页码圆点也藏起来了", count_color(tr2, (255, 221, 0), 30) == 0 and count_color(tr2, (51, 170, 51), 20) > 1000,
      count_color(tr2, (255, 221, 0), 30))
r15 = slides.create_project(m15["id"], SlidesCreateReq(name="track", notes_mode="verbatim", language="en-US", auto_voice=False))
p15 = storage.load(r15["project_id"])
video.render_project(p15, overrides=small)
storage.save(p15)
c15 = clips.media_dir(p15.id) / p15.steps[1].web_clip.file
i15 = clips.probe(c15)
g0 = clips.grab_frame(c15, 0.05, i15["width"], i15["height"])
check("录制一开始就是这一页（上一页滑走的过程不录进来）",
      count_color(g0, (204, 51, 51), 30) == 0 and count_color(g0, (51, 170, 51), 30) > 500,
      (count_color(g0, (204, 51, 51), 30), count_color(g0, (51, 170, 51), 30)))

print("\n== 13. 只录动画：不点按钮，分步出现的内容照样跟着解说出来 ==")
ra = slides.create_project(m1["id"], SlidesCreateReq(name="anim", notes_mode="verbatim", language="zh-CN", auto_voice=False,
                                                      web_mode="animate"))
pa = storage.load(ra["project_id"])
check("备注里有操作提示，也不安排点击（仍是自动安排）",
      all(not s.web.actions and s.web.clicks is False and not s.web.manual for s in pa.steps),
      [[a.kind for a in s.web.actions] for s in pa.steps])
w = c.put(f"/api/projects/{pa.id}/steps/{pa.steps[2].id}/web", headers=H, json={"auto": True}).json()["steps"][2]["web"]
check("按讲稿重新安排：点击加回来", w["clicks"] is True and [a["kind"] for a in w["actions"]] == ["click"], w["actions"])
rb = slides.create_project(m2["id"], SlidesCreateReq(name="anim2", notes_mode="verbatim", language="en-US", auto_voice=False,
                                                      web_mode="animate"))
pb = storage.load(rb["project_id"])
check("分步出现的两部分照样安排", [(a.kind, a.control) for a in pb.steps[1].web.actions] == [("next", -1), ("next", -1)],
      [(a.kind, a.control, a.pos) for a in pb.steps[1].web.actions])
video.render_project(pb, overrides=small)
storage.save(pb)
cb = clips.media_dir(pb.id) / pb.steps[1].web_clip.file
ib = clips.probe(cb)
b0 = clips.grab_frame(cb, 0.2, ib["width"], ib["height"])
bm = clips.grab_frame(cb, pb.steps[1].web_clip.length / 2, ib["width"], ib["height"])
b1 = clips.grab_frame(cb, max(0.0, pb.steps[1].web_clip.length - 0.2), ib["width"], ib["height"])


def bright(img):                         # light text on the dark slide
    return sum(1 for v in img.convert("L").tobytes() if v > 128)


n0, nm, n1 = bright(b0), bright(bm), bright(b1)
check("录制开头分步的内容还没出现，之后一条一条出来", n0 + 60 < nm < n1 - 60, (n0, nm, n1))

if os.name == "nt":
    left = ""
    for _ in range(10):                  # a helper process may still be on its way out
        left = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
                               "Where-Object { $_.CommandLine -like '*stepcast-browser-*' }).Count"],
                              capture_output=True, text=True).stdout.strip()
        if left == "0":
            break
        time.sleep(1)
    check("测试用过的后台浏览器都关掉了", left == "0", left)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
