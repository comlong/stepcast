"""Use the LLM to write the narration script from the recorded steps / to translate it (the provider is chosen in the settings)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .. import i18n
from ..models import DialogueLine, Project, Step, drop_stale_lines, join_lines
from .llm import ChatClient, LLMError, get_client

# One table for narration languages and second-language subtitles; its order is the order in the UI:
# Chinese → European languages (common ones first, the rest alphabetically by native name) → other languages. Each has free Edge voices
LANG_GROUPS = (
    ("zh", {"zh-CN": "简体中文", "zh-TW": "繁體中文"}),  # i18n: ignore
    ("europe", {
        "en-GB": "English (UK)", "en-US": "English (US)", "de-DE": "Deutsch", "fr-FR": "Français",
        "es-ES": "Español", "it-IT": "Italiano", "nl-NL": "Nederlands", "pl-PL": "Polski",
        "pt-PT": "Português (Portugal)",
        "bs-BA": "Bosanski", "ca-ES": "Català", "cs-CZ": "Čeština", "cy-GB": "Cymraeg", "da-DK": "Dansk",
        "et-EE": "Eesti", "ga-IE": "Gaeilge", "gl-ES": "Galego", "hr-HR": "Hrvatski", "is-IS": "Íslenska",
        "lv-LV": "Latviešu", "lt-LT": "Lietuvių", "hu-HU": "Magyar", "mt-MT": "Malti", "nb-NO": "Norsk (bokmål)",
        "ro-RO": "Română", "sq-AL": "Shqip", "sk-SK": "Slovenčina", "sl-SI": "Slovenščina", "fi-FI": "Suomi",
        "sv-SE": "Svenska", "tr-TR": "Türkçe",
        "el-GR": "Ελληνικά", "bg-BG": "Български", "mk-MK": "Македонски", "ru-RU": "Русский", "sr-RS": "Српски",
        "uk-UA": "Українська",
    }),
    ("other", {
        "ja-JP": "日本語", "ko-KR": "한국어", "pt-BR": "Português (Brasil)", "vi-VN": "Tiếng Việt",  # i18n: ignore
        "th-TH": "ไทย", "ar-SA": "العربية", "hi-IN": "हिन्दी",
    }),
)
LANG_NAMES = {code: name for _, names in LANG_GROUPS for code, name in names.items()}
LANG_GROUP = {code: group for group, names in LANG_GROUPS for code in names}

STYLE_HINT = {
    "friendly": "亲切自然，像同事在旁边手把手教你，可以用「我们」「接下来」这类口语连接词",  # i18n: ignore
    "concise": "极简高效，每步一句话说清做什么，不寒暄",  # i18n: ignore
    "formal": "正式专业，适合企业内部培训文档的旁白语气",  # i18n: ignore
    "marketing": "有感染力，突出这个功能带来的价值和好处",  # i18n: ignore
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def _short_url(url: str) -> str:
    try:
        u = urlparse(url)
        path = u.path if len(u.path) < 60 else u.path[:57] + "..."
        return f"{u.netloc}{path}"
    except Exception:
        return url[:80]


def step_digest(s: Step) -> Dict[str, Any]:
    """Compress one step into a compact description for the LLM."""
    t = s.target
    d: Dict[str, Any] = {
        "i": s.index,
        "action": s.kind,
        "page": s.page_title[:80],
        "url": _short_url(s.url),
    }
    if t:
        label = (t.text or t.name or "").strip()
        if label:
            d["element_text"] = label[:80]
        if t.role:
            d["element_role"] = t.role
        elif t.tag:
            d["element_tag"] = t.tag
        if t.input_type:
            d["input_type"] = t.input_type
    if s.kind == "input" and s.value:
        d["typed"] = s.value[:60]
    if s.kind == "key" and s.value:
        d["key"] = s.value
    if s.note:
        d["user_note"] = s.note[:200]
    if s.kind == "slide":
        d.pop("url", None)
        if s.slide_text:
            d["slide_text"] = s.slide_text[:600]
        if s.slide_notes:
            d["speaker_notes"] = s.slide_notes[:800]
    return d


SYSTEM = """你是一名专业的产品教学视频编剧。用户录制了一段浏览器操作，你要为每一步写旁白。
规则：
1. 只输出 JSON，不要任何解释文字。
2. narration 是要被念出来的旁白：口语化、连贯、能听懂；1~2 句，中文每步 15~45 字，英文 8~30 词。
3. title 是画面顶部的短标题：名词短语，不超过 12 个字 / 5 个词。
4. caption 是屏幕字幕，通常与 narration 相同；若 narration 太长可精简。
5. 不要编造界面上不存在的按钮或数据；不确定就用泛化描述。
6. 步骤之间要有衔接感（如「接着」「然后」「最后」），但不要每句都用。
7. 如果某步有 user_note，必须体现用户备注里的意图。
8. 涉及密码、密钥、邮箱等敏感值时，不要念出具体内容。"""  # i18n: ignore

USER_TMPL = """请为下面这段浏览器操作录制生成教学解说。  # i18n: ignore

输出语言：{lang}
解说风格：{style}
{audience}{extra}
共 {n} 个步骤，数据如下（JSON）：
{steps}

请严格按以下 JSON 结构输出：
{{
  "title": "整个视频的标题（不超过 20 字 / 8 词）",
  "subtitle": "一句副标题，说明看完能学会什么",
  "intro": "片头旁白，1~2 句，介绍这个教程要做什么",
  "outro": "片尾旁白，1 句，总结或提示下一步",
  "summary": "3~5 句的文字版操作摘要，用于文档",
  "steps": [
    {{"i": 0, "title": "...", "narration": "...", "caption": "..."}}
  ]
}}
steps 数组必须包含全部 {n} 个步骤，i 与输入的 i 一一对应。"""


def generate_script(
    proj: Project,
    style: str = "friendly",
    audience: str = "",
    extra: str = "",
    overwrite: bool = False,
    progress: Optional[Callable[[float, str], None]] = None,
    client: Optional[ChatClient] = None,
) -> Dict[str, Any]:
    """Write / complete the narration script, directly into proj (the caller saves it)."""
    if proj.source == "slides":
        ps = proj.settings or {}
        return generate_slides_script(
            proj, notes_mode=ps.get("slides_notes_mode", "verbatim"),
            detail=ps.get("slides_detail", "standard"), overwrite=overwrite,
            extra=extra, missing=ps.get("slides_missing", "ai"),
            progress=progress, client=client)
    # video steps aren't sent to the AI: they either play their own sound or you write the narration yourself
    steps = [s for s in proj.steps if s.include and s.kind != "video"]
    if not steps:
        raise LLMError(i18n.t("没有可用的步骤，请先录制。"))
    # Not overwriting and every step already has narration: no need to ask the LLM.
    # After stopping "Generate all" halfway and editing, clicking again continues right away without waiting for generation again
    if not overwrite and proj.title and all((s.narration or "").strip() for s in steps):
        if progress:
            progress(1.0, i18n.t("每一步都已有解说，跳过生成"))
        return {"updated": 0, "title": proj.title, "summary": proj.summary, "skipped": True}
    client = client or get_client()

    lang = lang_name(proj.language)
    style_txt = STYLE_HINT.get(style, style)
    audience_txt = f"目标观众：{audience}\n" if audience else ""  # i18n: ignore
    extra_txt = f"额外要求：{extra}\n" if extra else ""  # i18n: ignore

    BATCH = 20
    batches = [steps[i:i + BATCH] for i in range(0, len(steps), BATCH)]
    head: Dict[str, Any] = {}
    all_steps: Dict[int, Dict[str, str]] = {}

    for bi, batch in enumerate(batches):
        if progress:
            progress(bi / max(1, len(batches)),
                     i18n.t("生成解说 {start}-{end} / {total}", start=bi * BATCH + 1,
                            end=bi * BATCH + len(batch), total=len(steps)))
        digests = [step_digest(s) for s in batch]
        ctx = ""
        if bi > 0 and head:
            ctx = ("\n（这是同一个视频的后续步骤，视频标题已定为「"  # i18n: ignore
                   + str(head.get("title", "")) + "」，请保持语气连贯，不要重复片头介绍。）\n")  # i18n: ignore
        prompt = USER_TMPL.format(
            lang=lang, style=style_txt, audience=audience_txt,
            extra=extra_txt + ctx, n=len(batch),
            steps=json.dumps(digests, ensure_ascii=False, indent=1),
        )
        data = client.chat_json([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ], temperature=0.7)

        if bi == 0:
            head = data
        for item in data.get("steps", []) or []:
            try:
                idx = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            all_steps[idx] = {
                "title": str(item.get("title", "") or "").strip(),
                "narration": str(item.get("narration", "") or "").strip(),
                "caption": str(item.get("caption", "") or "").strip(),
            }

    if overwrite or not proj.title:
        proj.title = head.get("title", proj.title) or proj.name
    proj.subtitle = head.get("subtitle", proj.subtitle) or proj.subtitle
    if overwrite or not proj.intro:
        proj.intro = head.get("intro", "") or proj.intro
    if overwrite or not proj.outro:
        proj.outro = head.get("outro", "") or proj.outro
    proj.summary = head.get("summary", proj.summary) or proj.summary

    written = 0
    for s in proj.steps:
        item = all_steps.get(s.index)
        if not item:
            continue
        if s.voice_source == "own":
            s.title = s.title or item["title"]   # the recording is your own voice: only fill in the title, keep the text
            continue
        if s.narration and not overwrite:
            continue
        s.title = item["title"] or s.title
        s.narration = item["narration"] or s.narration
        drop_stale_lines(s)
        s.caption = item["caption"] or item["narration"] or s.caption
        s.audio = ""          # the narration changed, the old voice-over is outdated
        s.audio_duration = 0.0
        s.boundaries = []
        written += 1

    if progress:
        progress(1.0, i18n.t("完成，已生成 {n} 条解说", n=written))
    return {"updated": written, "title": proj.title, "summary": proj.summary}


DETAIL_HINT = {
    "brief": "简要：每页 1~2 句，只讲核心要点",  # i18n: ignore
    "standard": "标准：每页 2~4 句，讲清要点并补一点上下文",  # i18n: ignore
    "comprehensive": "详细：每页 4~7 句，完整解释页面内容，像讲师在逐条讲解",  # i18n: ignore
}

SLIDES_SYSTEM = """你是一名培训讲师，要把一份幻灯片讲成配音视频。你会拿到每页的标题、正文和演讲者备注。
规则：
1. 只输出 JSON。
2. narration 是讲师旁白，口语化、连贯，不要念「这一页」「如图所示」这类话，不要逐字朗读要点列表，要把要点讲成人话。
3. 有演讲者备注（notes）时，以备注的意图和信息为主，页面正文只作补充；没有备注时根据标题和正文讲解。
4. 不要编造页面和备注里都没有的数据、结论。
5. 页与页之间要有自然衔接。
6. title 是这一页的短标题（不超过 12 字 / 5 词），优先用幻灯片原标题。
7. 给了 content_items 时（页面上的内容块，已经按阅读顺序排好，视频里会随讲解一条条出现）：
   按这个顺序讲，讲到每一块时点出它的关键词，让观众能对上正在出现的那一块。
8. 标了 "notes_are_script": true 的页：备注就是讲稿，只是语言和输出语言不同。用输出语言把备注完整、忠实地讲出来
   （像口译一样，口语化但不删减、不概括、不另加内容，也不受详细程度限制），第 2、3 条对这种页不适用。"""  # i18n: ignore

SLIDES_TMPL = """请为下面这份幻灯片写配音旁白。  # i18n: ignore

输出语言：{lang}
详细程度：{detail}
{extra}
共 {n} 页（JSON）：
{slides}

严格按以下 JSON 输出：
{{
  "title": "整个视频标题（不超过 20 字 / 8 词）",
  "subtitle": "一句副标题",
  "intro": "片头旁白，1~2 句",
  "outro": "片尾旁白，1 句",
  "summary": "3~5 句文字摘要",
  "steps": [{{"i": 0, "title": "...", "narration": "..."}}]
}}
steps 必须覆盖输入的全部 i。"""


def generate_slides_script(
    proj: Project,
    notes_mode: str = "reference",
    detail: str = "standard",
    overwrite: bool = True,
    extra: str = "",
    missing: str = "ai",
    progress: Optional[Callable[[float, str], None]] = None,
    client: Optional[ChatClient] = None,
) -> Dict[str, Any]:
    """Narration for slides.

    notes_mode: verbatim = the notes are the narration | reference = AI rewrites from the notes | ignore = don't use the notes
    missing (verbatim only): slides without notes: ai = the AI writes from the slide content | empty = leave empty, fill in by hand

    In verbatim mode only slides "without notes and with AI fill-in" (or with notes in another language) are sent to the LLM; everything else stays offline.
    """
    if proj.is_dialogue():
        return generate_dialogue_script(proj, notes_mode=notes_mode, detail=detail, overwrite=overwrite,
                                        extra=extra, progress=progress, client=client)
    steps = [s for s in proj.steps if s.include and s.voice_source != "own" and s.kind != "video"]
    if not overwrite:
        steps = [s for s in steps if not (s.narration or "").strip()]

    from .langdetect import same_language
    verbatim_done = 0
    left_empty = 0
    other_lang = 0
    need_ai: List[Step] = []
    as_script: set[str] = set()           # the notes are the script, just in another language: the AI tells them in the narration language without rewriting
    for s in steps:
        if notes_mode == "verbatim" and not s.slide_notes.strip() and missing == "empty":
            left_empty += 1               # no notes and no AI wanted: leave it, to be filled in in the editor
            continue
        if notes_mode == "verbatim" and s.slide_notes.strip() and not same_language(s.slide_notes, proj.language):
            # notes and narration are in different languages (e.g. an English deck for a Chinese video): can't be read as they are, so the AI writes them in the narration language
            other_lang += 1
            need_ai.append(s)
            as_script.add(s.id)
            continue
        if notes_mode == "verbatim" and s.slide_notes.strip():
            # line breaks in the notes are just layout; join them for reading: directly for Chinese / Japanese, with spaces otherwise (judged by the notes' own script)
            cjk = len(re.findall(r"[぀-ヿ一-鿿]", s.slide_notes))  # i18n: ignore
            joiner = "" if cjk > len(re.findall(r"[A-Za-z]", s.slide_notes)) else " "
            text = re.sub(r"\s*\n\s*", joiner, s.slide_notes.strip())
            s.narration = text
            s.caption = text
            drop_stale_lines(s)
            s.title = s.title or s.page_title[:24]
            s.audio, s.audio_duration, s.boundaries = "", 0.0, []
            verbatim_done += 1
        else:
            need_ai.append(s)

    head: Dict[str, Any] = {}
    ai_done = 0
    # only go online if some slide really needs the AI; verbatim with notes on every slide = fully offline
    if need_ai:
        client = client or get_client()
        lang = lang_name(proj.language)
        BATCH = 25
        batches = [need_ai[i:i + BATCH] for i in range(0, len(need_ai), BATCH)]
        for bi, batch in enumerate(batches):
            if progress:
                progress(bi / max(1, len(batches)), i18n.t("AI 生成幻灯片解说 {i}/{n}", i=bi + 1, n=len(batches)))
            payload = []
            for s in batch:
                d: Dict[str, Any] = {"i": s.index, "slide_title": s.page_title[:120],
                                     "slide_text": s.slide_text[:1500]}
                if notes_mode != "ignore" and s.slide_notes:
                    d["notes"] = s.slide_notes[:3000]
                if s.id in as_script:
                    d["notes_are_script"] = True
                items = [it.text.replace("\r", " ").replace("\n", " ")[:120]
                         for it in ((s.reveal.items if s.reveal and s.reveal.enabled else []) or []) if it.text.strip()]
                if len(items) >= 2:
                    d["content_items"] = items
                payload.append(d)
            prompt = SLIDES_TMPL.format(
                lang=lang, detail=DETAIL_HINT.get(detail, detail),
                extra=f"额外要求：{extra}\n" if extra else "",  # i18n: ignore
                n=len(batch), slides=json.dumps(payload, ensure_ascii=False, indent=1))
            data = client.chat_json([
                {"role": "system", "content": SLIDES_SYSTEM},
                {"role": "user", "content": prompt},
            ], temperature=0.6)
            if bi == 0:
                head = data
            by_i = {}
            for it in data.get("steps", []) or []:
                try:
                    by_i[int(it.get("i"))] = it
                except (TypeError, ValueError):
                    continue
            for s in batch:
                it = by_i.get(s.index)
                if not it or s not in need_ai:
                    continue
                s.title = str(it.get("title") or s.title or s.page_title[:24]).strip()
                s.narration = str(it.get("narration") or "").strip()
                s.caption = s.narration
                drop_stale_lines(s)
                s.audio, s.audio_duration, s.boundaries = "", 0.0, []
                ai_done += 1
    first = next((s for s in proj.steps if s.include), None)
    if head:
        proj.title = head.get("title") or proj.title
        proj.subtitle = head.get("subtitle") or proj.subtitle
        proj.intro = head.get("intro") or proj.intro
        proj.outro = head.get("outro") or proj.outro
        proj.summary = head.get("summary") or proj.summary
    elif not proj.title and first:
        proj.title = first.page_title[:40] or proj.name

    if progress:
        if left_empty:
            msg = i18n.t("完成：{verbatim} 页用备注原文，{ai} 页由 AI 生成，{empty} 页没有备注已留空",
                         verbatim=verbatim_done, ai=ai_done, empty=left_empty)
        else:
            msg = i18n.t("完成：{verbatim} 页用备注原文，{ai} 页由 AI 生成", verbatim=verbatim_done, ai=ai_done)
        if other_lang:
            msg += " " + i18n.t("（其中 {n} 页的备注和解说不是同一种语言，由 AI 参考备注用{language}写）",
                                n=other_lang, language=lang_name(proj.language))
        progress(1.0, msg)
    return {"verbatim": verbatim_done, "ai": ai_done, "empty": left_empty, "other_lang": other_lang,
            "title": proj.title}


DIALOGUE_DETAIL = {
    "brief": "简要：只讲核心要点。内容页整页台词合计约 120~200 字（中文、日文）或 80~130 个词（其他语言）",  # i18n: ignore
    "standard": ("标准：讲清每个要点并补充上下文，讲师讲的内容不少于一个人单独讲这一页。"  # i18n: ignore
                 "内容页整页台词合计约 220~380 字（中文、日文）或 150~250 个词（其他语言）"),  # i18n: ignore
    "comprehensive": ("详细：像讲师在课堂上展开讲，逐条讲透，备注里的信息全部讲到。"  # i18n: ignore
                      "内容页整页台词合计约 380~650 字（中文、日文）或 250~420 个词（其他语言）"),  # i18n: ignore
}

DIALOGUE_SYSTEM = """你要把一份幻灯片写成两个人对话讲解的视频台词：一位主持人（host，女声）和一位讲师（expert，男声）。
你会拿到每页的标题、正文、演讲者备注（notes），有时还有页面上的内容块（content_items）。

内容：
1. 台词完全依据页面内容（标题、正文、备注）；不要编造页面和备注里都没有的数据、结论。
2. 不要因为是对话就压缩内容：备注和页面上的要点都要讲到，讲师讲的信息量不少于一个人单独讲这一页；主持人的话是额外加的。篇幅按「详细程度」来。
3. 给了 content_items 时（页面上的内容块，已按阅读顺序排好，视频里会随讲解一块块出现）：按这个顺序一块一块地讲，每块至少用一两句单独讲到并点出它的关键词，讲完一块再讲下一块；不要一句话把几块一带而过。
4. 封面、过渡页、只有一张图的页可以短，两三句即可。

风格（最重要）——像两个熟悉的同事在录一档轻松的培训播客，而不是一问一答的考试：
5. 主持人站在学员的角度：会接着讲师刚说的话往下问，会用自己的话复述、小结，会提出学员常见的疑问或误解，偶尔说出自己的第一感受；每句都短（一般不超过 25 字 / 15 个词）。
6. 讲师先直接回应，再展开；可以举例子、打比方、给实用建议，偶尔反问主持人；语气自然（「其实」「说白了」「举个例子」这类口头连接词适量用）。
7. 节奏要有变化：不要每轮都是「主持人一个问题 + 讲师一段回答」；可以讲师连着讲两句、主持人插一句追问，也可以主持人先抛出一个观点让讲师接着补充。一句台词只说一两句话，长内容拆成几句。
8. 不要每句都「好问题」「没错」「对」开头，不要播音腔和书面语，不要每页都用「那……是什么？」开头；新的一页从上一页的话题自然接过来。
9. 不要说「这一页」「如图所示」，不要逐字念要点列表，要把要点讲成人话。

格式：
10. 只输出 JSON。text 里只写这句话本身：不要写「主持人：」「讲师：」这样的说话人前缀，也不要称呼对方的名字或称谓（观众听不到名字）。
11. 整个视频的第一页（position 为 first）由主持人简单开场、引出主题，最后一页（position 为 last）由主持人收尾；其余页不要重新打招呼。
12. title 是这一页的短标题（不超过 12 字 / 5 词），优先用幻灯片原标题。intro / outro 是片头、片尾主持人一个人说的话，同样不带名字。

风格示例（只示意说话的感觉，内容不要照抄）：
host: 说到前脸，我第一眼注意到的就是这对大灯。
expert: 对，它是整个前脸的视觉焦点。不过它不只是好看。
expert: 大灯下面这道导流槽，其实是在帮车头引导气流。
host: 也就是说，造型本身就在做空气动力学？
expert: 没错。跟客户介绍时，可以先让他看这条线，再告诉他背后的功能。"""  # i18n: ignore

DIALOGUE_TMPL = """请为下面这份幻灯片写两人对话的讲解台词。  # i18n: ignore

输出语言：{lang}
详细程度：{detail}
{extra}
共 {n} 页（JSON）：
{slides}

严格按以下 JSON 输出：
{{
  "title": "整个视频标题（不超过 20 字 / 8 词）",
  "subtitle": "一句副标题",
  "intro": "片头主持人说的话，1~2 句",
  "outro": "片尾主持人说的话，1 句",
  "summary": "3~5 句文字摘要",
  "steps": [{{"i": 0, "title": "...", "lines": [{{"who": "host", "text": "..."}}, {{"who": "expert", "text": "..."}}]}}]
}}
steps 必须覆盖输入的全部 i；who 只能是 host 或 expert；text 里不要写名字和说话人前缀。"""  # i18n: ignore


def _slide_payload(s: Step, notes_mode: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {"i": s.index, "slide_title": s.page_title[:120], "slide_text": s.slide_text[:1500]}
    if notes_mode != "ignore" and s.slide_notes:
        d["notes"] = s.slide_notes[:3000]
    items = [it.text.replace("\r", " ").replace("\n", " ")[:120]
             for it in ((s.reveal.items if s.reveal and s.reveal.enabled else []) or []) if it.text.strip()]
    if len(items) >= 2:
        d["content_items"] = items
    return d


def set_lines(s: Step, lines: List[Dict[str, str]]) -> None:
    """Write one step's dialogue lines: the narration is the lines joined, the subtitle follows the narration; the old voice-over is outdated."""
    s.lines = [DialogueLine(who=x["who"], text=x["text"]) for x in lines]
    s.narration = join_lines(s.lines)
    if s.caption_follows_narration():
        s.caption = s.narration
    s.audio, s.audio_duration, s.boundaries, s.line_times = "", 0.0, [], []


def generate_dialogue_script(
    proj: Project,
    notes_mode: str = "reference",
    detail: str = "standard",
    overwrite: bool = True,
    extra: str = "",
    progress: Optional[Callable[[float, str], None]] = None,
    client: Optional[ChatClient] = None,
) -> Dict[str, Any]:
    """Two-person Q&A: each slide becomes a few lines where the host asks and the expert explains. Verbatim notes are treated as "reference" here
    (the notes are one person's script and can't be split into a two-person dialogue as they are)."""
    from . import dialogue
    notes_mode = "reference" if notes_mode == "verbatim" else notes_mode
    steps = [s for s in proj.steps if s.include and s.voice_source != "own" and s.kind != "video"]
    if not overwrite:
        steps = [s for s in steps if not (s.narration or "").strip()]
    head: Dict[str, Any] = {}
    done = 0
    if steps:
        client = client or get_client()
        lang = lang_name(proj.language)
        dialogue.ensure_speakers(proj)
        names = dialogue.speaker_names(proj)        # the AI isn't told the names; names and prefixes it adds anyway are removed here
        BATCH = 5                                   # dialogue is longer than single narration, so fewer slides per request, so the AI doesn't compress content to fit
        prev_tail: List[str] = []
        batches = [steps[i:i + BATCH] for i in range(0, len(steps), BATCH)]
        first_id = next((s.id for s in proj.steps if s.include and s.kind != "video"), "")
        last_id = next((s.id for s in reversed(proj.steps) if s.include and s.kind != "video"), "")
        for bi, batch in enumerate(batches):
            if progress:
                progress(bi / max(1, len(batches)), i18n.t("AI 生成双人问答台词 {i}/{n}", i=bi + 1, n=len(batches)))
            payload = []
            for s in batch:
                d = _slide_payload(s, notes_mode)
                if s.id == first_id:
                    d["position"] = "first"      # first slide of the whole video: the host opens
                if s.id == last_id:
                    d["position"] = "last"       # last slide: the host wraps up
                payload.append(d)
            ctx = extra and f"额外要求：{extra}\n" or ""  # i18n: ignore
            if bi > 0:
                ctx += "（这是同一个视频的后续页，前面已经开过场，接着往下讲，不要重新打招呼。）\n"  # i18n: ignore
            if prev_tail:
                ctx += "上一页最后几句（接着它自然往下讲）：\n" + "\n".join(prev_tail) + "\n"  # i18n: ignore
            prompt = DIALOGUE_TMPL.format(
                lang=lang, detail=DIALOGUE_DETAIL.get(detail, detail), extra=ctx, n=len(batch),
                slides=json.dumps(payload, ensure_ascii=False, indent=1))
            data = client.chat_json([
                {"role": "system", "content": DIALOGUE_SYSTEM},
                {"role": "user", "content": prompt},
            ], temperature=0.8)
            if bi == 0:
                head = data if isinstance(data, dict) else {}
            by_i = {}
            for it in (data.get("steps") or []) if isinstance(data, dict) else []:
                try:
                    by_i[int(it.get("i"))] = it
                except (TypeError, ValueError, AttributeError):
                    continue
            for s in batch:
                it = by_i.get(s.index)
                lines = dialogue.normalize_lines(it.get("lines"), names, clean=True) if it else []
                if not lines:
                    continue
                s.title = str(it.get("title") or s.title or s.page_title[:24]).strip()
                set_lines(s, lines)
                prev_tail = [f"{x['who']}: {x['text']}" for x in lines[-2:]]
                done += 1
    first = next((s for s in proj.steps if s.include), None)
    if head:
        def clean(x: Any) -> str:
            return dialogue.clean_text(str(x or ""), dialogue.speaker_names(proj))
        proj.title = head.get("title") or proj.title
        proj.subtitle = head.get("subtitle") or proj.subtitle
        proj.intro = clean(head.get("intro")) or proj.intro
        proj.outro = clean(head.get("outro")) or proj.outro
        proj.summary = head.get("summary") or proj.summary
    elif not proj.title and first:
        proj.title = first.page_title[:40] or proj.name
    if progress:
        progress(1.0, i18n.t("完成：{n} 页写成了双人问答", n=done))
    return {"verbatim": 0, "ai": done, "empty": 0, "title": proj.title}


TRANSLATE_SYSTEM = """你是专业的本地化译员，负责把教学视频旁白翻译成目标语言。
规则：保持口语化和简洁；界面上的按钮名保留原文（可在括号里给译文）；只输出 JSON。"""  # i18n: ignore


def _translate_items(proj: Project, steps: Optional[List[Step]] = None, card: bool = True) -> List[Dict[str, Any]]:
    """Texts to translate: title / intro and outro (card) and these steps' titles and narration (subtitles for videos with their own sound)."""
    items: List[Dict[str, Any]] = []
    if card:
        items += [{"k": "title", "t": proj.title}, {"k": "subtitle", "t": proj.subtitle},
                  {"k": "intro", "t": proj.intro}, {"k": "outro", "t": proj.outro}]
    for s in proj.steps if steps is None else steps:
        # consistent with writing narration: steps without "Include in video" are never sent
        if s.include and s.lines:
            items.append({"k": f"s{s.index}:title", "t": s.title})
            items += [{"k": f"s{s.index}:line{j}", "t": ln.text} for j, ln in enumerate(s.lines)]
        elif s.include and (s.narration or s.title):
            items.append({"k": f"s{s.index}:title", "t": s.title})
            items.append({"k": f"s{s.index}:narration", "t": s.narration})
        if s.include and s.plays_clip_audio() and s.caption:
            # steps playing the video's own sound: the sound stays, the subtitle (what is said in the video) is translated
            items.append({"k": f"s{s.index}:caption", "t": s.caption})
    return [x for x in items if (x["t"] or "").strip()]


def _run_translation(client: ChatClient, items: List[Dict[str, Any]], target: str,
                     progress: Optional[Callable[[float, str], None]] = None) -> Dict[str, str]:
    BATCH = 40
    result: Dict[str, str] = {}
    chunks = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    for ci, chunk in enumerate(chunks):
        if progress:
            progress(ci / max(1, len(chunks)), i18n.t("翻译中 {i}/{n}", i=ci + 1, n=len(chunks)))
        prompt = (
            "把下面每条文本翻译成 " + target + "。\n"  # i18n: ignore
            '输出 JSON：{"items":[{"k":"原样的k","t":"译文"}]}，条数必须一致。\n\n'  # i18n: ignore
            + json.dumps(chunk, ensure_ascii=False, indent=1)
        )
        data = client.chat_json([
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
        for it in data.get("items", []) or []:
            k = it.get("k")
            if k:
                result[str(k)] = str(it.get("t", "") or "")
    return result


def _apply_translation(proj: Project, result: Dict[str, str]) -> None:
    proj.title = result.get("title", proj.title)
    proj.subtitle = result.get("subtitle", proj.subtitle)
    proj.intro = result.get("intro", proj.intro)
    proj.outro = result.get("outro", proj.outro)
    for s in proj.steps:
        if s.lines and any(f"s{s.index}:line{j}" in result for j in range(len(s.lines))):
            set_lines(s, [{"who": ln.who, "text": result.get(f"s{s.index}:line{j}") or ln.text}
                          for j, ln in enumerate(s.lines)])
            s.voice_source = "tts"
        nt = None if s.lines else result.get(f"s{s.index}:narration")
        tt = result.get(f"s{s.index}:title")
        if nt:
            s.narration = nt
            if s.caption_follows_narration():
                s.caption = nt
            s.audio = ""
            s.voice_source = "tts"       # the recording is in the old language; after translation the AI reads the text
            s.audio_duration = 0.0
            s.boundaries = []
        if tt:
            s.title = tt
        ct = result.get(f"s{s.index}:caption")
        if ct and s.plays_clip_audio():
            s.caption = ct


def translate_project(
    proj: Project,
    target_language: str,
    apply: bool = True,
    progress: Optional[Callable[[float, str], None]] = None,
    client: Optional[ChatClient] = None,
) -> Dict[str, Any]:
    """Translate the title / intro and outro / every step's narration into the target language."""
    client = client or get_client()
    target = lang_name(target_language)
    items = _translate_items(proj)
    if not items:
        raise LLMError(i18n.t("没有可翻译的文本，请先生成解说脚本。"))
    result = _run_translation(client, items, target, progress)
    proj.translations[target_language] = result
    if apply:
        proj.language = target_language
        _apply_translation(proj, result)
    if progress:
        progress(1.0, i18n.t("翻译完成（{language}）", language=target))
    return {"language": target_language, "count": len(result), "applied": apply}


def switch_language(
    proj: Project,
    target_language: str,
    progress: Optional[Callable[[float, str], None]] = None,
    client: Optional[ChatClient] = None,
    voice: str = "",
) -> Dict[str, Any]:
    """Switch the whole project to another language ("translate and switch").

    Slide projects: slide narration isn't translated but rewritten in the target language from the deck (slide text + speaker notes),
    together with the title, intro and outro; inserted video steps have no deck text and are translated as usual. Recorded projects: translated as a whole."""
    if proj.is_dialogue():
        from . import dialogue
        dialogue.retarget(proj, target_language, voice)
    slides = [s for s in proj.steps if s.include and s.kind != "video"] if proj.source == "slides" else []
    if not slides:
        return translate_project(proj, target_language, apply=True, progress=progress, client=client)
    client = client or get_client()
    target = lang_name(target_language)
    ps = proj.settings or {}
    # switching language in verbatim mode uses "AI rewrites from the notes": the notes are in the old language and can't be read as they are
    notes_mode = ps.get("slides_notes_mode", "verbatim")
    notes_mode = "reference" if notes_mode == "verbatim" else notes_mode
    before = {s.id: s.narration for s in slides}
    card_before = (proj.title, proj.subtitle, proj.intro, proj.outro)
    for s in slides:
        if s.voice_source == "own":
            s.voice_source, s.audio, s.audio_duration, s.boundaries = "tts", "", 0.0, []   # your own recording is in the old language, switch to the AI voice
    proj.language = target_language

    def sub(lo: float, hi: float):
        return (lambda f, m: progress(lo + f * (hi - lo), m)) if progress else None
    res = generate_slides_script(proj, notes_mode=notes_mode, detail=ps.get("slides_detail", "standard"),
                                 overwrite=True, missing="ai", progress=sub(0.0, 0.85), client=client)
    # what's left without deck text: video steps, plus slides and titles the AI missed
    rest = [s for s in proj.steps if s.include and (s.kind == "video" or
                                                    (s.id in before and s.narration == before[s.id]))]
    card_left = (proj.title, proj.subtitle, proj.intro, proj.outro) == card_before
    items = _translate_items(proj, rest, card=card_left)
    result: Dict[str, str] = _run_translation(client, items, target, sub(0.85, 0.99)) if items else {}
    _apply_translation(proj, result)
    proj.translations[target_language] = {
        "title": proj.title, "subtitle": proj.subtitle, "intro": proj.intro, "outro": proj.outro,
        **{f"s{s.index}:{k}": getattr(s, k) for s in proj.steps if s.include for k in ("title", "narration")}}
    if progress:
        progress(1.0, i18n.t("已按 PPT 原文用{language}重写 {n} 页解说", language=target, n=res.get("ai", 0))
                 + (i18n.t("，另外翻译了 {n} 个视频步骤", n=sum(1 for s in rest if s.kind == "video"))
                    if any(s.kind == "video" for s in rest) else ""))
    return {"language": target_language, "rewritten": res.get("ai", 0), "translated": len(result),
            "applied": True, "mode": "slides"}


REWRITE_SYSTEM = "你是教学视频编剧，按用户要求改写这一句旁白。只输出改写后的文本，不要引号和解释。"  # i18n: ignore


REWRITE_DIALOGUE_SYSTEM = ("你是教学视频编剧，按用户要求改写这一页两个人（host 主持人，expert 讲师）的对话台词。"  # i18n: ignore
                           "像两个同事自然地聊，不要一问一答的考试腔；台词依据页面内容，不编造；"  # i18n: ignore
                           "text 里不要写说话人前缀，也不要称呼对方的名字。"  # i18n: ignore
                           '只输出 JSON：{"lines": [{"who": "host 或 expert", "text": "..."}]}')  # i18n: ignore


def rewrite_dialogue(proj: Project, step: Step, instruction: str,
                     client: Optional[ChatClient] = None) -> List[Dict[str, str]]:
    """Rewrite one slide's Q&A dialogue as instructed ("AI rewrite" in Q&A projects)."""
    from . import dialogue
    client = client or get_client()
    dialogue.ensure_speakers(proj)
    notes_mode = (proj.settings or {}).get("slides_notes_mode", "reference")
    notes_mode = "ignore" if notes_mode == "ignore" else "reference"
    prompt = (
        f"视频标题：{proj.title}\n输出语言：{lang_name(proj.language)}\n"  # i18n: ignore
        f"这一页的内容：{json.dumps(_slide_payload(step, notes_mode), ensure_ascii=False)}\n"  # i18n: ignore
        f"当前台词：{json.dumps([{'who': ln.who, 'text': ln.text} for ln in step.lines], ensure_ascii=False)}\n\n"  # i18n: ignore
        f"改写要求：{instruction}"  # i18n: ignore
    )
    data = client.chat_json([
        {"role": "system", "content": REWRITE_DIALOGUE_SYSTEM},
        {"role": "user", "content": prompt},
    ], temperature=0.7)
    lines = dialogue.normalize_lines(data.get("lines") if isinstance(data, dict) else None,
                                     dialogue.speaker_names(proj), clean=True)
    if not lines:
        raise LLMError(i18n.t("AI 没有返回台词，请再试一次"))
    return lines


def rewrite_step(proj: Project, step: Step, instruction: str,
                 client: Optional[ChatClient] = None) -> str:
    """Rewrite one step's narration as instructed (the "AI rewrite" button)."""
    client = client or get_client()
    ctx = json.dumps(step_digest(step), ensure_ascii=False)
    prompt = (
        f"视频标题：{proj.title}\n输出语言：{lang_name(proj.language)}\n"  # i18n: ignore
        f"这一步的操作数据：{ctx}\n当前旁白：{step.narration or '（空）'}\n\n"  # i18n: ignore
        f"改写要求：{instruction}"  # i18n: ignore
    )
    txt = client.chat([
        {"role": "system", "content": REWRITE_SYSTEM},
        {"role": "user", "content": prompt},
    ], temperature=0.7, max_tokens=500)
    return txt.strip().strip('"').strip()
