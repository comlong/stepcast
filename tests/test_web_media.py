"""HTML decks, part 2: what moves or loads outside the page's scripts (SVG animations, GIFs, frames, dialogs, late pictures), decks whose
slides can't be read from the page (a canvas, a viewer around files, a web app), notes kept in the script, covers, decks that turn their
own pages, and a browser that doesn't answer when it starts. Synthetic decks only (web_fixtures.py); skipped without Edge or Chrome."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "web_media_data"
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
config.save({"ui_language": "zh", "language": "en-US", "voice": "en-US-AriaNeural", "ai_disabled": True})
from PIL import Image  # noqa: E402

from backend import storage  # noqa: E402
from backend.models import SlidesCreateReq  # noqa: E402
from backend.services import cdp, clips, slides, video  # noqa: E402
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
SMALL = {"video_width": 640, "video_height": 360, "video_fps": 24, "intro_enabled": False, "outro_enabled": False}


def analyze(name, content):
    p = UP / name
    if isinstance(content, Path):
        shutil.copyfile(content, p)
    else:
        p.write_text(content, encoding="utf-8")
    return slides.analyze(p, name)


def project(m, mode="demo"):
    r = slides.create_project(m["id"], SlidesCreateReq(name="t", notes_mode="verbatim", missing="empty", language="en-US", auto_voice=False,
                                                       web_mode=mode))
    return storage.load(r["project_id"])


def render(p):
    out = video.render_project(p, overrides=SMALL)
    storage.save(p)
    return out


def frames_of(p, step, times):
    s = p.steps[step]
    f = clips.media_dir(p.id) / s.web_clip.file
    i = clips.probe(f)
    return [clips.grab_frame(f, t, i["width"], i["height"]).convert("RGB") for t in times]


def near(px, rgb, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], rgb))


def centroid(im, rgb):
    xs = [x for x in range(0, im.width, 4) for y in range(0, im.height, 4) if near(im.getpixel((x, y)), rgb, 50)]
    return sum(xs) / len(xs) / im.width if xs else -1


def still(m, n):
    with Image.open(slides.import_dir(m["id"]) / f"slide_{n:03d}.png") as im:
        return im.convert("RGB")


print("== 1. SVG 自己的动画（<animate>）跟着录制的时间走 ==")
m = analyze("smil.html", fx.smil_deck())
p = project(m)
render(p)
a = frames_of(p, 0, (0.1, 1.0, 2.0, 3.0))
got = [round(centroid(x, (224, 48, 48)), 3) for x in a]
want = [(100 + 425 * t) / 1920 for t in (0.1, 1.0, 2.0, 3.0)]
check("红点按 4 秒走完的速度移动", all(abs(g - w) < 0.03 for g, w in zip(got, want)), (got, [round(w, 3) for w in want]))

print("\n== 2. GIF 动图一帧帧跟着录制的时间走 ==")
m = analyze("gif.zip", fx.gif_zip(DATA / "gif"))
p = project(m)
render(p)
a = frames_of(p, 0, (0.25, 0.75, 1.25, 1.75, 2.25))
cols = [x.getpixel((x.width // 2, x.height // 2)) for x in a]
want = [(220, 40, 40), (40, 180, 70), (50, 80, 220), (240, 200, 30), (220, 40, 40)]
check("每半秒换一种颜色，循环播放", all(near(c, w) for c, w in zip(cols, want)), cols)

print("\n== 3. 页面弹窗不会把页面卡住 ==")
t0 = time.time()
m = analyze("dialog.html", fx.dialog_deck())
check("有 alert() 和“离开此页”的网页照常导入", m["count"] == 2 and time.time() - t0 < 40, round(time.time() - t0, 1))
p = project(m)
render(p)
a = frames_of(p, 0, (0.2, p.steps[0].web_clip.length - 0.2))
check("按钮里的 confirm / prompt 走通了", not near(a[0].getpixel((a[0].width // 4, a[0].height // 2)), (32, 176, 64))
      and near(a[1].getpixel((a[1].width // 4, a[1].height // 2)), (32, 176, 64)),
      (a[0].getpixel((a[0].width // 4, a[0].height // 2)), a[1].getpixel((a[1].width // 4, a[1].height // 2))))

print("\n== 4. 页里的 <iframe> 跟着录制的时间走 ==")
m = analyze("frame.zip", fx.frame_zip(DATA / "frame"))
p = project(m)
render(p)
a = frames_of(p, 0, (0.2, 1.5, 2.4))
bar = lambda im: sum(1 for x in range(0, im.width, 4) if near(im.getpixel((x, im.height // 4)), (34, 170, 119), 50)) * 4 / im.width  # noqa: E731
red = lambda im: near(im.getpixel((im.width // 2, im.height * 3 // 4)), (224, 48, 48), 50)                                            # noqa: E731
check("frame 里的动画在长（CSS）", bar(a[0]) < 0.3 and 0.5 < bar(a[1]) < 0.95 and bar(a[2]) > 0.95, [round(bar(x), 2) for x in a])
check("frame 里的定时器按时触发", not red(a[0]) and red(a[1]), [red(x) for x in a])

print("\n== 5. 外壳页面 + 每页一个文件（iframe 里换页） ==")
m = analyze("viewer.zip", fx.viewer_zip(DATA / "viewer"))
check("按画面数出 3 页", m["count"] == 3 and m["web"]["kind"] == "frames" and m["web"]["nav"] == "key", m.get("web"))
check("标题和备注从 frame 里读", [s["title"] for s in m["slides"]] == ["Page 1", "Page 2", "Page 3"]
      and m["slides"][1]["notes"].endswith("Notes of page 2."), ([s["title"] for s in m["slides"]], m["slides"][1]["notes"][-20:]))
check("frame 里的按钮认得出", [c.get("text") for c in m["slides"][1]["web"]["controls"]] == ["Run"], m["slides"][1]["web"]["controls"])
p = project(m)
render(p)
a = frames_of(p, 1, (0.2, p.steps[1].web_clip.length - 0.2))
check("frame 里的按钮点得到", not near(a[0].getpixel((a[0].width // 4, a[0].height // 2)), (32, 176, 64))
      and near(a[1].getpixel((a[1].width // 4, a[1].height // 2)), (32, 176, 64)),
      (a[0].getpixel((a[0].width // 4, a[0].height // 2)), a[1].getpixel((a[1].width // 4, a[1].height // 2))))

print("\n== 6. 讲稿只写在脚本里 ==")
m = analyze("list.html", fx.script_notes_list())
check("函数内部的文字数组（每页一条）", [s["notes"] for s in m["slides"]] == ["Notes of the first slide.", 'Notes of the second slide, with a "quote" in it.'],
      [s["notes"] for s in m["slides"]])
m = analyze("objects.html", fx.script_notes_objects())
check("幻灯片对象列表的 notes 字段（一个元素换内容的稿子）", m["count"] == 3 and m["slides"][2]["notes"] == "Notes of the gamma page."
      and m["slides"][2]["title"] == "Gamma page", (m["count"], [s["notes"] for s in m["slides"]]))

print("\n== 7. 读不出页的演示稿：按画面分页 ==")
m = analyze("canvas.html", fx.canvas_deck())
check("画在 canvas 上、按 → 翻页：3 页", m["count"] == 3 and m["web"]["nav"] == "key", m.get("web"))
check("每页的静态图不同", near(still(m, 2).getpixel((960, 540)), (51, 170, 51)) and near(still(m, 3).getpixel((960, 540)), (51, 51, 204)),
      still(m, 2).getpixel((960, 540)))
m = analyze("click.html", fx.canvas_deck("click"))
check("点一下翻页的 canvas：3 页", m["count"] == 3 and m["web"]["nav"] == "clickpage", m.get("web"))
check("第 3 页的静态图是第 3 页", near(still(m, 3).getpixel((960, 540)), (51, 51, 204)), still(m, 3).getpixel((960, 540)))
m = analyze("moving.html", fx.moving_canvas_deck())
check("自己在动的页面：动的部分不算新的一页", m["count"] == 2, m["count"])
m = analyze("spa.zip", fx.spa_zip(DATA / "spa"))
check("单页应用（真实路径的路由）：从根地址打开，3 页", [s["title"] for s in m["slides"]] == ["One", "Two", "Three"],
      [s["title"] for s in m["slides"]])

print("\n== 8. 开始封面、懒加载图片 ==")
m = analyze("cover.html", fx.cover_deck())
deck = json.loads((slides.import_dir(m["id"]) / "deck" / "deck.json").read_text(encoding="utf-8"))
check("封面的开始按钮按过了，后面的幻灯片读得到", m["count"] == 2 and bool(deck.get("start")) and [s["title"] for s in m["slides"]] == ["Slide one", "Slide two"],
      (m["count"], deck.get("start"), [s["title"] for s in m["slides"]]))
check("静态图里没有封面", not near(still(m, 2).getpixel((960, 800)), (18, 52, 86), 20), still(m, 2).getpixel((960, 800)))
t0 = time.time()
m = analyze("lazy.zip", fx.lazy_zip(DATA / "lazy"))
check("懒加载的图片显示出来了，也没有白等", near(still(m, 2).getpixel((300, 300)), (40, 160, 90)) and time.time() - t0 < 25,
      (still(m, 2).getpixel((300, 300)), round(time.time() - t0, 1)))

print("\n== 9. 按键翻到的页：录制从这一页开始算时间 ==")
m = analyze("keys.html", fx.keys_anim_deck())
p = project(m)
render(p)
a = frames_of(p, 1, (0.1, 1.5))
top = lambda im: sum(1 for x in range(0, im.width, 4) if near(im.getpixel((x, im.height // 12)), (34, 170, 119), 50)) * 4 / im.width  # noqa: E731
check("一开始就动（没有冻住一段）", top(a[0]) < 0.15 and 0.35 < top(a[1]) < 0.65, [round(top(x), 2) for x in a])

print("\n== 10. 自己翻页的演示稿 ==")
m = analyze("auto.html", fx.auto_turn_deck())
p = project(m)
out = render(p)
check("录制时翻走的页改用静态画面，并说明原因", "自己翻走了" in out["warning"] and p.steps[0].web_clip is None
      and (storage.output_dir(p.id) / out["file"]).exists(), out["warning"])

print("\n== 11. 4:3 的 remark 式幻灯片 ==")
m = analyze("remark.html", fx.remark_like())
s = still(m, 1)
check("只截 4:3 的幻灯片，不带两侧", abs(s.width / s.height - 4 / 3) < 0.02 and near(s.getpixel((3, 500)), (255, 255, 255), 8), (s.size, s.getpixel((3, 500))))

print("\n== 12. 刚启动的浏览器不响应：换一个新的 ==")
orig = cdp.Browser._window
calls = []


async def flaky(self, timeout):
    calls.append(str(self.profile))
    if len(calls) == 1:
        raise cdp.CDPError("no answer")
    return await orig(self, timeout)


cdp.Browser._window = flaky


async def use():
    b = cdp.Browser()
    await b.start()
    try:
        s = await b.new_page()
        return await s.evaluate("1 + 1")
    finally:
        await b.close()


try:
    value = cdp.run(use())
finally:
    cdp.Browser._window = orig
check("第二个浏览器接上了", value == 2 and len(calls) == 2 and calls[0] != calls[1], (value, len(calls)))
check("第一个的临时文件夹清掉了", not Path(calls[0]).exists(), calls[0])

print("\n== 13. 从网页结构读出内容块，讲到哪亮到哪 ==")
config.save({"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural", "ai_disabled": True})
m = analyze("cards.html", fx.cards_deck())
keys = [b["key"] for b in m["slides"][0]["web"]["blocks"]]
check("卡片读出来了，页面大标题不算", keys == ["扳手", "锤子", "卷尺"], keys)
check("列表项也读出来了", [b["key"] for b in m["slides"][1]["web"]["blocks"]] == ["先备份", "再发布"], m["slides"][1]["web"]["blocks"])
r = slides.create_project(m["id"], SlidesCreateReq(name="t", notes_mode="verbatim", missing="empty", language="zh-CN", auto_voice=False))
p = storage.load(r["project_id"])
s0 = p.steps[0]
check("提到的三张卡片都安排了高亮", [(a.kind, s0.web.blocks[a.control].key) for a in s0.web.actions] == [("focus", "扳手"), ("focus", "锤子"), ("focus", "卷尺")],
      [(a.kind, a.control) for a in s0.web.actions])
s1 = p.steps[1]
check("有点击的那句不加高亮，别的句子照常", [a.kind for a in s1.web.actions] == ["focus", "click"], [(a.kind, a.pos) for a in s1.web.actions])
out = render(p)
check("渲染没有提醒", not out["warning"], out["warning"])
length = video.step_duration(s0, video.effective_config(p, SMALL))
from backend.services import web_actions  # noqa: E402

tl = web_actions.timeline(s0, video.speech_lead(s0), length)
f0 = clips.media_dir(p.id) / s0.web_clip.file
i0 = clips.probe(f0)


def card_light(t, n):                    # how bright the inside of card n is (the others are dimmed while one is lit)
    im = clips.grab_frame(f0, t, i0["width"], i0["height"]).convert("RGB")
    return im.getpixel((int(im.width * (260 + n * 440) / 1920), int(im.height * 430 / 1080)))[0]


lit = [[card_light(tl[k][0] + 0.5, n) for n in range(3)] for k in range(3)]
check("讲到哪张卡片，哪张卡片亮", all(lit[k][k] > 225 and all(lit[k][o] < lit[k][k] - 25 for o in range(3) if o != k) for k in range(3)), lit)
before = clips.grab_frame(f0, 0.2, i0["width"], i0["height"]).convert("RGB")
during = clips.grab_frame(f0, tl[0][0] + 0.5, i0["width"], i0["height"]).convert("RGB")
pt = (int(before.width * 0.97), int(before.height * 0.9))
check("其余地方压暗一点，字还看得清", before.getpixel(pt)[0] > 245 and 170 < during.getpixel(pt)[0] < 225, (before.getpixel(pt), during.getpixel(pt)))
mtime = (clips.media_dir(p.id) / s0.web_clip.file).name
p.settings["web_focus"] = False
storage.save(p)
render(p)
check("关掉开关：重新录，没有高亮", p.steps[0].web_clip.file != mtime, (mtime, p.steps[0].web_clip.file))
f0 = clips.media_dir(p.id) / p.steps[0].web_clip.file
off = [card_light(tl[1][0] + 0.5, n) for n in range(3)]
check("关掉以后三张卡片一样亮", min(off) > 225, off)
from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
wj = c.put(f"/api/projects/{p.id}/steps/{p.steps[0].id}/web", headers=H,
           json={"actions": [{"kind": "focus", "control": 2, "unit": 0}]}).json()["steps"][0]["web"]
check("手动设置高亮：保存下来", wj["manual"] and [(a["kind"], a["control"]) for a in wj["actions"]] == [("focus", 2)], wj["actions"])
check("没有这块内容：400", c.put(f"/api/projects/{p.id}/steps/{p.steps[0].id}/web", headers=H,
                         json={"actions": [{"kind": "focus", "control": 9, "unit": 0}]}).status_code == 400)
check("项目数据里有内容块（编辑器用）", [b["key"] for b in c.get(f"/api/projects/{p.id}").json()["steps"][0]["web"]["blocks"]] == ["扳手", "锤子", "卷尺"])

print("\n== 14. 让 AI 写解说时，告诉它页面结构和按钮的作用 ==")
from backend.services import script_gen, webdeck  # noqa: E402

p2 = storage.load(r["project_id"])
webdeck.explore(p2, [p2.steps[1]])
check("把按钮点一遍，记下点完出现的内容", p2.steps[1].web.controls[0].effect == "已完成处理", p2.steps[1].web.controls[0].effect)
d = {}
script_gen._add_items(d, p2, p2.steps[1])
check("给 AI 的内容里有内容块和按钮作用", d.get("page_blocks") == ["先备份", "再发布"]
      and d.get("page_buttons") == [{"name": "运行", "result": "已完成处理"}], d)
check("提示词说明了怎么用", "page_blocks" in script_gen.SLIDES_SYSTEM and "page_buttons" in script_gen.SLIDES_SYSTEM
      and "page_blocks" in script_gen.DIALOGUE_SYSTEM)
p2.steps[1].web.clicks = False
d = {}
script_gen._add_items(d, p2, p2.steps[1])
check("选了“只录动画”：不告诉 AI 有按钮可点", "page_buttons" not in d and "page_blocks" in d, d)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
