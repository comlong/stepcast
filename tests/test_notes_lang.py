"""备注原文模式：备注和解说是同一种语言才照念，不同就交给 AI 用解说语言写；换行按备注本身的文字拼；语言判断。"""
import os
import shutil
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "notes_lang_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from backend import storage  # noqa: E402
from backend.models import Step  # noqa: E402
from backend.services import script_gen  # noqa: E402
from backend.services.langdetect import detect, same_language  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


print("\n== 1. 语言判断 ==")
cases = [("Trainer Script — Built Around Speed\\nThis slide opens the design chapter of Nova Z.".replace("\\n", "\n"), "en"),
         ("这一页介绍设计理念，重点讲外观和驾驶体验。", "zh"),
         ("Die Fahrzeuge sind für den europäischen Markt und die Kunden sind anspruchsvoll.", "de"),
         ("Le design est pensé pour les clients qui veulent une expérience unique et sportive.", "fr"),
         ("このページでは車のデザインについて説明します。", "ja"),
         ("OK", "")]
bad = [(t[:20], detect(t), want) for t, want in cases if detect(t) != want]
check("能分出英文、中文、德文、法文、日文；太短的不判断", not bad, bad)
check("判断不出来时当作同一种语言（不乱改）", same_language("OK", "zh-CN") and same_language("第一页。", "zh-TW"))


class Rec:
    calls = []

    def chat_json(self, messages, **k):
        u = messages[-1]["content"]
        Rec.calls.append(u)
        import json
        import re
        idx = [int(x) for x in re.findall(r'"i": (\d+)', u)]
        return {"title": "标题", "steps": [{"i": i, "title": f"页{i}", "narration": f"AI 用中文写的第 {i} 页。"} for i in idx]}


print("\n== 2. 英文备注 + 中文解说：不照念，交给 AI 用中文写 ==")
proj = storage.create("备注语言", "zh-CN")
proj.source = "slides"
en_notes = "Trainer Script — Built Around Speed\nOpening line\nThis slide opens the design chapter of Nova Z. The design comes from speed."
proj.steps = [Step(kind="slide", index=0, slide_notes=en_notes, page_title="Design"),
              Step(kind="slide", index=1, slide_notes="这一页讲外观设计，重点是前脸和大灯。", page_title="外观"),
              Step(kind="slide", index=2, slide_notes="", page_title="Empty")]
msgs = []
res = script_gen.generate_slides_script(proj, notes_mode="verbatim", missing="ai", client=Rec(),
                                        progress=lambda f, m: msgs.append(m))
st = proj.steps
check("英文备注那页由 AI 用中文写", st[0].narration == "AI 用中文写的第 0 页。" and "This slide opens" in Rec.calls[0], st[0].narration)
import json as _json  # noqa: E402
prompt0 = Rec.calls[0]
sent = _json.JSONDecoder().raw_decode(prompt0[prompt0.index(chr(10) + "["):].strip())[0]   # 提示词里的页面列表
check("这页标了「备注就是讲稿」：AI 照着备注完整地讲，不改写、不压缩",
      [x.get("notes_are_script") for x in sent] == [True, None], [(x["i"], x.get("notes_are_script")) for x in sent])
check("中文备注那页照念", st[1].narration == "这一页讲外观设计，重点是前脸和大灯。")
check("没备注的页 AI 补写", st[2].narration.startswith("AI "))
check("完成提示里说了几页语言不同", res["other_lang"] == 1 and "1" in msgs[-1] and "简体中文" in msgs[-1], msgs[-1])

print("\n== 3. 英文项目照念英文备注：换行用空格接，单词不粘在一起 ==")
p2 = storage.create("英文", "en-US")
p2.source = "slides"
p2.steps = [Step(kind="slide", index=0, slide_notes=en_notes)]
script_gen.generate_slides_script(p2, notes_mode="verbatim", client=Rec())
check("「Speed Opening line This slide」而不是「SpeedOpening lineThis」", "Speed Opening line This slide" in p2.steps[0].narration,
      p2.steps[0].narration[:80])
p3 = storage.create("中文", "zh-CN")
p3.source = "slides"
p3.steps = [Step(kind="slide", index=0, slide_notes="第一行说明。\n第二行说明。")]
script_gen.generate_slides_script(p3, notes_mode="verbatim", client=Rec())
check("中文备注换行直接接上", p3.steps[0].narration == "第一行说明。第二行说明。", p3.steps[0].narration)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
