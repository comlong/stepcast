# StepCast — notes for Claude

A local tutorial video generator: the Chrome extension records browser actions, or a PPT / PDF deck is imported → AI writes the narration → voice-over → rendered MP4 with subtitles. Mainly used on Windows inside a company.

## Rules

- **Always talk to the user in Chinese**, including progress notes along the way.
- **Code comments, docstrings, commit messages and documentation are in English.** The project is presented as an international project; Chinese is an auxiliary translation (`README.zh-CN.md`, `static/locales`).
- **Only when the user asks:** restarting run.bat, git commits, version bumps, packaging. Don't do them on your own after a change.
- **Tests never spend money:** no real LLM or cloud TTS calls — use fake clients and fake keys.
- **Tests never touch the user's data:** separate `VT_DATA_DIR` and `config.CONFIG_PATH`; never modify the real `config.json` or delete the user's projects. Reading real projects (`projects/`) for diagnosis is fine, read-only.
- **No registry changes** anywhere (app, packaging, installation, tests).
- **Never close the user's PowerPoint:** the code only quits a PowerPoint instance it started itself; tests that need PowerPoint run only when it isn't open.
- **The About page names no companies**; the disclaimer appears only on the About page.
- Show the DeepSeek model only as "deepseek", without `-chat` / `-flash` suffixes (it still uses deepseek-chat).
- **The release packages must never contain `config.json` (API keys) or `projects/`.**
- **No company-internal material in git:** real decks, screenshots or texts from real projects. Test data is generated; internal material goes to `tests/private/` (ignored). Real user projects are for diagnosis only — never copy them into tests or commits.
- **No names of other commercial products used for comparison** (competitors etc.) in docs or comments. Names of services the app actually integrates with (AI providers, voice services, Chrome, PowerPoint, Windows) are fine.
- Never type passwords into login forms, and never ask for passwords / API keys in the chat.

## Running

- `setup.bat` creates `.venv` and installs dependencies; `run.bat` → `app.py` → FastAPI at `http://127.0.0.1:8756`.
- Restarting run.bat (when asked): stop the process listening on 8756, and its parent if that is the cmd window running run.bat; then from the project folder run `Start-Process cmd.exe -ArgumentList '/c', "$PWD\run.bat" -WorkingDirectory $PWD` (opens a new window).

## GitHub

- Public repository: github.com/comlong/stepcast. History starts at 1.8.0; older history exists only in a local backup and is never published.
- Push only `main` and the new version tag (`git push origin main vX.Y.Z`); **never** `git push --tags` / `--all`.
- Installers go to GitHub Releases (`gh release create`), not into the repository.
- `README.md` (English) and `README.zh-CN.md` (Chinese) must stay in sync.

## Code structure

- `backend/main.py`: all API routes; `app = FastAPI(title="StepCast", version=...)` is the source of the version number.
- `backend/storage.py`: a project is `projects/<pid>/project.json` + `screenshots/` `audio/` `output/`. Background jobs work on a copy and merge it back with `storage.commit(pid, before, after, ...)` (three-way, so the user's edits made meanwhile are kept); `main.STEP_TEXT` (texts) and `storage.AUDIO_FIELDS` (voice-over) are field groups merged as a whole. Use `storage.update(pid, fn)` for simple changes.
- `backend/models.py`: Pydantic models (Project / Step / DialogueLine / SubtitleTrack …).
- `backend/config.py`: `config.json`; `config.save` keeps only keys present in `DEFAULTS` — add new settings to `DEFAULTS` first.
- `backend/services/`
  - `jobs.py`: background jobs; jobs listed in `HEAVY` are mutually exclusive per project.
  - `script_gen.py`: narration (recordings / slides / two-person Q&A); `llm.py` + `llm_openai.py` / `llm_anthropic.py`: providers (`Preset`).
  - `tts.py` (Edge voices, dispatch by voice-id prefix), `tts_cloud.py` (Doubao / MiniMax / Qwen; voice ids prefixed `doubao:` etc.), `voice.py`, `dialogue.py` (per-line synthesis for Q&A).
  - `video.py` (rendering, parallel segments, encoder fallback), `renderer.py` (PIL frames: subtitles, cursor, zoom, slide layout), `textlayout.py`, `gdi_text.py`.
  - `slides.py` (PPT/PDF import, export via PowerPoint / LibreOffice), `slide_reveal.py` (reveal points one by one, aligned to narration sentences), `slide_sequence.py` (order of a slide and its videos: narration first, or video first with the text over the video revealed afterwards).
  - `second_subs.py` (second-language subtitles: VTT/SRT, web player package), `subtitles.py`, `langdetect.py`, `clips.py`, `asr.py`, `redact.py`, `cards.py` (intro / outro), `ffmpeg_util.py`.
- `static/`: the editor (`index.html`, `app.js`, `style.css`); `extension/`: the Chrome extension.
- `tools/`: `i18n_extract.py` (translation checks), `selftest.py`, `dump_llm_payload.py` (prints what would be sent to the model, offline).
- `packaging/build_exe.py`: PyInstaller packaging.

## Interface translations (i18n)

- **The Chinese source text is the key:** `t("中文")`, `t('已删除 {n} 个', { n })` in Python / JS; translations live in `static/locales/{en,de,fr,pl,it,es,nl}.json` (the extension uses `extension/locales/` and `extension/_locales/`). Counts use ICU plurals: `{n, plural, one {# page} other {# pages}}`.
- Chinese text that users never see (LLM prompts, regexes) gets `# i18n: ignore` on that line.
- After changing UI text run `python tools/i18n_extract.py`; `--missing de` lists missing keys. **All 7 languages must be completed**; remove unused keys. Button names mentioned in translations must match the actual buttons in that language.
- Text written into videos follows the project's narration language (`i18n.content_lang`), not the interface language.

## Narration / second-subtitle languages

- One table, `script_gen.LANG_GROUPS` (its order is the UI order: Chinese → European → others); `/api/languages` and `/api/health` (used by the extension) both come from it.
- Adding a language also needs `tts.DEFAULT_VOICES` (female), `dialogue.MALE_VOICES` and a preview sentence in `main.tts_preview`. Notes-language detection in `langdetect.py` and `guessLang` in `static/app.js` implement the same rules and must be changed together. `tests/test_languages.py` checks all of this.

## Tests

- `.venv\Scripts\python.exe -X utf8 tests\run_all.py --lint` (about 10 minutes; `-k name` runs a subset; `--exe` / `--ppt` see `tests/README.md`).
- Run the related tests after a change, all of them after larger changes. Write new tests like the existing ones (see `tests/README.md`) and add them to `run_all.py`.
- Use `tests/fixtures_build.py` for a synthetic recorded project — never copy a user's project.
- Check UI changes in the browser with `ui-test` from `.claude/launch.json` (port 8770, separate data).

## Version, commits, packaging (only when asked)

- The version lives in `backend/main.py` (`FastAPI(version=...)`) and `extension/manifest.json`; update both READMEs as well.
- Commits: author Zhao Long <comlong@gmail.com> (set in the repo config); messages in English, first line "StepCast x.y.z: topic", then bullet points; lightweight tag `vx.y.z` after a release commit.
- Packaging: `.venv\Scripts\python.exe -X utf8 packaging\build_exe.py --with-model` → `dist\StepCast-<version>-win64.zip` and `-with-model-small.zip`; then run `tests\run_all.py --exe -k none` and make sure the package contains no `config.json` / `projects/`.

## Editing tips (Windows)

- Use the Write / Edit tools for content with backslashes (`\n`, regexes, `\uXXXX`); bash heredocs mangle them.
- Run Python that prints Chinese with `python -X utf8` and `PYTHONIOENCODING=utf-8`.
- `ffmpeg` / `ffprobe` are taken from PATH; the packaged app brings its own ffmpeg.
