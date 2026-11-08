"""The assistant's client script agent/workbuddy/stepcast-video/scripts/stepcast.ps1, run with Windows PowerShell against a real
local server (own port and data folder): PPT import without AI, JSON through files, the hint when quotes were lost,
script import matched by step id, slide text in the step list, rendering and second subtitles, an HTML deck and its clicks
(when Edge or Chrome is installed). No voice-over, no AI."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SP = Path(sys.argv[1])
if os.name != "nt" or not shutil.which("powershell"):
    print("== 跳过：需要 Windows PowerShell")
    print("\n结果: 全部通过")
    sys.exit(0)

DATA = SP / "agent_script_data"
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
import uvicorn  # noqa: E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import llm  # noqa: E402

SCRIPT = ROOT / "agent" / "workbuddy" / "stepcast-video" / "scripts" / "stepcast.ps1"
WORK = DATA / "work"
WORK.mkdir()
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)

def ps(*args, base=BASE):
    """Run the script the way the skill tells the assistant to; returns (exit code, stdout, stderr)."""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *map(str, args), "-Base", base]
    r = subprocess.run(cmd, capture_output=True, cwd=WORK, timeout=300)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def ps_json(*args):
    code, out, err = ps(*args)
    try:
        return code, json.loads(out), err
    except ValueError:
        return code, None, err + out


def write(name, text):
    p = WORK / name
    p.write_text(text, encoding="utf-8")
    return p


print("\n== 0. 技能文件能被 WorkBuddy 读 ==")
skill_md = (SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
head = skill_md.split("---", 2)[1] if skill_md.startswith("---") else ""
meta = dict(ln.split(":", 1) for ln in head.strip().splitlines() if ":" in ln)
meta = {k.strip(): v.strip() for k, v in meta.items()}
check("头部有 name / description 和 WorkBuddy 的显示名、中英文简介",
      all(meta.get(k) for k in ("name", "description", "display_name", "display_name_en", "description_zh", "description_en")),
      sorted(meta))
check("name 是小写连字符", bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta.get("name", ""))), meta.get("name"))
check("简介里没有尖括号（WorkBuddy 的校验不收）", not any(c in meta.get(k, "") for k in meta if k.startswith("description")
                                               for c in "<>"))
check("技能版本和 StepCast 一致", meta.get("version") == main.app.version, (meta.get("version"), main.app.version))
check("脚本是带 BOM 的 UTF-8（Windows PowerShell 5.1 才认得中文）", SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf")

print("\n== 1. 连接与内置 AI 开关 ==")
code, h, err = ps_json("health")
check("health 返回版本和数据目录", code == 0 and h and h["version"] == main.app.version
      and Path(h["data_dir"]) == Path(os.environ["VT_DATA_DIR"]), err[:200])
code, _, err = ps_json("ai-off")
check("ai-off 之后内置 AI 关闭", code == 0 and llm.ai_disabled(), err[:200])
code, out, err = ps("health", base="http://127.0.0.1:1")
check("服务没开：提示先启动 StepCast.exe", code == 1 and "Start StepCast.exe" in err, err[:200])

print("\n== 2. PPT 导入：备注当解说、没备注的留空、不调 AI、不配音 ==")
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
NOTES = {1: "这一页介绍整体流程，先看三个阶段。", 3: "This page explains the approval rules."}
for i in range(1, 5):
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = f"第 {i} 页"
    s.placeholders[1].text_frame.text = f"要点 {i}-A\n要点 {i}-B"
    if i in NOTES:
        s.notes_slide.notes_text_frame.text = NOTES[i]
deck = WORK / "演示 文稿.pptx"
prs.save(deck)
code, r, err = ps_json("import", "-File", deck.name, "-Language", "zh-CN", "-Voice", "zh-CN-YunxiNeural", "-Pages", "1-3")
check("导入成功，选中的 3 页", code == 0 and r and r["slides"] == 3, err[-300:])
pid = (r or {}).get("project", "")
proj = storage.load(pid) if pid else None
check("中文文件名直接当项目名", proj is not None and proj.name == "演示 文稿", proj and proj.name)
if proj is None:
    print(f"\n结果: 失败 {len(fails) or 1} 项: {fails}")
    sys.exit(1)
steps = proj.steps
check("每页一个幻灯片步骤", [s.kind for s in steps] == ["slide"] * 3 and proj.source == "slides")
check("同语言的备注直接当解说", steps[0].narration == NOTES[1], steps[0].narration)
check("没备注的页留空", steps[1].narration == "")
check("备注是别的语言：留空，在 AI 关闭检查处就停下", steps[2].narration == "" and "内置 AI 已关闭" in r["warning"],
      r["warning"])
check("还没配音", all(not s.audio for s in steps))
check("语言和音色按参数设置", proj.language == "zh-CN" and proj.voice == "zh-CN-YunxiNeural", (proj.language, proj.voice))
code, out, err = ps("import", "-File", "missing.pptx")
check("文件不存在：说清楚", code == 1 and "file not found" in err, err[:200])
write("notes.txt", "x")
code, out, err = ps("import", "-File", "notes.txt")
check("不支持的格式：说清楚", code == 1 and ".pptx" in err, err[:200])

print("\n== 3. 步骤列表里有幻灯片文字和备注 ==")
code, pj, err = ps_json("project", "-Project", pid)
rows = (pj or {}).get("steps", [])
check("列出 3 步，序号从 0 开始", code == 0 and [x["index"] for x in rows] == [0, 1, 2], err[:200])
check("幻灯片文字和备注都在", rows and "要点 1-A" in rows[0]["slide_text"] and rows[0]["notes"] == NOTES[1]
      and rows[2]["notes"] == NOTES[3], rows[:1])
code, path, err = ps("shot", "-Project", pid, "-Step", "1")
check("shot 给出这一页的图片路径", code == 0 and Path(path.strip()).exists(), path.strip() or err[:200])

print("\n== 4. 脚本导出 / 导入按步骤 id 对位 ==")
code, out, err = ps("script-export", "-Project", pid, "-Out", "script.json")
sj = json.loads((WORK / "script.json").read_text(encoding="utf-8"))
check("导出的每一步带 id", code == 0 and [x["id"] for x in sj["steps"]] == [s.id for s in steps], err[:200])
NEW = ["第一页：先看整体流程的三个阶段。", "第二页讲提交前要准备的材料，比如“报销单”和发票。", "第三页讲审批规则：金额超过一千元要经理批准。"]
sj["title"] = "流程培训"
sj["intro"] = "大家好，这个视频讲报销流程。"
for x, t in zip(sj["steps"], NEW):
    x["narration"] = t
    x["title"] = t[:3]
    x.pop("index")                           # matched by id alone
sj["steps"].reverse()                        # order in the file doesn't matter
write("script.json", json.dumps(sj, ensure_ascii=False, indent=1))
code, r, err = ps_json("script-import", "-Project", pid, "-File", "script.json")
proj = storage.load(pid)
check("按 id 写回每一步", code == 0 and [s.narration for s in proj.steps] == NEW and proj.title == "流程培训"
      and proj.intro == sj["intro"], err[:200] or [s.narration for s in proj.steps])
check("字幕跟着解说", [s.caption for s in proj.steps] == NEW)
bad = {"steps": [{"index": i + 1, "narration": f"错位 {i}"} for i in range(3)]}      # counted from 1
write("bad.json", json.dumps(bad, ensure_ascii=False))
code, out, err = ps("script-import", "-Project", pid, "-File", "bad.json")
check("序号从 1 数（有一项对不上）：整份不导入，并说明从 0 开始",
      code == 1 and "index 3" in err and "start at 0" in err and storage.load(pid).steps[0].narration == NEW[0], err[:300])
write("bad2.json", json.dumps({"steps": [{"id": "s_nope", "narration": "x"}]}))
code, out, err = ps("script-import", "-Project", pid, "-File", "bad2.json")
check("不存在的 id：整份不导入", code == 1 and "s_nope" in err, err[:200])
old = {"steps": [{"index": 1, "narration": "旧格式只有序号，也能导入。"}]}
write("old.json", json.dumps(old, ensure_ascii=False))
code, out, err = ps("script-import", "-Project", pid, "-File", "old.json")
proj = storage.load(pid)
check("没有 id 的旧格式按序号（从 0 开始）", code == 0 and proj.steps[1].narration == old["steps"][0]["narration"]
      and proj.steps[0].narration == NEW[0], err[:200])
NEW[1] = old["steps"][0]["narration"]

print("\n== 5. 项目设置：JSON 走文件、常用项直接给参数 ==")
write("project.json", json.dumps({"outro": "谢谢观看，有问题找“财务部”。", "settings": {"intro_enabled": False}},
                                 ensure_ascii=False))
code, r, err = ps_json("set", "-Project", pid, "-File", "project.json")
proj = storage.load(pid)
check("-File 读 JSON（中文和引号都保留）", code == 0 and proj.outro == "谢谢观看，有问题找“财务部”。"
      and proj.settings.get("intro_enabled") is False, err[:200])
code, r, err = ps_json("set", "-Project", pid, "-Subtitle", "新员工必看", "-Voice", "zh-CN-XiaoxiaoNeural")
proj = storage.load(pid)
check("直接参数设置副标题和音色", code == 0 and proj.subtitle == "新员工必看" and proj.voice == "zh-CN-XiaoxiaoNeural", err[:200])
code, r, err = ps_json("set", "-Project", pid, "-File", "project.json", "-Title", "报销流程")
proj = storage.load(pid)
check("文件和参数可以一起用", code == 0 and proj.title == "报销流程" and proj.outro.startswith("谢谢观看"), err[:200])
# what WorkBuddy did: its PowerShell tool starts the script in another PowerShell, which loses the double quotes
nested = ("& powershell -NoProfile -ExecutionPolicy Bypass -File '" + str(SCRIPT) + "' set -Project " + pid
          + " -Json '{\"intro\":\"x\"}' -Base " + BASE)
r = subprocess.run(["powershell", "-NoProfile", "-Command", nested], capture_output=True, cwd=WORK, timeout=120)
err = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
check("-Json 的引号在转手时丢了：报错并建议用 -File", r.returncode != 0 and "-File" in err and "quotes" in err, err[:300])
check("丢了引号的 JSON 没写进项目", storage.load(pid).intro == sj["intro"])
code, out, err = ps("set", "-Project", pid)
check("什么都没给：说明能设什么", code == 1 and "nothing to set" in err, err[:200])

print("\n== 6. 单步修改、背景页、按序号找步骤 ==")
write("narr.txt", "这一页先说为什么要走这个流程。\n")
code, out, err = ps("slide", "-Out", "bg.png", "-Title", "为什么要报销审批", "-File", write("bg.txt", "控制成本\n  每笔都有记录\n· 补充说明").name)
png = Path(out.strip())
with Image.open(png) as im:
    size = im.size
check("slide 画出 1920x1080 的页面", code == 0 and size == (1920, 1080), err[:200])
code, added, err = ps_json("image", "-Project", pid, "-Image", "bg.png", "-Title", "背景", "-File", "narr.txt", "-Position", "0")
proj = storage.load(pid)
check("插到最前面", code == 0 and proj.steps[0].kind == "slide" and proj.steps[0].title == "背景"
      and proj.steps[0].narration.startswith("这一页先说") and [s.index for s in proj.steps] == [0, 1, 2, 3], err[:200])
code, r, err = ps_json("step", "-Project", pid, "-Step", "3", "-File", write("n3.txt", "审批规则改成：超过两千元要经理批准。").name)
proj = storage.load(pid)
check("-Step 按序号找到第 4 步", code == 0 and proj.steps[3].narration == "审批规则改成：超过两千元要经理批准。", err[:200])
code, r, err = ps_json("step", "-Project", pid, "-Step", proj.steps[2].id, "-Include", "false")
check("-Step 也可以给 id", code == 0 and storage.load(pid).steps[2].include is False, err[:200])
code, out, err = ps("step", "-Project", pid, "-Step", "9", "-Title", "x")
check("序号不存在：说明从 0 开始", code == 1 and "start at 0" in err, err[:200])

print("\n== 7. 渲染和第二字幕 ==")
code, r, err = ps_json("render", "-Project", pid, "-Width", "640", "-Height", "360", "-NoSidecar", "-NoSubSpace")
video = Path((r or {}).get("video", ""))
check("渲染出 MP4（没配音按最短时长）", code == 0 and video.suffix == ".mp4" and video.exists(), err[-300:])
check("视频旁没有 .srt", code == 0 and not video.with_suffix(".srt").exists())
code, out, err = ps("sentences", "-Project", pid, "-Out", "s.json")
sent = json.loads((WORK / "s.json").read_text(encoding="utf-8"))
n = len(sent["sentences"])
check("取到字幕句子", code == 0 and n > 0, err[:200])
write("t.json", json.dumps({"key": sent["key"], "texts": [f"Sentence {i + 1}." for i in range(n)]}))
code, tracks, err = ps_json("subs-import", "-Project", pid, "-Lang", "en-US", "-File", "t.json")
check("导入英文字幕", code == 0 and video.with_name(video.stem + ".en-US.vtt").exists(), err[:200])

print("\n== 8. HTML 讲稿：导入、按钮和点击、手动设置点击 ==")
from backend.services import cdp  # noqa: E402
import web_fixtures as fx  # noqa: E402

if cdp.find_browser() is None:
    print("  这台电脑没有 Edge / Chrome，跳过")
else:
    write("演示.html", fx.custom_deck())
    code, r, err = ps_json("import", "-File", "演示.html", "-Language", "zh-CN")
    html = (r or {}).get("html") or {}
    check("导入 HTML 讲稿：4 页，说明怎么翻页、几页的备注里有操作提示",
          code == 0 and r["slides"] == 4 and html.get("turns_pages_by") == "hash" and html.get("slides_with_directions") == 2,
          err[-300:] if code else r)
    wid = (r or {}).get("project", "")
    code, pj, err = ps_json("project", "-Project", wid)
    rows = (pj or {}).get("steps", [])
    h2 = (rows[2].get("html") if len(rows) > 2 else None) or {}
    m = re.fullmatch(r"click control 0 at sentence (\d+)", (h2.get("clicks") or [""])[0])
    check("步骤列表里有按钮、解说的句子和点击（都从 0 编号）",
          code == 0 and h2.get("controls") == ["0 ▶ 运行演示"] and len(h2.get("clicks", [])) == 1 and m is not None
          and h2["live"] is True and h2["set_by_hand"] is False, h2 or err[:200])
    check("点击在操作提示后面那句的开头",
          m is not None and h2["sentences"][int(m.group(1))].startswith(m.group(1) + " 它会先读文件"), h2.get("sentences"))
    check("没有按钮的页也列出（空的）", rows and rows[0].get("html", {}).get("controls") == [] and rows[0]["html"]["clicks"] == [],
          rows[0].get("html") if rows else None)
    check("每页的内容块也列出", all(isinstance(x.get("html", {}).get("blocks"), list) for x in rows), rows[1].get("html") if len(rows) > 1 else None)
    write("clicks.json", json.dumps({"actions": [{"kind": "click", "control": 0, "unit": 0}]}))
    code, h, err = ps_json("web", "-Project", wid, "-Step", "2", "-File", "clicks.json")
    st = storage.load(wid).steps[2]
    check("web -File：在第 0 句开头点 0 号按钮，记为手动设置",
          code == 0 and h and h["clicks"] == ["click control 0 at sentence 0"] and h["set_by_hand"] is True and st.web.manual
          and [(a.kind, a.control, a.unit) for a in st.web.actions] == [("click", 0, 0)], err[:300] or h)
    write("none.json", json.dumps({"actions": []}))
    code, h, err = ps_json("web", "-Project", wid, "-Step", "2", "-File", "none.json")
    check("空列表：去掉所有点击", code == 0 and h and h["clicks"] == [] and not storage.load(wid).steps[2].web.actions,
          err[:300] or h)
    code, h, err = ps_json("web", "-Project", wid, "-Step", "2", "-Auto")
    check("web -Auto：按备注里的提示重新放", code == 0 and h and h["set_by_hand"] is False and len(h["clicks"]) == 1,
          err[:300] or h)
    write("far.json", json.dumps({"actions": [{"kind": "click", "control": 7, "unit": 0}]}))
    code, out, err = ps("web", "-Project", wid, "-Step", "2", "-File", "far.json")
    check("没有这个按钮：报出 StepCast 的说明，原来的点击不变",
          code == 1 and "没有这个控件：7" in err and len(storage.load(wid).steps[2].web.actions) == 1, err[:300])
    write("bad_clicks.json", json.dumps({"click": 1}))
    code, out, err = ps("web", "-Project", wid, "-Step", "2", "-File", "bad_clicks.json")
    check("点击文件的格式不对：给出示例", code == 1 and '"actions"' in err, err[:300])
    code, h, err = ps_json("web", "-Project", wid, "-Step", "2", "-Live", "false")
    check("web -Live false：这一页变成静态图片", code == 0 and h and h["live"] is False
          and storage.load(wid).steps[2].web.live is False, err[:300] or h)
    code, out, err = ps("web", "-Project", pid, "-Step", "1")
    check("不是 HTML 讲稿的页：说清楚", code == 1 and "not a slide of an HTML deck" in err, err[:300])

check("内置 AI 一直是关的", llm.ai_disabled())

server.should_exit = True
print(f"\n结果: {'全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'}")
