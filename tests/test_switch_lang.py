"""Switching language: slide projects are rewritten from the deck (fake AI, no cost); video steps and missed slides are translated; recorded projects are translated as a whole."""
import os
import shutil
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "switch_lang_data"
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
from backend.models import Step, VideoClip  # noqa: E402
from backend.services import script_gen  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


class Fake:
    calls = []

    def chat_json(self, messages, **k):
        u = messages[-1]["content"]
        Fake.calls.append(u)
        import json
        import re
        if "翻译成" in u:
            items = json.loads(u[u.rindex("\n\n[") + 2:])
            return {"items": [{"k": x["k"], "t": "TR:" + x["t"]} for x in items]}
        idx = [int(x) for x in re.findall(r'"i": (\d+)', u)]
        return {"title": "EN Title", "subtitle": "EN sub", "intro": "EN intro", "outro": "EN outro",
                "steps": [{"i": i, "title": f"EN t{i}", "narration": f"EN from PPT {i}"} for i in idx if i != 2]}


def slide(i, **k):
    return Step(kind="slide", index=i, narration=f"中文解说{i}", caption=f"中文解说{i}", audio=f"a{i}.mp3",
                audio_duration=3.0, slide_text=f"Slide text {i}", slide_notes=f"Trainer notes {i}", **k)


proj = storage.create("换语言", "zh-CN")
proj.source = "slides"
proj.settings = {"slides_notes_mode": "verbatim"}
proj.title, proj.intro = "中文标题", "中文片头"
proj.steps = [slide(0), slide(1, voice_source="own"), slide(2), slide(3, include=False),
              Step(kind="video", index=4, narration="视频解说", caption="视频解说",
                   clip=VideoClip(file="v.mp4", audio="mute", has_audio=True)),
              Step(kind="video", index=5, caption="视频原声字幕",
                   clip=VideoClip(file="w.mp4", audio="original", has_audio=True))]
res = script_gen.switch_language(proj, "en-US", client=Fake())
st = proj.steps
print("   ", res)
check("项目语言换成英文", proj.language == "en-US")
check("备注原文模式换语言时改成 AI 参考备注改写（页面发给了 AI，带备注）",
      any("请为下面这份幻灯片写配音旁白" in c and "Trainer notes 0" in c and "English" in c for c in Fake.calls))
check("幻灯片页按 PPT 原文重写，不是翻译", st[0].narration == "EN from PPT 0" and st[0].caption == st[0].narration
      and st[0].audio == "", (st[0].narration, st[0].audio))
check("自己录音的页也重写并改成 AI 朗读", st[1].narration == "EN from PPT 1" and st[1].voice_source == "tts" and st[1].audio == "")
check("AI 漏掉的页退回翻译", st[2].narration == "TR:中文解说2", st[2].narration)
check("没勾选的页不动", st[3].narration == "中文解说3")
check("静音配解说的视频步骤：解说翻译", st[4].narration == "TR:视频解说", st[4].narration)
check("播原声的视频步骤：字幕翻译", st[5].caption == "TR:视频原声字幕", st[5].caption)
check("标题片头用重写的，不再翻译", proj.title == "EN Title" and proj.intro == "EN intro"
      and not any('"k": "title"' in c for c in Fake.calls if "翻译成" in c), proj.title)

Fake.calls.clear()
web = storage.create("网页", "zh-CN")
web.title = "网页标题"
web.steps = [Step(kind="click", index=0, narration="点击登录", caption="点击登录")]
script_gen.switch_language(web, "en-US", client=Fake())
check("网页录制项目：整体翻译", web.steps[0].narration == "TR:点击登录" and web.title == "TR:网页标题"
      and not any("幻灯片" in c for c in Fake.calls))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
