# StepCast

**English** | [中文](README.zh-CN.md)

A tutorial video generator that runs on your own computer. Click through a task in Chrome once (or drop in a PowerPoint deck) and StepCast produces an MP4 tutorial with **narration, voice-over, subtitles, cursor animation and highlighted actions**.

Your data stays on your machine. Speech recognition runs locally. Only writing narration, translating, AI rewrites, second-language subtitles (and the alignment for slide "reveal one by one") call the AI model you choose — DeepSeek, OpenAI, Claude, Mistral, Gemini, Azure, Doubao, Qwen, GLM, MiniMax or a local Ollama (see [Privacy](#privacy)). AI voice-over uses Microsoft Edge's free voices by default.

**Download for Windows:** [latest release](https://github.com/comlong/stepcast/releases/latest) — unzip and run `StepCast.exe`, nothing else to install.

---

## What it does

| Feature | Details |
|---|---|
| Capture browser actions | The Chrome extension records clicks, typing, Enter and page navigation, and takes a screenshot at every step |
| Understand the target | Records the clicked element's text, role (button / link / text box) and position |
| Write the narration | An AI model writes the video title, intro, and per-step narration and subtitles from the recorded actions |
| Voice-over | Neural voices from edge-tts (free), or paid voices from Doubao, MiniMax or Qwen-TTS |
| Voice input | **Narrate while recording** (your speech is split across the steps automatically), record your own voice per step, upload audio, dictate text |
| PPT / PDF to video | One step per slide; **points appear as the narration reaches them**; **videos embedded in slides play in place**; **speaker notes used word for word as narration** (or rewritten by AI); pick slides and level of detail |
| Two-person Q&A | Turn a deck into a **conversation between a host and an expert**: two voices, full content, sounds like two colleagues talking |
| Highlight actions | Outline + breathing pulse + dimmed background + click ripple + cursor fly-in |
| Automatic camera | Eases in on the action area and back out to the full view |
| Subtitles | Burned into the video, plus `.srt` / `.vtt` export |
| Second-language subtitles | After rendering, generate subtitles in one or more of 46 languages; shown in italics right below the main subtitle; export a web player package where viewers pick the language (off by default) |
| Redaction | Automatically blurs e-mail addresses, phone numbers, ID / bank card numbers, IP addresses and names while recording; draw boxes by hand too |
| Translate | Switch the whole script to another language with matching voices; slide projects are rewritten from the original slides instead of translating the narration |
| Script editor | Edit text, adjust highlight boxes, reorder and delete steps, preview single steps, AI rewrite |
| Export | MP4, SRT subtitles, Markdown how-to document, script JSON |

---

## Quick start (Windows package)

1. Download `StepCast-<version>-win64.zip` from [Releases](https://github.com/comlong/stepcast/releases) and unzip it anywhere (paths with spaces or non-Latin characters are fine)
2. Double-click `StepCast.exe`
   - A console window opens — **keep it open**, closing it stops the app. The editor opens in your browser at <http://127.0.0.1:8756/>
   - On first run Windows may say "Windows protected your PC": click "More info" → "Run anyway" (the exe is not code-signed)
   - Double-clicking again doesn't start a second copy; it just reopens the editor
3. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension` folder inside the unzipped folder
4. In the editor, open **⚙ Settings → AI & voice**, choose an AI provider and enter your API key. The interface starts in English; switch the language at the top right

**Where things are stored**

| What | Where |
|---|---|
| All projects (screenshots, voice-over, videos) | `projects\<project id>\` next to the exe |
| Rendered videos / subtitles | `projects\<project id>\output\` |
| Uploaded PPT / PDF files | `projects\_imports\` (cleaned up 48 hours after the project is created) |
| Settings and API keys | `config.json` next to the exe |
| Speech recognition model (downloaded on first use, about 465 MB) | `%USERPROFILE%\.cache\huggingface` |

If the folder isn't writable (for example under `C:\Program Files`), `projects\` and `config.json` go to `%LOCALAPPDATA%\StepCast` instead; the console window prints the actual folder at start-up.

**Upgrading:** unzip the new version and copy `projects` and `config.json` over from the old folder.

**Internet access** is needed for the voice-over (Edge voices) and the AI model. Speech recognition runs offline once the model is downloaded.

---

## Install from source

1. **Dependencies:** double-click **`setup.bat`** (creates a `.venv` and installs everything), or:

   ```bash
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

   Python 3.10+ is required. Installing ffmpeg is recommended (`winget install Gyan.FFmpeg`); without it StepCast falls back to the copy bundled with `imageio-ffmpeg`.

2. **Start:** double-click **`run.bat`** or run `python app.py`. The editor opens at <http://127.0.0.1:8756/>.

3. **Chrome extension:** `chrome://extensions` → **Developer mode** → **Load unpacked** → select the `extension` folder of this repository. An orange ▶ icon appears in the toolbar.

### Build the Windows package

Double-click **`build_exe.bat`** (the first run installs PyInstaller into `.venv`; building takes a few minutes). The result is in `dist\`: a `StepCast` folder (`StepCast.exe`, `_internal`, `extension`, `README.txt` with English and Chinese instructions, `LICENSE.txt`) and `StepCast-<version>-win64.zip` (about 170 MB) to share. It is "exe + folder" rather than a single exe: the dependencies are several hundred MB, and a single-file exe would unpack itself on every start and trigger more antivirus false positives.

Your own `config.json` (API keys) and `projects\` are **never** packaged — the build script checks this.

`build_exe.bat --with-model` builds a second zip that already contains the `small` speech recognition model (about 450 MB more), for colleagues who can't download it (see [Speech recognition models](#speech-recognition-models)).

---

## Interface language

Pick the working language next to **⚙ Settings** at the top right: **中文 / English / Deutsch / Français / Polski / Italiano / Español / Nederlands**. The page reloads in that language.

- The **Chrome extension** follows the same setting (popup, recording overlay, microphone page) when it is connected to the app; its name and shortcut descriptions follow Chrome's own language
- **English by default:** a fresh install uses an English interface, English (en-US) narration and the en-US-AriaNeural voice. Your choice is remembered
- **Errors and progress messages** follow the interface language too
- **Interface language ≠ narration language:** the narration language is chosen separately under **⚙ Settings → AI & voice** (see [Narration and subtitle languages](#narration-and-subtitle-languages)). Default texts written into videos and documents ("Step 3", "Done!") follow the project's narration language

> Automatic redaction rules lean towards Chinese pages: phone and ID numbers use Chinese formats, labelled names are recognised after Chinese and English labels ("Name:", "Contact:" …). E-mail, IP and your own keywords work in every language — for European systems, add customer names to "Additional words to redact".

---

## AI providers

Writing narration, translating and AI rewrites need a large language model. Open **⚙ Settings → AI & voice**, pick a provider, enter the API key, choose a model, click **Test connection**, then save.

| Provider | Protocol | Where data is processed | Notes |
|---|---|---|---|
| **DeepSeek** | OpenAI-compatible | China | Default; settings from older versions carry over |
| **Doubao (Volcengine Ark)** | OpenAI-compatible | China | Default `doubao-seed-2-1-pro-260628`; enable the model in the Ark console first; deep thinking is switched off automatically |
| **Qwen (Alibaba Cloud Model Studio)** | OpenAI-compatible | China (Beijing region) | Default `qwen-plus`; thinking mode switched off automatically |
| **Zhipu GLM** | OpenAI-compatible | China | Default `glm-5`; thinking mode switched off automatically |
| **MiniMax** | OpenAI-compatible | China | Default `MiniMax-M3`; always thinks before answering, so it is slower (the thinking text is removed); the mainland endpoint is `api.minimax.cn` |
| **OpenAI** | OpenAI-compatible | USA | Default `gpt-5-mini` |
| **Claude (Anthropic)** | Official SDK | USA | Default `claude-opus-5` (best quality); `claude-sonnet-5` is faster and cheaper |
| **Mistral AI** | OpenAI-compatible | **EU** (French company; see their terms) | Default `mistral-large-latest` |
| **Google Gemini** | OpenAI-compatible | USA | Free-tier data may be used to improve models |
| **Azure OpenAI** | OpenAI-compatible | **EU possible** (European region + EU Data Zone deployment) | Endpoint `https://<resource>.openai.azure.com/openai/v1`, model = deployment name |
| **Local Ollama** | OpenAI-compatible | **This computer, offline** | Quality depends on the model and your hardware; run `ollama pull qwen3:8b` first |
| **Other OpenAI-compatible APIs** | OpenAI-compatible | Depends on the service | OpenRouter, LM Studio, vLLM, Scaleway, IONOS … enter the endpoint and model name |

- Type a model name or click **Fetch model list** to get the provider's current models
- **Test connection:** Claude only queries model information (no tokens used); other providers get a very short test prompt
- Keys are stored per provider, so switching providers doesn't lose them; saving with an empty field keeps the existing key
- Parameters a model doesn't accept (`temperature`, `max_tokens`, JSON mode, thinking switches) are adjusted and retried automatically
- Claude `claude-opus-5` / `claude-fable-5-1` use server-side fallback: if a rare request is refused by the safety system, Anthropic reruns it on another model automatically

Environment variables take precedence over the settings page: `DEEPSEEK_API_KEY`, `ARK_API_KEY`, `DASHSCOPE_API_KEY`, `ZHIPUAI_API_KEY`, `MINIMAX_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, `GEMINI_API_KEY`, `AZURE_OPENAI_API_KEY`.

Keys are kept in the local `config.json`; the interface and API only show masked values, and a key is only ever sent to its own provider.

> In Europe and concerned about data leaving the EU: use **Mistral** or **Azure OpenAI (European region)**. Data must stay in China: **DeepSeek / Doubao / Qwen / GLM / MiniMax**. Nothing should leave your computer: **local Ollama**.

### More voice services (optional, paid)

Edge voices are free and the default. For more natural voices, add a key under **⚙ Settings → AI & voice → Voice-over → More voice services**. That service's voices then appear in every voice dropdown (prefixed with the service name) and can be mixed freely, including for the two speakers in Q&A mode:

| Service | Strengths | Key |
|---|---|---|
| **Doubao Speech (Volcengine)** | The most natural Chinese voices; your own cloned voices (IDs starting with `S_`) can be added | Enable "Speech Synthesis Model 2.0" in the Doubao Speech console and create a key under "API Key management" |
| **MiniMax Speech** | Very natural in Chinese and English, many voices (the full list, including your clones, is fetched with your key) | Same key as MiniMax under AI models |
| **Qwen-TTS (Alibaba Cloud Model Studio)** | One voice speaks ten languages including Chinese and English | Same key as Qwen under AI models |

- **Test** synthesises one sentence to check the key (costs a tiny amount)
- Long paragraphs are synthesised sentence by sentence and joined; the timings align subtitles and slide reveals
- Errors (wrong key, no balance) are reported instead of silently falling back to a robotic Windows voice
- Environment variables `MODEL_SPEECH_API_KEY` (Doubao), `MINIMAX_API_KEY`, `DASHSCOPE_API_KEY` also work

> No key at all? Write the narration yourself in the editor — voice-over and rendering work the same.

---

## Workflow

```
Extension → Start recording → use the website → Stop → edit the script in the editor → Generate all → MP4
```

**1. Record.** Click the extension icon, enter a tutorial name and click **● Start recording**, then use the website as usual. A small "● Recording" badge appears at the bottom right (hidden automatically in screenshots).

- `Alt+Shift+R` starts / stops recording
- `Alt+Shift+S` takes an extra screenshot (for screens you only show, without clicking)

Stopping opens the editor automatically.

**2. Edit.**

- **Left column:** the **Intro** card at the top, the **Outro** card at the bottom, recorded steps in between (drag to reorder)
  - Click the intro to change the video title, subtitle and intro narration, or untick **Include intro in video**; the same for the outro
  - To leave intro / outro out of all new projects: ⚙ Settings → Video style → untick **Add intro** / **Add outro** (a project's own setting wins)
  - **Your own intro / outro picture:** under **Background**, upload an image or a PPT / PDF and pick a page (for example your company's cover slide). Choose "fit" (blurred fill around it) or "fill", whether to overlay the title text, and how long it stays on screen (never shorter than its voice-over). Converting PPT needs PowerPoint or LibreOffice; PDF and images always work
- **Middle:** drag the orange box to adjust the highlight; switch to **Rendered preview** to see the actual video frame
- **Right column:** edit the on-screen title, narration and subtitle; **AI rewrite** rewrites the current text as you ask; **Note for the AI** is taken into account when writing narration
- Untick **Include in video** to skip a mistaken step

**3. Generate.** The bottom bar, left to right:

- **⚡ Generate all** — narration → voice-over → render in one go
- **① Write narration** — let the AI write the script (style and audience selectable)
- **② Generate voice-over** — voice the steps that don't have audio yet
- **③ Render video** — produce the MP4
- **🌐 Translate** — switch the whole script to another language with matching voices
  - Recorded projects: the existing narration is translated
  - Slide projects: the narration is **not** translated; it is rewritten in the target language from the original slides (text + speaker notes), together with the title, intro and outro. This avoids "English deck → Chinese narration → back to English" double translation
- **Export ▾** — Markdown document / script JSON / SRT subtitles

Rendering takes roughly 0.2–0.5× the video length (1080p30; slide projects are faster), with parallel rendering and GPU encoding (see [Rendering speed](#rendering-speed)).

**Stopping halfway:** while a task runs, **■ Stop** appears next to the progress bar and usually stops within a second. Finished work is kept: completed voice-overs stay, and an interrupted render never overwrites the previous video. Clicking **⚡ Generate all** again continues: steps that already have narration don't call the AI again, and only steps whose text changed get new voice-over.

---

## Voice input

Speech recognition uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your computer — **your recordings never leave it**.

### Narrate while recording

Tick **🎙 Narrate while recording** in the extension popup before recording, then talk while you click. After you stop:

1. The recording is sent to the local app and transcribed; filler words and stutters are removed
2. **Each sentence is assigned to a step by click time** — "say it, then click": a sentence belongs to the first action after it
3. Voice-over, as you chose: **AI voice** (default; your words become the narration, read by an AI voice without slips and pauses) or **keep my own voice** (your recording is cut per step; subtitles use word timestamps)

The first time, a microphone permission page opens (the extension's background page can't ask directly). Later you can change the device and see a level meter under **Microphone settings** in the popup.

### Record or upload per step

In the right column's **Voice-over** card: **🎙 Record my voice** (pick microphone and recognition language, up to 60 s, play back, redo), **⬆ Upload audio** (mp3 / wav / m4a / webm, up to 15 MB), **Switch to AI voice**, delete. Your own recordings are protected: correcting the text doesn't discard them, and **② Generate voice-over** / **① Write narration** never overwrite them (only translating switches them back to AI, since the recording is in the old language). The **🎙 ▾** menu at the bottom has bulk actions (**Switch all to AI voice**, delete all voice-overs).

**🎤 Dictate** above the narration box appends what you say to the text, without keeping the audio.

### Speech recognition models

⚙ Settings → AI & voice → speech recognition: `tiny` / `base` (75 / 145 MB, fast, weaker for Chinese), **`small`** (465 MB, **default**, reliable for everyday narration), `medium` / `large-v3` (1.5 / 3 GB, more accurate, noticeably slower on CPU). The model is downloaded to `~/.cache/huggingface` on first use; `small` runs at about 2–3× real time on a normal CPU.

**Can't download the model (mainland China / company network)?**

- **Automatic mirror:** the official HuggingFace source is tried first; if it can't be reached, StepCast switches to `hf-mirror.com` within seconds. You can also enter your own mirror under "Model download source"
- **Offline:** put the model folder at `models\faster-whisper-<size>\` next to the program (next to `StepCast.exe` for the packaged version), containing `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.*`. Copy it from a computer that has it in `%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-<size>\snapshots\<hash>\`
- **Package with the model:** `build_exe.bat --with-model` (or `--with-model medium`)

---

## PPT / PDF to video

**Projects → 📊 Create from PPT / PDF**:

1. **Upload** a .pptx / .ppt / .pdf (up to 100 slides)
2. **Pick slides:** thumbnail grid, all non-hidden slides selected by default; slides with notes are marked "Notes", slides without notes "No notes"
3. **Where the narration comes from** (shown when the deck has speaker notes):

   | Option | What happens | Calls the AI |
   |---|---|---|
   | **Use the notes as narration (recommended)** | Notes are used word for word as narration and subtitles (line breaks joined). Slides whose notes are in a different language than the narration are handed to the AI, which tells the full content of the notes in the narration language | Not for same-language slides |
   | **AI rewrites the notes in a spoken style** | The notes' intent comes first, slide text supports it; the AI writes a presenter's narration | Yes |
   | **Ignore notes, AI writes from the slide content** | Uses only titles and slide text | Yes |

   If the notes are in a different language than the narration, the import dialog switches to "AI rewrites the notes" automatically (and back if you change the narration language to match)
4. **Slides without notes** (with "use the notes"): **AI writes it from the slide content** (only those slides' titles and text are sent) or **Leave empty, I'll fill it in** (fully offline; add the narration in the editor later)
5. **Level of detail:** brief (1–2 sentences per slide) / standard (2–4) / detailed (4–7) — only for slides the AI writes
6. Choose the narration language and voice (can differ from the deck's language) and create

> **PDFs have no speaker notes** (PowerPoint drops them when saving as PDF), so a PDF can only be narrated by the AI from the slide content. Import the `.pptx` / `.ppt` to use your notes. Notes from Google Slides / Keynote exports are read too.

**Changing your mind later:** in a slide project, **① Write narration** offers the same choices. With "use the notes", **Overwrite narration I've edited by hand** is ticked by default, because the notes are your reference script; after editing a slide's **Speaker notes** in the editor, writing the narration again uses the new notes. The choice is stored with the project and reused by **⚡ Generate all**.

Each slide becomes a static step: centred page, slide transitions, subtitles below the page so they never cover content. PowerPoint animations and transitions are not kept.

### Videos inside slides

Videos embedded in slides are kept: each becomes its own "video" step right after its slide (you can switch off "include videos" in the import dialog).

- **Default:** the slide's narration is spoken first, then the video plays **at its original position on the slide with its own sound**; trim points set in PowerPoint are used
- **Video first:** for slides whose callouts belong on top of the video, select the slide and set **Order of this slide and its video** to *Video first, then explain the text over the video*. The video plays first while the text lying over it (at least half of its area inside the video) stays hidden; afterwards the frame holds the video's last frame, that text appears point by point over it with the narration (with reveal switched off: in quick succession), and the rest of the slide is visible from the start. **① Write narration** then only explains the text over the video — regenerate it after changing the order. Hiding the text needs the reveal data from a PowerPoint import (see below); without it the whole slide is shown after the video
- In the editor's **Video** panel: play in place / full screen / show the poster only; trim start and end (**⏱ Current position** picks the time from the player); play the original sound or mute it and read the step's narration; **🎤 Turn speech in the video into subtitles** transcribes it locally and aligns the subtitles
- Videos that are only linked (a file on the author's computer, YouTube…) are left out until you **⬆ Upload video file**
- PDFs contain no videos — import the `.pptx` to keep them
- In recorded tutorials, **🎬 +** at the top of the left column inserts a video after the current step (full screen), e.g. a product demo; portrait phone videos and browser-recorded webm files work

### Reveal points one by one

With **Reveal points one by one, highlight the current one** (default when importing), slide content no longer appears all at once:

- Title and page number stay visible; the body is split into points in reading order, and **each point fades in when the narration reaches it**
- When the narration moves on, earlier points dim and the current one stands out; points mentioned in the same sentence appear one after another; everything returns to full brightness near the end of the slide
- Slides cross-fade into each other, with a short pause (0.35 s) before the voice starts

How the timing is decided:

- **With an AI model configured:** before rendering, the AI matches each point to the narration sentence that talks about it (by meaning — works even if the narration is in another language or paraphrased); points appear at the start of that sentence. Points the narration doesn't mention appear in order between their neighbours; if nothing matches, the points appear quickly one after another after the slide change. Results are cached, so unchanged slides aren't sent again
- **Without AI:** the slide text is searched in the narration; if fewer than half of the points are found, they appear quickly one after another without highlighting
- Images without text appear together with the nearest text point
- When the AI writes the narration, it gets each slide's points in reading order, so it talks about them in the order they appear

Card layouts count each card as one point (arrows go with the following card); classic bullet lists count each top-level bullet with its sub-bullets; reading order is row by row, then column by column. Slides with a single point, more than 12 points or redactions — and the cover — appear as a whole. Switch it per project or per slide (**Reveal points on this slide**) under **Slide content** in the editor.

This needs **PowerPoint** on the importing computer (it exports extra images per point; importing takes about a second longer per slide). Rendering doesn't need PowerPoint, so projects keep the effect on other computers. Without PowerPoint, or for PDFs, slides appear as a whole.

### Two-person Q&A

Choose **Two-person Q&A (host asks, expert explains)** as the narration style when importing, and the video is told as a conversation between a host (female voice) and an expert (male voice):

- **Based entirely on the slides** (titles, text, speaker notes) and **not shortened** because it's a dialogue: the expert covers at least as much as a single narrator would; the host's questions and summaries are extra
- **Sounds like colleagues talking**, not a quiz: the host follows up and rephrases, the expert gives examples and practical tips, and each slide picks up from the previous one
- Works with **reveal one by one**: the expert talks through the points in reading order
- **Names are never spoken or shown in subtitles** — they only identify the speakers in the editor
- **Two voices**, chosen when importing (e.g. Aria / Guy in English); each line uses its speaker's voice
- The slides look exactly like single-narrator videos; subtitles never span two speakers

In the editor, **Q&A lines** lists one line per row (switch speaker, play, reorder, delete, add); **🎭 Speakers** changes names and voices; **🌐 Translate** rewrites the dialogue in the target language with matching voices. There is no "use the notes" option in this mode — the notes are one person's script and serve as reference.

**How slide images are produced:** with PowerPoint installed, slides are exported exactly as they look (fonts, charts, SmartArt), including old `.ppt`; otherwise LibreOffice; without either, PDFs still work and PPTX falls back to a text-only page — save as PDF first in that case.

---

## Redaction

Three layers of protection against customer names, e-mail addresses or phone numbers in screenshots:

1. **Automatic while recording (on by default):** at the moment of each screenshot the extension walks the page and gets the **exact rectangle of every match**, so only those characters are blurred. Detects e-mail, phone, ID and bank card numbers, IP addresses (patterns); names after labels such as "Name:" **and the cells of name / contact / owner columns in tables**; your own keyword list; and input fields that match. Configure it or add "Additional words to redact" in the extension popup's settings
2. **By hand in the editor:** **🔒 Redact** above the screenshot — drag to create a box, move or resize it, switch between blur / pixelate / solid block
3. **Scan the whole project afterwards:** **Scan whole project…** in the right column — rerun the rules, redact a list of keywords in every step at once, remove all automatic boxes, or clear the stored text index

Redaction is applied by down-sampling the source image, so the information is really gone, not just covered; zooming in later can't bring it back. Matches are also masked in the page title, element text and typed values — the only fields sent to the AI.

---

## Second-language subtitles

The main subtitle is burned into the video; second-language subtitles are separate subtitle files shown right below it, in a slightly smaller italic font. **Viewers choose one themselves; off by default.** Render first, then add as many languages as you need, now or later.

1. **Render the video.** The main subtitle's bottom edge sits at 93.5% of the frame height, leaving one line below for the second subtitle (⚙ Settings → Video style → **Leave room for second-language subtitles**, on by default; off = 95.5%). The subtitle box hugs the text (5.5% high for one line); slides start 1.2% from the top and end at 87.5% (89.5% when off)
2. Click **▶ View video** (or **Export ▾ → Second-language subtitles / web player…**), then the orange **🌐 Generate second-language subtitles**, tick the languages and click **Generate**. The main subtitles are merged into full sentences and translated by your AI model, each keeping its timing; very short sentences are merged with the next one, long translations are split into two cues that switch at the same moment as the main subtitle
3. Pick a language in **Preview** to see it in the editor (also in ⛶ full screen)
4. Each language can be downloaded as **VTT / SRT**; **Download web player package** gives a zip with the video, the subtitles and an `index.html` player
   - **SRT is for desktop players** (Windows Media Player etc.), which show external subtitles slightly late — the SRT timeline is shifted 0.25 s earlier. **VTT is for web players** and matches the video exactly

**On your intranet:** unzip the web player package into any folder of your website and open `index.html`, or embed it with an iframe (add `allow="fullscreen"` for full screen):

```html
<iframe src="/training/demo/index.html" width="960" height="600" allow="fullscreen" style="border:0"></iframe>
```

- 💬 at the bottom right picks the second subtitle ("—" = off), ⛶ is full screen; append `?sub=en-US` to the link to open with a language selected
- The subtitle timelines are embedded in the page — no server configuration; it even works when opened from disk
- With your own player, use the VTT files (`<track kind="subtitles" srclang="en" label="English" src="….en-US.vtt">`; without `default` it's off initially). The included player is the most accurate
- After re-rendering (changed narration), older second subtitles are marked "needs regenerating" and left out of the package
- Videos rendered by old versions have no room for a second subtitle; you'll be asked to render again

## Narration and subtitle languages

Narration (new projects, PPT import, translation, the extension's "Narrate while recording") and second-language subtitles share one list of **46 languages**, each with free Edge voices (one female, one male — the male voice is the expert in Q&A mode). Order in the dropdowns:

1. **Chinese:** Simplified, Traditional
2. **European languages:** the common ones first — English (UK), English (US), Deutsch, Français, Español, Italiano, Nederlands, Polski, Português (Portugal) — then alphabetically by native name: Bosanski, Català, Čeština, Cymraeg, Dansk, Eesti, Gaeilge, Galego, Hrvatski, Íslenska, Latviešu, Lietuvių, Magyar, Malti, Norsk, Română, Shqip, Slovenčina, Slovenščina, Suomi, Svenska, Türkçe, followed by Ελληνικά and the Cyrillic Български, Македонски, Русский, Српски, Українська
3. **Other languages:** 日本語, 한국어, Português (Brasil), Tiếng Việt, ไทย, العربية, हिन्दी

- If a deck's notes are in another language than the narration (e.g. English notes, Swedish narration), they aren't read out as they are; the AI writes the narration in the narration language from them. Closely related languages that can't be told apart reliably (Danish / Norwegian / Swedish, Czech / Slovak) count as the same
- Speech recognition supports almost all of them; for Irish the model detects the language itself
- Second subtitles are italic to set them apart (except Arabic, which is written right to left)

### Complex scripts

Subtitles, intro / outro cards and step titles are drawn into the frames by StepCast itself, so every script is handled explicitly:

- **Fonts are chosen per character:** Microsoft YaHei by default; text it can't show (Arabic, Hebrew, Hindi, Thai, Korean, some Vietnamese letters…) automatically switches to a Windows font that has those characters (Segoe UI, Nirmala UI, Leelawadee UI, Malgun Gothic…)
- **Complex text layout by Windows:** Arabic joining and right-to-left order, Hindi conjuncts and vowel signs, Thai tone marks are laid out by Windows' own Uniscribe engine — no extra install
- A font set in ⚙ Settings is always tried first

On non-Windows systems StepCast falls back to `arabic-reshaper` + `python-bidi`: Arabic and Hebrew display correctly; Hindi and Thai show no boxes but aren't laid out perfectly. `.srt` / `.vtt` files are laid out by the player and are always fine.

---

## Rendering speed

Frames are drawn with Pillow in Python, then encoded by ffmpeg. Drawing is the bottleneck, so three things are combined (all on by default):

| Technique | Details |
|---|---|
| Parallel rendering | Every step (including intro / outro) is an independent segment; several are rendered at once. Pillow releases the GIL, so threads really use multiple cores. Automatic by CPU count, at most 4 |
| Static frame reuse | Frames that don't change reuse the previous frame's data. Huge gain for slide projects |
| GPU encoding | NVIDIA (NVENC), Intel (QSV) and AMD (AMF, including Ryzen APUs) are detected by actually encoding a short test clip; falls back to CPU automatically if anything goes wrong, even halfway |

Measured on an 8-core desktop, 1920×1080 @ 30 fps:

| Project | Before | Now | Speed-up |
|---|---|---|---|
| PPT slides (9 slides, 2 min 36 s video) | 164 s | 22 s | 7.6× |
| Recorded website (8 steps, 68 s video) | 59 s | 30 s | 2.0× |

Parallel and serial output are pixel-identical. To control it manually, set `"video_encoder"` (`auto` / `cpu` / `nvenc` / `qsv` / `amf`) and `"render_workers"` (`0` = automatic) in `config.json`. GPU-encoded files are usually 30–50% larger; use `cpu` for smaller files. Lower resolution or frame rate (⚙ Settings → Video style) for more speed: 720p is more than twice as fast as 1080p.

## Video style

**⚙ Settings → Video style:** resolution / frame rate (default 1920×1080 / 30; 720p, 2K and portrait 1080×1920 too), accent color (`#FF5C39`), automatic zoom and its factor, dimming outside the highlight, cursor animation, browser frame, **Burn subtitles into the video**, **Leave room for second-language subtitles**, and minimum step duration (2.5 s; actual = max(this, voice-over + padding)). Zoom, highlight and duration can also be set per step.

---

## FAQ

- **Extension says "not connected"** — start `run.bat` / `StepCast.exe` first. If you changed the port, update the server address in the extension's settings
- **The "● Recording" badge appears in a screenshot** — it's normally hidden automatically; delete the step and redo it, or take one with `Alt+Shift+S`
- **Some screenshots lag behind fast clicks** — Chrome allows about two screenshots per second; slow down a little
- **The highlight box is off** — drag the orange box in the editor; drag its corner to resize
- **Actions inside iframes aren't recorded** — only the top-level page is captured; use `Alt+Shift+S` and write the narration yourself
- **Voice-over failed** — edge-tts needs internet; offline, Windows SAPI5 voices are used (lower quality, no word timing)
- **Chinese characters show as boxes** — set `font_path` in `config.json` to a font that has them (default is `C:\Windows\Fonts\msyh.ttc`)
- **403 "only 127.0.0.1 / localhost"** — the app only accepts local requests so that websites you visit can't read your projects. To use it from another computer on your network, `set VT_ALLOWED_HOSTS=<host name>` and start with `python app.py --host 0.0.0.0`
- **"Narrate while recording" captured no sound** — check permission, device and the level meter under **Microphone settings** in the popup
- **Imported PPT looks text-only** — neither PowerPoint nor LibreOffice was found; save the deck as PDF in PowerPoint or WPS and import that, or install LibreOffice
- **Rendering is slow** — use 720p and 24 fps, or switch off automatic zoom

---

## Privacy

Screenshots, audio, videos and scripts stay in the local `projects/` folder. Text is sent to the AI provider you chose (nothing leaves your computer with local Ollama) only for: **writing narration / translating / AI rewrite**; **generating second-language subtitles** (the main subtitle text); and **rendering slides with "reveal one by one"** (each slide's narration and point texts, so the AI can tell which point is being discussed — without AI, text matching is used offline). Every provider receives exactly the same content. Voice-over text goes to the voice service you chose (Edge by default; Doubao / MiniMax / Qwen if you use their voices).

**See exactly what would be sent** (intercepted, nothing is sent, no key needed):

```bash
python tools/dump_llm_payload.py
```

For recorded tutorials, each step sends: the action (click / type / key / navigate), page title (80 characters), URL **domain and path only** (60), the clicked element's visible text (80) and role, what you typed in input steps (60), and your own "Note for the AI" (200). **Never sent:** screenshots, audio, videos, CSS selectors, coordinates, page HTML/DOM, cookies, localStorage, timestamps, or any other text on the page.

⚠️ Element text, page title and typed text are **real text from the page** — if you click "Customer John Smith", that goes into the request. Run the [redaction](#redaction) scan first (matches are masked in these fields too), untick steps you don't want sent, or write the narration yourself. Password fields (and inputs whose name/id/autocomplete contains `pass`, `pwd`, `secret`, `token`, `cvv`, `card`) are replaced with `••••••••` while recording.

**Voice input** is transcribed locally; the only network access is downloading the model once.

**PPT import:** slide images are never uploaded. With "use the notes as narration", slides whose notes are in the narration language stay completely offline; slides without notes send only their title and text if you let the AI fill them in (no notes); slides whose notes are in another language send title, text and notes so the AI can tell them in the narration language. "AI rewrites the notes" sends title, text and notes of the selected slides. The bottom of the import dialog always shows what will be sent.

---

## Development

```
app.py                  entry point
backend/main.py         FastAPI routes
backend/storage.py      project files; background jobs merge their results back three-way
backend/services/       script_gen (narration), llm* (providers), tts / tts_cloud / dialogue (voice-over),
                        video / renderer / textlayout (rendering), slides / slide_reveal / slide_sequence / clips (PPT import),
                        second_subs (second-language subtitles), asr / voice (speech), redact, cards …
static/                 editor (plain HTML / CSS / JS) and locales/ (en, de, fr, pl, it, es, nl)
extension/              Chrome MV3 extension
packaging/              PyInstaller spec and build script
tools/                  selftest.py, dump_llm_payload.py, i18n_extract.py
tests/                  regression tests
```

- **Self-test** without the extension: `python tools/selftest.py --no-tts` (render only), `python tools/selftest.py` (with voice-over), `--llm` (with AI narration)
- **Regression tests:** `.venv\Scripts\python.exe -X utf8 tests\run_all.py --lint` — 25 groups with synthetic data, fake AI and voice clients (no credits used), isolated data folders. See [tests/README.md](tests/README.md)
- **Translations:** interface strings are written in Chinese inside `t()`; translations live in `static/locales/<lang>.json`. Run `python tools/i18n_extract.py` to list missing translations and mismatched placeholders (`--missing de` lists what German still needs, `--sync-extension` copies the extension's strings). Plurals use ICU syntax, e.g. `{n, plural, one {# step} other {# steps}}`

## Known limitations

- Only the top-level page is captured (no iframes)
- It's "screenshots + animation", not screen video — page animations aren't captured, but files are small, frames are clean and every sentence is editable
- The extension works on `http(s)` pages only (not `chrome://` pages)
- Single-user, local use; no accounts or collaboration

## License

[Apache License 2.0](LICENSE). Third-party components bundled in the Windows package (ffmpeg, PyMuPDF, Python libraries, …) keep their own licenses.
