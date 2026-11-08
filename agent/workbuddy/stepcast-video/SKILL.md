---
name: stepcast-video
display_name: StepCast 教学视频
display_name_en: StepCast Tutorial Videos
description: This skill should be used when the user wants a narrated tutorial, training or explainer video (1080p MP4, voice-over, burned-in subtitles) made with the StepCast app on this computer, from a StepCast browser recording, from a PowerPoint / PDF deck or from an HTML (web page) deck, or wants such a video's narration rewritten, a background page added, or second-language subtitles. StepCast records, highlights, voices and renders; the assistant writes every text and drives StepCast through its local API with scripts/stepcast.ps1. 触发词：教学视频、培训视频、讲解视频、操作演示视频、录屏做视频、PPT 转视频、课件做成视频、网页演示稿做成视频、HTML 讲稿转视频、StepCast、配音、字幕、第二语言字幕。
description_zh: 用本机的 StepCast 把浏览器录制、PPT / PDF 或网页演示稿（HTML）做成带配音和字幕的 1080p 教学视频：解说词、背景页和翻译由助手来写，StepCast 负责截图高亮、录下网页动画和点击、免费配音和渲染。
description_en: Turns StepCast browser recordings, PowerPoint / PDF decks or HTML decks into 1080p tutorial videos with voice-over and subtitles. The assistant writes narration, background pages and translations; StepCast highlights, records web animations and clicks, voices (free Edge voices) and renders locally.
category: video
version: 1.10.0
---

# StepCast tutorial videos

StepCast is a local app (service at http://127.0.0.1:8756). This skill brings StepCast's program with it and installs it on first use into a work folder the user picks; a StepCast.exe the user already runs works as well. The work is split:

| StepCast does | You do |
|---|---|
| recording (Chrome extension), PPT / PDF / HTML import (slide pictures, speaker notes, PowerPoint animations; an HTML deck's own animations and clicks recorded moving), click highlight + zoom, voice-over (free Edge voices), rendering, subtitle timing | all writing: which steps or slides to keep, titles, narration, intro / outro, background and principle pages, second-language translations |

**StepCast's own AI stays off.** Never call `/script`, `/auto`, `/rewrite`, `/translate`, `POST /subtitles2`, `/notes/narration`, `/voice/ai`, `/voice/all-ai`, `/magic-mic`, and create slide projects only with `notes_mode: "verbatim"` + `missing: "empty"`. The script's `import` command does exactly that. Whatever those endpoints do, you do yourself and write back.

Reply to the user in their language. Say what you are about to do, and where the video is when it's done.

## Running the tool

All calls go through one script (Windows PowerShell 5.1, nothing to install):

```
powershell -NoProfile -ExecutionPolicy Bypass -File "<skill folder>\scripts\stepcast.ps1" <command> [-Param value ...]
```

- **In WorkBuddy, run it with the PowerShell tool.** The Bash tool refuses to start PowerShell ("use the PowerShell tool instead").
- **Texts and JSON go into UTF-8 files, passed with `-File`.** The command above starts a second PowerShell, and on the way every `"` inside an argument is lost, so `-Json '{"voice":"…"}'` arrives broken (HTTP 422). Short titles and ids are fine as arguments.
- Steps are named by id (`s_…`) or by `index`; **indexes start at 0** (`project` shows both). Slide numbers in `-Pages` start at 1, as in PowerPoint.
- Output is JSON (UTF-8); progress goes to stderr. `stepcast.ps1 help` lists every command.
- **Change nothing outside the work folder.** No PATH or other environment variables, no registry, no installers (winget, choco, .msi), no system settings: company computers usually don't allow it, and StepCast doesn't need it. Everything StepCast uses lives in the work folder.

References: [references/api.md](references/api.md) (the endpoints behind the commands), [references/narration-guide.md](references/narration-guide.md) (how to write narration). The complete API is at `GET http://127.0.0.1:8756/openapi.json`.

## Before anything

Run `status`. Its `next` says what to do:

| `next` | Meaning and what you do |
|---|---|
| `first-use` | StepCast isn't installed yet: follow **First use** below |
| `update` | the skill is newer than the installed program: tell the user, `stop` (only when no render is running), `setup`, `start`. Settings, projects and videos in the work folder stay as they are |
| `ffmpeg` | installed, but there is no ffmpeg (needed to make videos): see **ffmpeg by hand** below |
| `start` | installed but not running: `start` |
| `start-exe` | this copy of the skill has no program (it came with StepCast.exe): ask the user to start StepCast.exe |
| `ready` | StepCast is running: go on |

Then:

1. If `ai_disabled` (in `status` → `server`, or `health`) is false, tell the user you will switch StepCast's built-in AI off (so StepCast sends no text to an AI service; you write the texts instead, no AI account needed) and run `ai-off`. Voice-over keeps working.
2. Find the material: a recording in `projects`, or the deck file the user names. To record something new, the user records it in Chrome with the StepCast extension (see **Chrome extension**); you cannot record for them.

### First use

1. **Show the user this notice** in their language and wait for a clear yes. Don't install anything before.

   > **Before StepCast is installed**
   > - StepCast is installed into a work folder you choose: the program, its Python environment, ffmpeg, the settings, and all projects and videos. Updating the skill later replaces only the program; your settings, projects and videos stay.
   > - The first installation downloads open-source components — Python packages, and ffmpeg for making videos — about 100 MB, taking about 300 MB once installed. In mainland China they come from the Tsinghua University or Alibaba Cloud PyPI mirror, elsewhere from the official PyPI (each falls back to the other). ffmpeg is checked against a fixed SHA-256 checksum. If it can't be downloaded, an ffmpeg already on this computer is used, or you download one in the browser and I tell you where.
   > - StepCast's interface and narration start in this computer's display language; both can be changed later.
   > - StepCast runs a local service at 127.0.0.1:8756 that only this computer can reach.
   > - Voice-over sends the narration text to Microsoft's online voice service (Edge voices). Don't put confidential or personal information into narration.
   > - To write the narration, WorkBuddy reads your screenshots, slide texts and notes, so they go to the AI model WorkBuddy uses.
   > - StepCast's own AI stays off: no DeepSeek or other AI account is needed.
   > - When you record, don't record passwords or personal data.
   >
   > **安装 StepCast 之前请确认**
   > - StepCast 会装进你选的工作文件夹：程序、Python 运行环境、ffmpeg、设置，以及所有项目和视频都在里面。以后更新技能只替换程序，设置、项目和视频都保留。
   > - 首次安装会下载开源组件——Python 组件，以及生成视频用的 ffmpeg——约 100 MB，装好后约占 300 MB。在中国大陆从清华大学或阿里云的 PyPI 镜像下载，其他地区从官方 PyPI 下载（不通时互为备用）。ffmpeg 会按固定的 SHA-256 校验值核对；下载不了时，就用这台电脑上已有的 ffmpeg，或者由你在浏览器里下载，我会告诉你去哪下、放到哪里。
   > - StepCast 的界面和解说默认使用这台电脑的显示语言，以后都可以改。
   > - StepCast 在本机运行一个只有这台电脑能访问的服务（127.0.0.1:8756）。
   > - 配音时，解说文字会发送到微软的在线语音服务（Edge 语音）。不要把机密或个人信息写进解说。
   > - WorkBuddy 写解说时会读取你的截图、幻灯片文字和备注，这些内容会发给 WorkBuddy 使用的 AI 模型。
   > - StepCast 自带的 AI 保持关闭，不需要 DeepSeek 或其他 AI 账号。
   > - 录制时不要录到密码和个人信息。

2. **Ask two questions** (together with the notice is fine):
   - **Is this computer's network in mainland China?** It decides where the components are downloaded from (`-Network cn`: the Chinese PyPI mirrors first; `-Network global`: the official PyPI first). Don't guess it from the language: Chinese speakers abroad and foreigners in China both exist.
   - **Which work folder?** Suggest `%USERPROFILE%\StepCast` (or `D:\StepCast` when there is a D: drive with more space). Not inside the skill folder, and preferably not in a folder synced to the cloud.
3. **Install**: `setup -Folder "D:\StepCast" -Network cn -AcceptRisk`. It takes a few minutes the first time; run it with a long timeout or as a background task. When the company has its own PyPI mirror, add `-Index <url>`.
   The settings start in the computer's display language (`status` → `computer_language`, e.g. zh-CN → Chinese interface, Chinese narration and a Chinese voice). If the user wants another language, add `-Locale en-US` etc. Later the user can change it in ⚙ Settings; for one project use `set -Project P -Language … -Voice …`.
   `setup` prints the work folder, the download sources, the ffmpeg in use, `StepCast.cmd` and the settings it wrote (`new_settings`).
4. **Start**: `start`. Then run `health` once more in a separate command: if StepCast is gone again, the sandbox ended it; ask the user to double-click `StepCast.cmd` in the work folder (its window must stay open while StepCast is in use). The user can always start StepCast that way, also without you; the editor opens in the browser.
5. Tell the user where the work folder is, which language StepCast uses, and that StepCast's built-in AI is off.

`setup` remembers the work folder in `%USERPROFILE%\.stepcast\home.json` and the network choice in the work folder's `setup.json`; later `status` / `setup` / `start` need neither again. If the user moves between networks, pass `-Network` again.

### ffmpeg by hand

`setup` gets ffmpeg in this order: an `ffmpeg.exe` already in the work folder's `ffmpeg` folder → the download (imageio-ffmpeg, SHA-256 checked) → an ffmpeg that is already installed on this computer and found through PATH (only read, never changed; it is copied into the work folder). When all of that fails, `setup` installs everything else, then stops with an error, and `setup` / `status` print `ffmpeg_help`: the download links for the user's network and the folder to put it in. Tell the user, in their language:

1. Download one of these in the browser (the first one is the same package `setup` tried, and is checked against its SHA-256):
   - mainland China: the Alibaba Cloud or Tsinghua PyPI mirror link to `imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl` (31 MB), or `ffmpeg-win32-x64.gz` from the npmmirror (Alibaba) npm mirror (29 MB, not checked against a fixed checksum)
   - elsewhere: the same `.whl` from files.pythonhosted.org, or `ffmpeg-release-essentials.zip` from gyan.dev (not checked against a fixed checksum)

   Give the exact links from `ffmpeg_help` → `download`.
2. Then either tell you where the file is (usually the Downloads folder): run `setup -FfmpegFile "<file>"`; it unpacks ffmpeg into the work folder's `ffmpeg` folder. Or, if they already have an `ffmpeg.exe` (from IT, for example), they put it into that folder themselves (`ffmpeg_help` → `put_into`) and you run `setup` again.
3. `setup` takes only seconds then. StepCast itself also looks into that folder first, so a StepCast that is already running needs a restart (`stop`, `start`).

Never tell the user to install ffmpeg system-wide or to add it to PATH; the work folder's `ffmpeg` folder is all it takes.

### Chrome extension (only for recording browser steps)

The extension is in the work folder's `extension` folder (`setup` prints the path; it stays the same across updates). The user installs it once: open `chrome://extensions` → switch on "Developer mode" (top right) → "Load unpacked" → choose that folder. After an update of the skill: the reload button on the extension's card.

## Workflow: PPT / PDF / HTML → video

1. **Import**: `import -File "D:\…\deck.pptx" -Language zh-CN [-Voice zh-CN-XiaoxiaoNeural] [-Pages "1-12"] [-Name "…"]` prints the new project id.
   StepCast renders the slides itself (with PowerPoint when installed, keeping the slides' own animations and revealing points one by one). File names with Chinese or spaces are fine.
   An **HTML deck** (`.html`, or a `.zip` of its folder when pictures and scripts are separate files) is read in a headless Edge: slides, notes (`data-notes`, `aside.notes` …) and what can be clicked. Its slides are recorded moving when the video is rendered, and their controls are clicked as the stage directions in the notes say — brackets such as `（点按钮）`, `(click the "Run" button)`, never read aloud. Each click lands on the sentence of the narration it belongs to and follows when you rewrite the narration (a control the narration names by its text is found again). A card, list item or table row of the slide lights up when the narration uses its words, so name them as the slide does. `project` shows each HTML slide's `html`: its `controls`, `blocks` (pieces of content), the narration's `sentences` and the `clicks` (highlights too), numbered from 0. To set the clicks yourself, write `{"actions": [{"kind": "click", "control": 0, "unit": 2}]}` to a file (click control 0 when sentence 2 starts; `unit` = the number of sentences: after the narration) and run `web -Project P -Step S -File clicks.json`; `web … -Auto` places them from the notes again, `web … -Live false` makes the slide a still picture. Don't turn HTML slides into pictures yourself.
   Speaker notes written in the video's language become the narration of their slide; slides without notes, or with notes in another language, stay empty for you to write. A warning that the narration "could not be generated" because the built-in AI is off is expected.
   Don't export slides yourself (PowerPoint COM, python-pptx): the import is faster and gives the same pictures plus the animations.
2. **Read the deck**: `project -Project P` lists every slide with `slide_text`, `notes` and the current `narration` (HTML slides also `html`). Look at the slide pictures where the layout carries meaning (charts, diagrams, process flows): `shot -Project P -Step 3`.
3. **Write the narration**. If the user wants the notes read as they are, keep them and fill only the empty slides. Otherwise explain each slide in spoken sentences (see the narration guide): don't read the slide out word by word. Then `script-export -Project P -Out script.json`, edit `title` / `narration` / `include` per step and the project `title`, `subtitle`, `intro`, `outro`, and `script-import -Project P -File script.json`. Steps are matched by their `id`; keep the ids from the export.
4. **Voice-over and video**: steps 6–8 of the recording workflow below.

## Workflow: recording → video

1. **Read the recording**: `project -Project P` lists every step (kind, clicked element text, typed value, page, URL, highlight rect).
   Look at the screenshots that matter: `shot -Project P -Step 5` prints the PNG path; open it to see the page.
2. **Decide the story**: which steps carry the lesson. Hide noise (log-in, typing passwords, mis-clicks, duplicate captures, unrelated tabs) with `step -Project P -Step S -Include false`. Never show steps with passwords or personal data.
3. **Write the texts** (see the narration guide): a short title per step and 1–3 spoken sentences. Write them all in one go:
   `script-export -Project P -Out script.json` → edit `title` / `narration` / `include` per step, plus `title`, `subtitle`, `intro`, `outro` → `script-import -Project P -File script.json`.
   The subtitle follows the narration; set `caption` only for a shorter subtitle. `script-import` refuses the whole file when a step doesn't match (an unknown id, or an index counted from 1).
   One step: write the narration to a file, then `step -Project P -Step 7 -Title "…" -File n7.txt`.
4. **Background and principle pages** (optional, recommended for training): draw a text page and insert it as a slide step:
   `slide -Out bg.png -Title "为什么要…" -File bg.txt` (lines starting with spaces are indented; `·` lines are dimmed), then
   `image -Project P -Image bg.png -Title "…" -File bg-narration.txt -Position 0`.
5. **Highlight box**: if the zoom lands on the wrong area, set it in CSS pixels of the recorded page: `step -Project P -Step 9 -Rect "688,482,400,378"` (the `rect` column of `project` shows the current one). `-Zoom false -Highlight false` shows the whole page.
6. **Voice and language**: `set -Project P -Language zh-CN -Voice zh-CN-XiaoxiaoNeural` (other project texts: `set -Project P -File project.json` with e.g. `{"intro": "…", "outro": "…", "settings": {"intro_enabled": true}}`).
   Then `voice -Project P` voices the steps without voice; `-All` redoes every step.
7. **Render**: `render -Project P -NoSidecar` prints the MP4 path and length. `-NoSidecar` keeps players from showing the burned-in subtitles twice. Add `-NoSubSpace` when no second-language subtitles are planned.
8. Tell the user the path, length, and what they can still change.

## Workflow: assemble from several recordings

`new -Name "…" -Language zh-CN -Voice zh-CN-XiaoxiaoNeural` → `copy -Project NEW -From REC -Steps "12,13,15" [-Crop 22] [-Position i]` for each recording → continue at step 3 of the recording workflow.
`-Crop` cuts pixels from every screenshot edge (e.g. a coloured glow from browser automation); the highlight follows. Crop only once.

## Second-language subtitles

After rendering: `sentences -Project P -Out s.json` → translate every `text` in order (one translation per sentence, same count; keep product and button names) → write `{"key": "<key from s.json>", "texts": [...]}` to t.json → `subs-import -Project P -Lang en-US -File t.json`. The .vtt / .srt land next to the video; rendering again invalidates them (fetch the sentences again).

## Troubleshooting

- **`voice` reports `fallback` > 0** (those paragraphs were read by Windows' own voice, which sounds poor): the online voice service (speech.platform.bing.com) couldn't be reached. `fallback_reason` says why, `proxy` which proxy StepCast used.
  StepCast handles proxies itself: the environment and the Windows system proxy (only read), and WorkBuddy's sandbox proxy, whose port changes every session. Every `stepcast.ps1` call tells StepCast the current one, so after WorkBuddy was restarted any command (e.g. `status`) brings it up to date. Then run `voice` again: only the paragraphs read by Windows' voice are redone.
  If it still fails, the network blocks the voice service: tell the user, with `fallback_reason`. Don't work around it inside StepCast.
- **Never patch StepCast**: no `sitecustomize.py`, no monkeypatching, no edits under the work folder's `runtime\`. `setup` replaces the program, and a patch hides the real cause. If `setup` reports a `sitecustomize.py` from an earlier workaround, it can be deleted: StepCast now does that job itself.
- **A mirror says "from versions: none"** during `setup`: that mirror can't be reached from here; `setup` moves on to the next one by itself.
- **`start` fails, or StepCast is gone after `start`**: ask the user to double-click `StepCast.cmd` in the work folder and keep its window open.

## Good to know

- Rendering takes about 1/3 of the video length; voice-over a few seconds per step; a deck import a few seconds per slide. All are background jobs; the script waits and prints progress to stderr.
- Only one heavy job per project at a time (HTTP 409 = wait and retry).
- The MP4 is named after the project title, and rendering again replaces it. For two versions of one deck, import it twice with different `-Name`s, or copy the finished MP4 under a clear name.
- Narration and on-screen text are sent to Microsoft's Edge voice service for voice-over. Do not put personal data in narration; if the deck is confidential, tell the user before voicing.
- Projects live in `projects\<id>\` of the work folder (with StepCast.exe: next to the exe), videos in `projects\<id>\output\`; `health` → `data_dir` shows the folder. Never delete projects unless the user asks.
- StepCast's settings are in the work folder's `config.json`, the program and its Python environment in `runtime\`. Don't edit them by hand; `setup` repairs a broken environment. Speech recognition (narrating while recording, transcribing the sound of videos in slides) isn't part of this installation.
