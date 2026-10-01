"""PPT 备注作解说。"""
import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "notes_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import script_gen, slides, tts  # noqa: E402

slides.powerpoint_available = lambda: False      # 不去碰你正开着的 PowerPoint
tts.synth = lambda text, voice, out, *a, **k: (out.write_bytes(b"\0" * 2048), (1.0, []))[1]

c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers={"Origin": "http://127.0.0.1:8756"})
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


class RecordingLLM:
    calls = []

    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        prompt = messages[-1]["content"]
        RecordingLLM.calls.append(prompt)
        import re
        idx = [int(x) for x in re.findall(r'"i": (\d+)', prompt)]
        return {"title": "AI 标题", "intro": "AI 片头。", "outro": "AI 片尾。",
                "steps": [{"i": i, "title": f"页{i}", "narration": f"AI 写的第 {i} 页。"} for i in idx]}


script_gen.get_client = RecordingLLM


def wait(j):
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)


# ---------------------------------------------------------------- 造一份 PPT
NOTES = {1: "欢迎来到销售对话培训，这是我们认为标准的开场讲解。",
         2: "第二页，先确认客户需求，再介绍产品。\n注意语气要自然。",
         4: "第四页的备注写在普通文本框里，不在标准占位符中。"}
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
for i in range(1, 5):
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = f"第 {i} 页标题"
    s.placeholders[1].text_frame.text = f"第 {i} 页的正文要点"
    if i in (1, 2):
        s.notes_slide.notes_text_frame.text = NOTES[i]
    if i == 4:
        ns = s.notes_slide
        body = ns.notes_placeholder
        body._element.getparent().remove(body._element)          # 去掉标准备注占位符
        # 备注页不支持直接加文本框：先在幻灯片上建一个，再把它的 XML 挪到备注页里
        tb = s.shapes.add_textbox(Inches(1), Inches(5), Inches(6), Inches(2))
        tb.text_frame.text = NOTES[4]
        el = tb._element
        el.getparent().remove(el)
        ns.shapes._spTree.append(el)
deck = SP / "notes_deck.pptx"
prs.save(deck)


def upload(path, name):
    with open(path, "rb") as f:
        r = wait(c.post("/api/import/slides", files={"file": (name, f)}).json())
    assert r["status"] == "done", r
    return r["result"]


print("\n== 解析备注 ==")
man = upload(deck, "销售对话.pptx")
got = {s["i"]: s["notes"] for s in man["slides"]}
check("标准占位符里的备注读到了", got[1] == NOTES[1] and got[2] == NOTES[2])
check("第 3 页没有备注", got[3] == "")
check("写在普通文本框里的备注也读到了（兜底）", got[4] == NOTES[4], repr(got[4]))
check("统计出 3 页有备注", man["with_notes"] == 3, man["with_notes"])

print("\n== 备注原文 + 没备注的页留空 ==")
RecordingLLM.calls.clear()
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "verbatim", "missing": "empty", "language": "zh-CN"}).json())
p = storage.load(r["result"]["project_id"])
narr = {s.index + 1: s.narration for s in p.steps}
check("任务成功", r["status"] == "done", r.get("error", ""))
check("完全没有调用 DeepSeek", not RecordingLLM.calls, len(RecordingLLM.calls))
check("有备注的页 = 备注原文（换行已并成一段）",
      narr[1] == NOTES[1] and narr[2] == NOTES[2].replace("\n", "") and narr[4] == NOTES[4], narr)
check("没备注的第 3 页留空", narr[3] == "")
check("选择被记在项目里", p.settings.get("slides_notes_mode") == "verbatim"
      and p.settings.get("slides_missing") == "empty")
check("有备注的页已配音、空页没有", [bool(s.audio) for s in p.steps] == [True, True, False, True],
      [bool(s.audio) for s in p.steps])
PID_EMPTY = p.id

print("\n== 备注原文 + 没备注的页 AI 补写 ==")
RecordingLLM.calls.clear()
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "verbatim", "missing": "ai", "language": "zh-CN"}).json())
p = storage.load(r["result"]["project_id"])
narr = {s.index + 1: s.narration for s in p.steps}
sent = "\n".join(RecordingLLM.calls)
check("只调用一次 DeepSeek", len(RecordingLLM.calls) == 1, len(RecordingLLM.calls))
check("请求里只有第 3 页", "第 3 页标题" in sent and "第 1 页标题" not in sent and "第 4 页标题" not in sent)
check("备注原文没有被发出去", not any(n.split("，")[0] in sent for n in NOTES.values()))
check("第 3 页由 AI 补写，其余是备注原文",
      narr[3].startswith("AI 写的") and narr[1] == NOTES[1] and narr[4] == NOTES[4], narr)

print("\n== AI 参考备注改写（对照）==")
RecordingLLM.calls.clear()
r = wait(c.post(f"/api/import/slides/{man['id']}/create",
                json={"notes_mode": "reference", "language": "zh-CN"}).json())
p_ref = storage.load(r["result"]["project_id"])
sent = "\n".join(RecordingLLM.calls)
check("参考模式会把备注发给 AI", "标准的开场讲解" in sent)
check("参考模式解说是 AI 写的", all(s.narration.startswith("AI 写的") for s in p_ref.steps))

print("\n== 已导入的项目，在「生成解说」里改用备注原文 ==")
RecordingLLM.calls.clear()
r = wait(c.post(f"/api/projects/{p_ref.id}/script",
                json={"notes_mode": "verbatim", "missing": "empty", "overwrite": True}).json())
p2 = storage.load(p_ref.id)
narr = {s.index + 1: s.narration for s in p2.steps}
check("任务成功", r["status"] == "done", r.get("error", ""))
check("没有调用 DeepSeek", not RecordingLLM.calls)
check("有备注的页换成了备注原文", narr[1] == NOTES[1] and narr[4] == NOTES[4], narr)
check("没备注的页保留原来 AI 写的内容（留空模式不删已有文字）", narr[3].startswith("AI 写的"))
check("项目记住了新的选择", p2.settings.get("slides_notes_mode") == "verbatim")

print("\n== 之后点「一键生成」沿用这个选择 ==")
RecordingLLM.calls.clear()
c.patch(f"/api/projects/{p_ref.id}/steps/{p2.steps[0].id}", json={"narration": "", "caption": ""})
c.patch(f"/api/projects/{p_ref.id}/steps/{p2.steps[0].id}", json={"slide_notes": "我在编辑器里改过的备注。"})
import backend.services.video as video
video.render_project = lambda proj, **k: {"file": ""}
r = wait(c.post(f"/api/projects/{p_ref.id}/auto", json={}).json())
p3 = storage.load(p_ref.id)
check("一键生成成功", r["status"] == "done", r.get("error", ""))
check("第 1 页用了编辑器里改过的备注", p3.steps[0].narration == "我在编辑器里改过的备注。", p3.steps[0].narration)
check("没有调用 DeepSeek", not RecordingLLM.calls)

print("\n== PDF ==")
pdf = SP / "pptspike" / "export_slides.pdf"
man_pdf = upload(pdf, "导出.pdf")
check("PDF 解析成功", man_pdf["count"] == 3)
check("PDF 没有备注", man_pdf["with_notes"] == 0 and man_pdf["ext"] == ".pdf")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
print("IIDS", json.dumps({"pptx": man["id"], "pdf": man_pdf["id"]}))
