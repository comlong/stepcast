"""HTML slides, without a browser: stage directions in the notes, where the clicks go in the narration, their seconds, and how they
follow an edited narration. Synthetic texts only."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.models import Step, WebAction, WebBlock, WebControl, WebPage  # noqa: E402
from backend.services import script_gen, web_actions as wa  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def ctl(text="", name="", cls="", group="g", path=""):
    return WebControl(text=text, name=name, cls=cls, group=group, path=path or f":scope > button:nth-of-type({len(text) + 1})")


def at(narration, acts, k):
    return narration[acts[k].pos:acts[k].pos + 6]


print("== 1. 讲稿里的操作提示 ==")
notes = "看一个例子。（点按钮）它会先读文件（也就是 PDF），再算结果。（依次点 1 到 3 号热点）按照计划进行。(click the Run button) Done."
cues = wa.find_cues(notes)
check("找到三个提示（中文、英文）", [c.body for c in cues] == ["点按钮", "依次点 1 到 3 号热点", "click the Run button"],
      [c.body for c in cues])
clean = wa.strip_cues(notes)
check("念的文字里没有提示，内容括号留着", "点按钮" not in clean and "（也就是 PDF）" in clean and "click" not in clean and "按照计划" in clean,
      clean)
check("「（按照…）」「(open source)」这类内容不是提示",
      not wa.find_cues("（按照计划）(open source)（打开方式见附录）(Drucker im 2. Stock)"))
check("各种语言的提示", len(wa.find_cues("(Klick auf Start) (cliquer sur OK) (haz clic en Ir) (clicca Avvia) (klik op Start) "
                                      "(kliknij Start) [click] 【点击开始】")) == 8)
check("备注原文作解说时，提示不会被念出来",
      script_gen.notes_as_narration("第一句。（点按钮）第二句。\n\n(click) Third.") == "第一句。第二句。Third.",
      script_gen.notes_as_narration("第一句。（点按钮）第二句。\n\n(click) Third."))

print("\n== 2. 一个按钮：在提示的位置点 ==")
notes = "看一个例子。我们让它整理一份名单。（点按钮）它会先读文件，最后把结果写回去。"
narr = script_gen.notes_as_narration(notes)
controls = [ctl("▶ 运行演示", cls="btn")]
acts = wa.plan(narr, notes, controls)
check("点一次，在提示后面那句话开头", len(acts) == 1 and narr[acts[0].pos:].startswith("它会先读文件"), [at(narr, acts, 0)])
notes_end = "最后看结果。（点按钮看结果）"
acts = wa.plan(script_gen.notes_as_narration(notes_end), notes_end, controls)
check("提示在最后：讲完再点", len(acts) == 1 and acts[0].pos == len(script_gen.notes_as_narration(notes_end)))

print("\n== 3. 依次点热点：跟着解说里各自的名字 ==")
notes = "界面上有三个地方。第一，任务列表，每个任务独立；第二，文件夹，只能读写它；第三，模型，自动选择。（依次点 1 到 3 号热点）"
narr = script_gen.notes_as_narration(notes)
hots = [ctl("1", "任务列表", "hot", "hots"), ctl("2", "文件夹", "hot", "hots"), ctl("3", "模型", "hot", "hots"),
        ctl("① 任务列表", cls="chip", group="chips")]
acts = wa.plan(narr, notes, hots)
check("点三个热点，按顺序", [a.control for a in acts] == [0, 1, 2], [(a.control, at(narr, acts, k)) for k, a in enumerate(acts)])
check("各自在讲到它的地方", [narr[a.pos:a.pos + 2] for a in acts] == ["任务", "文件", "模型"], [narr[a.pos:a.pos + 4] for a in acts])
check("同名的其他按钮不点", all(a.control != 3 for a in acts))

print("\n== 4. 依次点「下一步」：在第二步、第三步……开头 ==")
notes = "第一步，打开模板。第二步，填写名称。第三步，检查格式。最后第四步，保存结果。（依次点下一步）"
narr = script_gen.notes_as_narration(notes)
acts = wa.plan(narr, notes, [ctl("‹ 上一步"), ctl("下一步 ›", cls="btn")])
check("点三次「下一步」", [a.control for a in acts] == [1, 1, 1], [a.control for a in acts])
check("在第二、三、四步开头", [narr[a.pos:a.pos + 3] for a in acts] == ["第二步", "第三步", "最后第"], [narr[a.pos:a.pos + 3] for a in acts])

print("\n== 5. 没有提示：只点解说里明确说到的 ==")
notes = "它有三种用法。快速模式适合日常小事；精确模式结果更严谨；还可以导出报告。"
narr = script_gen.notes_as_narration(notes)
tabs = [ctl("快速模式", group="tabs"), ctl("精确模式", group="tabs"), ctl("批量模式", group="tabs"), ctl("帮助", group="h")]
acts = wa.plan(narr, notes, tabs)
check("点说到的两个，没说到的不点", [a.control for a in acts] == [0, 1], [a.control for a in acts])
check("在分句开头", [narr[a.pos:a.pos + 4] for a in acts] == ["快速模式", "精确模式"])
en = "There are two views. The list view shows every task; the board view groups them by status."
acts = wa.plan(en, en, [WebControl(text="List view", group="v", path="#a"), WebControl(text="Board view", group="v", path="#b")])
check("英文也一样", [a.control for a in acts] == [0, 1] and en[acts[1].pos:].startswith("the board view"),
      [en[a.pos:a.pos + 14] for a in acts])

print("\n== 6. 名字不完全一样也认得 ==")
check("少了「个」：做个备份 ~ 做备份", wa.find_phrase("还能帮你做备份。", "做个备份") is not None)
check("长名字说了大半：每周自动生成报表 ~ 每周自动生成", (wa.find_phrase("以后每周自动生成，一句话就行。", "每周自动生成报表") or (0, 0, False))[2])
check("分句开头", wa.clause_start("前面一句，或者在邮件里发出去。", 8) == 5)

print("\n== 7. 分步出现（→）：跟着解说里各部分的文字 ==")
narr = "Here are the points. Alpha arrives first in the list. Then beta follows next."
acts = wa.plan(narr, "", [], builds=2, build_texts=["Alpha arrives first", "Beta follows next"])
check("两次「下一部分」", [a.kind for a in acts] == ["next", "next"])
check("在说到它们的那个分句开头", [narr[a.pos:a.pos + 5] for a in acts] == ["Alpha", "Then "], [narr[a.pos:a.pos + 5] for a in acts])

print("\n== 8. 秒数 ==")
step = Step(kind="slide", narration="第一句话说完。第二句话说完。第三句。", audio_duration=6.0,
            boundaries=[{"t": 0.0, "d": 2.0, "text": "第一句话说完。"}, {"t": 2.2, "d": 2.0, "text": "第二句话说完。"},
                        {"t": 4.4, "d": 1.4, "text": "第三句。"}])
step.web = WebPage(controls=[ctl("A"), ctl("B")],
                   actions=[WebAction(control=0, pos=7), WebAction(control=1, pos=7), WebAction(control=0, pos=len(step.narration))])
tl = wa.timeline(step, 0.35, 7.0)
check("第二句开头：配音里第二句开始的时候（稍早一点）", abs(tl[0][0] - (0.35 + 2.2 - 0.15)) < 0.05, tl[0][0])
check("两个点击至少隔开一点", tl[1][0] - tl[0][0] >= wa.MIN_GAP - 1e-6, [t for t, _ in tl])
check("讲完以后的点击：配音结束之后", tl[2][0] >= 0.35 + 6.0, tl[2][0])
step.web.actions = [WebAction(control=0, pos=0)]
check("最早也要等页面出来", wa.timeline(step, 0.35, 7.0)[0][0] >= wa.MIN_FIRST)
check("没有配音时按文字比例估", Step(kind="slide", narration="abcd", web=WebPage(controls=[ctl("A")], actions=[WebAction(control=0, pos=2)]))
      and abs(wa.timeline(Step(kind="slide", narration="abcdefghij", web=WebPage(controls=[ctl("A")], actions=[WebAction(control=0, pos=5)])),
                          0.0, 10.6)[0][0] - 5.0) < 0.01)

print("\n== 9. 解说改了：自动的重新安排，手动的跟着原来那句话 ==")
notes = "它有两种用法。快速模式适合小事；精确模式更严谨。"
st = Step(kind="slide", slide_notes=notes, narration=script_gen.notes_as_narration(notes))
st.web = WebPage(controls=[ctl("快速模式", group="t"), ctl("精确模式", group="t")])
wa.refresh(st)
first = [a.pos for a in st.web.actions]
st.narration = "先说一句开场白。" + st.narration
changed = wa.refresh(st)
check("自动：解说改了就重新安排", changed and [a.pos for a in st.web.actions] == [p + 8 for p in first],
      ([a.pos for a in st.web.actions], first))
check("没改就不动", not wa.refresh(st))
st.web.manual = True
st.web.actions = [WebAction(control=1, pos=st.narration.index("精确"), quote=wa.quote(st.narration, st.narration.index("精确")))]
st.narration = st.narration.replace("先说一句开场白。", "")
wa.refresh(st)
check("手动：找回原来那句话", st.narration[st.web.actions[0].pos:].startswith("精确模式"), st.web.actions[0].pos)

print("\n== 10. 解说翻译成别的语言：点击留在各自那句话 ==")
notes = "它有两种用法。快速模式适合小事。精确模式更严谨。"
st = Step(kind="slide", slide_notes=notes, narration=script_gen.notes_as_narration(notes))
st.web = WebPage(controls=[ctl("快速模式", group="t"), ctl("精确模式", group="t")])
wa.refresh(st)
check("中文解说：第 2、3 句各点一次", [a.unit for a in st.web.actions] == [1, 2], [a.unit for a in st.web.actions])
st.narration = "There are two ways to use it. The fast mode suits small jobs. The precise mode is more careful."
wa.refresh(st)
check("译成英文（按钮名对不上了）：点击还在第 2、3 句开头", [st.narration[a.pos:a.pos + 8] for a in st.web.actions] == ["The fast", "The prec"],
      [st.narration[a.pos:a.pos + 8] for a in st.web.actions])
st.web.manual = True
st.web.actions = wa.stamp(st.narration, [WebAction(control=1, pos=st.narration.index("The precise"),
                                                   quote=wa.quote(st.narration, st.narration.index("The precise")))])
st.web.unit_count = 3
st.narration = "Es gibt zwei Arten. Der schnelle Modus passt zu kleinen Aufgaben. Der genaue Modus ist sorgfältiger."
wa.refresh(st)
check("手动设置的点击：换了语言也留在第 3 句", st.narration[st.web.actions[0].pos:].startswith("Der genaue"), st.web.actions[0].pos)

print("\n== 11. 更多说法的提示 ==")
check("「（此处播放动画）」「（这里停顿一下）」「（稍等）」都是提示", len(wa.find_cues("A（此处播放动画）B（这里停顿一下）C（稍等）")) == 3)
check("「（这里指的是…）」不是提示", not wa.find_cues("A（这里指的是上个月）"))

print("\n== 12. 讲到哪亮到哪：解说明确提到内容块时才高亮 ==")


def blk(key, text=""):
    return WebBlock(key=key, text=text or key, path=f":scope > div:nth-of-type({len(key)})")


B = [blk("收件箱"), blk("日历提醒"), blk("通讯录")]
n = "先看收件箱。每封邮件都是分开的。然后说日历提醒，它会提前通知你。最后是天气。"
acts = wa.plan(n, "", [], 0, (), B)
check("提到的两块亮，没提到的不亮", [(a.kind, a.control) for a in acts] == [("focus", 0), ("focus", 1)], [(a.kind, a.control) for a in acts])
check("亮在提到它的那个分句开头", n[acts[1].pos:acts[1].pos + 2] == "然后", n[acts[1].pos:acts[1].pos + 6])
check("换一种说法（解说没用原词）：宁可不亮", wa.plan("这里讲的是可以访问哪些目录。", "", [], 0, (), B) == [])
check("太笼统的名字不算", wa.plan("这个结果很重要，注意看。", "", [], 0, (), [blk("结果"), blk("注意")]) == [])
check("两块同名：说不清是哪块，不亮", wa.plan("我们看月度报表。", "", [], 0, (), [blk("月度报表"), blk("月度报表", "月度报表 另一块")]) == [])
c1 = [ctl(text="运行演示")]
n2 = "先看收件箱。点一下运行演示，看收件箱怎么变化。"
acts = wa.plan(n2, "", c1, 0, (), B)
check("同一句里页面自己有点击：不再加高亮", [(a.kind, wa.units(n2)[a.unit][0]) for a in acts] == [("focus", 0), ("click", n2.index("点一下"))],
      [(a.kind, a.pos, a.unit) for a in acts])
lst = "扳手、锤子和卷尺，这三样东西。"
acts = wa.plan(lst, "", [], 0, (), [blk("扳手"), blk("锤子"), blk("卷尺")])
check("一句话里连着说几块：各亮各的", [a.control for a in acts] == [0, 1, 2] and [lst[a.pos] for a in acts] == ["扳", "锤", "卷"], [a.pos for a in acts])
acts = wa.plan("数据分析工具很好用。", "", [], 0, (), [blk("数据"), blk("数据分析工具")])
check("名字有长有短：长的优先，短的不抢同一处", [a.control for a in acts] == [1], [a.control for a in acts])
check("英文按整词：start 里的 art 不算", wa.plan("Let us start now.", "", [], 0, (), [blk("Art")]) == [], wa.block_phrases(blk("Art")))
check("英文整词：多个词的名字也认", [a.control for a in wa.plan("Now the Quick Mode, and art at the end.", "", [], 0, (), [blk("Quick Mode"), blk("Art")])] == [0, 1])
check("序号不算名字的一部分", wa.block_phrases(blk("01 任务列表")) == ["任务列表"], wa.block_phrases(blk("01 任务列表")))
check("文字开头的分句也算名字", "先看预算" in wa.block_phrases(WebBlock(key="99%", text="先看预算：不要一次花完")),
      wa.block_phrases(WebBlock(key="99%", text="先看预算：不要一次花完")))
st = Step(index=0, kind="slide", narration=n, web=WebPage(blocks=B))
st.web.actions = wa.plan(n, "", [], 0, (), B)
tl = wa.timeline(st, 0.5, 20.0)
check("秒数：跟着话走，亮到这句结束、最长不超过下一处", len(tl) == 2 and tl[0][0] >= 0.7 and tl[0][0] < tl[0][1].until <= tl[1][0] and tl[1][1].until <= 19.75,
      [(t, a.until) for t, a in tl])
check("关掉高亮：时间线里没有", wa.timeline(st, 0.5, 20.0, focus=False) == [])
data = st.web.actions[0].model_dump()
check("结束时间不存进项目", "until" not in data, sorted(data))
st.web.manual = False
st.web.plan_key = ""
st.slide_notes = ""
st.narration = "这一页只讲通讯录。"
wa.refresh(st)
check("改了解说：高亮自动重新安排", [(a.kind, a.control) for a in st.web.actions] == [("focus", 2)], [(a.kind, a.control) for a in st.web.actions])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
