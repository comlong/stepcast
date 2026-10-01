# StepCast 开发说明（给 Claude 看的）

本地运行的教学视频生成器：Chrome 扩展录制网页操作，或者导入 PPT / PDF → AI 写解说 → 配音 → 渲染成带字幕的 MP4。
公司内部使用，Windows 为主。

## 必须遵守

- **和用户对话一律用中文**，包括中途的进度说明。代码注释、提交说明也用中文（和现有风格一致）。
- **只在用户开口时才做**：重启 run.bat、提交 git、改版本号、打包。改完代码不要顺手做这些。
- **测试不花钱**：不调用真实的大模型 / 云端配音，用假客户端、假 key。
- **测试不碰用户的东西**：独立的 `VT_DATA_DIR` 和 `config.CONFIG_PATH`，不改真实的 `config.json`，不删用户的项目。
  读用户的真实项目（`projects/`）做诊断可以，只读。
- **不修改注册表**（整个项目，包括打包、安装、测试）。
- **不关闭用户的 PowerPoint**：代码只退出它自己启动的 PowerPoint；需要 PowerPoint 的测试只在 PowerPoint 没开着时跑。
- **「关于」页面不点名任何公司**；免责声明只放在「关于」页。
- 大模型的名字只显示「deepseek」，不显示 `-chat`、`-flash` 之类的后缀（实际仍用 deepseek-chat）。
- **发布包里绝不能有 `config.json`（含 API Key）和 `projects/`。**
- **公司内部资料不进 git**：真实 PPT、真实项目的截图 / 文案。测试素材只用自己生成的；
  内部资料放 `tests/private/`（已忽略）。用户的真实项目只用来诊断，不能拷进测试或提交。
- 不在登录框里输入密码，也不在对话里要密码 / API Key。

## 运行

- `setup.bat` 建 `.venv` 装依赖；`run.bat` → `app.py` → FastAPI，地址 `http://127.0.0.1:8756`。
- 重启 run.bat（用户要求时）：找到监听 8756 的进程结束掉；它的父进程如果是运行 run.bat 的 cmd 窗口也一起关，
  然后在项目目录下 `Start-Process cmd.exe -ArgumentList '/c', "$PWD\run.bat" -WorkingDirectory $PWD`（新开一个窗口跑）。

## GitHub

- 公开仓库：github.com/comlong/stepcast。只有 1.8.0 起的历史；更早的历史只在本地备份里，不公开。
- 推送只推 `main` 和新的版本标签（`git push origin main vX.Y.Z`），**不要** `git push --tags` / `--all`。
- 安装包放在 GitHub Releases（`gh release create`），不进仓库。

## 代码结构

- `backend/main.py`：全部 API；`app = FastAPI(title="StepCast", version=...)` 是版本号的来源。
- `backend/storage.py`：项目 = `projects/<pid>/project.json` + `screenshots/` `audio/` `output/`。
  后台任务先拷一份项目，做完用 `storage.commit(pid, before, after, ...)` 三方合并回去（用户在此期间的编辑不丢）；
  `main.STEP_TEXT`（文案）、`storage.AUDIO_FIELDS`（配音）是要整体合并的字段组。简单修改用 `storage.update(pid, fn)`。
- `backend/models.py`：Pydantic 模型（Project / Step / DialogueLine / SubtitleTrack …）。
- `backend/config.py`：`config.json`；`config.save` 只保留 `DEFAULTS` 里有的键 —— 加新设置必须先加进 `DEFAULTS`。
- `backend/services/`
  - `jobs.py`：后台任务；`HEAVY` 里的任务同一项目互斥。
  - `script_gen.py`：写解说（网页录制 / 幻灯片 / 双人问答），`llm.py` + `llm_openai.py` / `llm_anthropic.py`：各家模型（`Preset`）。
  - `tts.py`（Edge 配音、按前缀分发）、`tts_cloud.py`（豆包 / MiniMax / 通义，音色 id 带 `doubao:` 等前缀）、`voice.py`、`dialogue.py`（双人台词逐句合成）。
  - `video.py`（整体渲染、分段并行、编码器回退）、`renderer.py`（PIL 画每一帧：字幕、光标、放大、幻灯片布局）、`textlayout.py`、`gdi_text.py`。
  - `slides.py`（PPT/PDF 导入，PowerPoint / LibreOffice 导出）、`slide_reveal.py`（逐条出现：条目和解说句子对齐）。
  - `second_subs.py`（第二语言字幕：VTT/SRT、网页播放包）、`subtitles.py`、`langdetect.py`、`clips.py`、`asr.py`、`redact.py`、`cards.py`（片头片尾）、`ffmpeg_util.py`。
- `static/`：编辑器（`index.html`、`app.js`、`style.css`）；`extension/`：Chrome 扩展。
- `tools/`：`i18n_extract.py`（翻译检查）、`selftest.py`、`dump_llm_payload.py`（看发给模型的内容，不联网）。
- `packaging/build_exe.py`：PyInstaller 打包。

## 多语言界面（i18n）

- **中文原文就是 key**：Python / JS 里写 `t("中文")`、`t('已删除 {n} 个', { n })`；译文在 `static/locales/{en,de,fr,pl,it,es,nl}.json`
  （扩展在 `extension/locales/` 和 `extension/_locales/`）。数量用 ICU 复数：`{n, plural, one {# page} other {# pages}}`。
- 给模型的提示词、正则等不给用户看的中文，在那一行加 `# i18n: ignore`。
- 改了界面文字后：`python tools/i18n_extract.py` 检查，`--missing de` 列出缺的 key，**7 种语言都要补**；删掉不用的旧 key。
  译文里提到的按钮名要和该语言界面上真实的按钮名一致。
- 写进视频的文字跟项目的解说语言（`i18n.content_lang`），不跟界面语言。

## 解说语言 / 第二字幕语言

- 一张表：`script_gen.LANG_GROUPS`（顺序就是界面顺序：中文 → 欧洲语言 → 其他），`/api/languages` 和 `/api/health`（Chrome 扩展用）都从它出。
- 加一种语言要一起补：`tts.DEFAULT_VOICES`（女声）、`dialogue.MALE_VOICES`、`main.tts_preview` 的试听句；
  备注语言判断 `langdetect.py` 和 `static/app.js` 的 `guessLang` 是同一套规则，要两边一起改。`tests/test_languages.py` 会检查这些。

## 测试

- `.venv\Scripts\python.exe -X utf8 tests\run_all.py --lint`（约 20 分钟；`-k 名字` 只跑一部分；`--exe` / `--ppt` 见 `tests/README.md`）。
- 改完功能要跑相关测试；较大的改动跑全部。新测试照 `tests/README.md` 写，并加进 `run_all.py`。
- 需要网页录制项目时用 `tests/fixtures_build.py` 造假的，不要拷用户的项目。
- 界面改动在浏览器里看效果：`.claude/launch.json` 的 `ui-test`（8770 端口，独立数据）。

## 版本、提交、打包（都等用户开口）

- 版本号两处：`backend/main.py` 的 `FastAPI(version=...)`、`extension/manifest.json`；同时更新 `README.md`。
- 提交：作者 Zhao Long <comlong@gmail.com>（仓库里已设置）；说明用中文，第一行「StepCast x.y.z：主题」，下面列要点；提交后打轻量 tag `vx.y.z`。
- 打包：`.venv\Scripts\python.exe -X utf8 packaging\build_exe.py --with-model` → `dist\StepCast-<版本>-win64.zip` 和 `-with-model-small.zip`；
  打完跑 `tests\run_all.py --exe -k none` 验证 exe，并确认包里没有 `config.json` / `projects/`。

## 编辑技巧（Windows）

- 内容里有反斜杠（`\n`、正则、`\uXXXX`）时用 Write / Edit 工具写文件，别用 bash heredoc（反斜杠会被改掉）。
- 跑含中文的 Python 用 `python -X utf8`，环境变量 `PYTHONIOENCODING=utf-8`。
- `ffmpeg` / `ffprobe` 从 PATH 找；打包版带自己的 ffmpeg。
