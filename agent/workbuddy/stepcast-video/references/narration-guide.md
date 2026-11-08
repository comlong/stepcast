# Writing narration for StepCast videos

The narration is spoken by a synthetic voice and shown as burned-in subtitles. Write for the ear.

## Structure of a good tutorial

1. **Intro** (project `intro`): one or two sentences — what the viewer will be able to do after the video.
2. **Background** (slide page, optional): why the process exists, where it sits in the bigger picture, who does it.
3. **Principles** (1–3 slide pages, optional): the rules the viewer must understand before clicking — states, roles, what depends on what. One idea per page, 4–7 short lines.
4. **Walkthrough** (recorded steps): one step = one action. Title = the action ("Select the month"), narration = what to do and the one thing to watch out for.
5. **Common problems** (slide page, optional): symptom → cause → fix, 4–6 lines.
6. **Outro** (project `outro`): one sentence wrap-up.

## Step narration

- 1–3 sentences, roughly 15–60 Chinese characters or 10–40 English words. The step lasts as long as its voice-over.
- Name the button or field exactly as on screen, in the screen's language: 点「提交」, click **Save**.
- Say *why* or *what happens next* when it isn't obvious: "提交后门店记录直接完成，不走审批".
- Don't describe what the viewer can already see ("Now we see a list").
- Consecutive steps should read as a story; vary sentence openings.
- Steps that only open a menu can be hidden (`include false`) or merged into the next step's narration.

## Slides (PPT / PDF)

- One slide = one step. Its narration explains the slide in 2–5 spoken sentences; it doesn't read out the bullet points or the title.
- Start from `slide_text` (what the viewer sees) and `notes` (what the author meant to say). Notes written as keywords or reminders are material, not narration: turn them into sentences.
- Notes in another language than the video: translate them into the narration language, keeping product and department names.
- Link the slides: the first sentence of a slide may say how it follows from the previous one ("有了这三个阶段，再看每个阶段由谁负责").
- Charts and diagrams: say what the viewer should take from them (the trend, the bottleneck), not every number.
- Cover and agenda slides get one or two sentences; a closing slide can carry the outro instead.
- When the user asks for the notes "as they are", keep them, and only fix what a voice reads badly (abbreviations, symbols, numbers).

## Background / principle pages

- Draw them with `slide`: a title and short lines. Lines starting with spaces are indented sub-points; `·` lines are dimmed.
- The narration of a page explains the lines in full sentences; it does not read them out word by word.
- Stay factual. If you are not sure how the business process works, ask the user instead of inventing it.

## For the voice

- Write numbers and symbols the way they should be spoken when it matters: 「三加三」 instead of "3+3", 「二零二六年十二月」 or "December 2026".
- Avoid abbreviations the voice may spell out oddly; spell them out the first time.
- No emoji, no markdown, no bullet characters in narration.
- Keep subtitles readable: the `caption` defaults to the narration; set a shorter caption only when the narration is long.

## Language

- Write in the project's language (`language`, e.g. zh-CN). The voice must match (zh-CN-XiaoxiaoNeural, en-US-AriaNeural …).
- Second-language subtitles are translations of the finished narration, sentence by sentence (see SKILL.md).
