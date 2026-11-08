# StepCast local API (for assistants)

Base URL `http://127.0.0.1:8756` (only reachable from this computer). JSON in and out; send UTF-8.
Long work runs as a background job: the call returns `{"id": job id}`; poll `GET /api/jobs/{id}` until `status` is `done` or `error` (`progress` 0–1, `message`, `result`, `error`).
This page lists what the skill needs; `GET /openapi.json` has every endpoint. Step `index` starts at 0.

## State

| Call | Purpose |
|---|---|
| `GET /api/health` | version, `ai_disabled`, `data_dir` (projects folder), languages, `web_browser` (Edge / Chrome for HTML decks, "" = none) |
| `GET /api/settings` / `POST /api/settings {"ai_disabled": true}` | read / change global settings |
| `GET /api/projects` | list projects |
| `GET /api/projects/{pid}` | the whole project: `steps[]` with `id`, `index`, `kind`, `target.text`, `target.rect`, `value`, `page_title`, `url`, `screenshot`, `title`, `narration`, `caption`, `include`, `zoom`, `highlight`, `audio`; slide steps also `slide_text`, `slide_notes` |
| `GET /api/projects/{pid}/file/screenshots/{file}` | a step's screenshot or slide picture (or read `<data_dir>/<pid>/screenshots/<file>` directly) |

## PPT / PDF / HTML import

| Call | Purpose |
|---|---|
| `POST /api/import/slides` (multipart, field `file`: .pptx / .ppt / .pdf, or an HTML deck: .html / .htm, or a .zip of its folder) | job: read the deck; `result` is the manifest: `id` (`imp_…`), `count`, `with_notes`, `engine`, `slides[{i, title, text, notes, hidden}]` (`i` starts at 1). HTML decks also have `web` (`kind`, `nav`, `cues`: slides whose notes have stage directions, `missing` / `failed` files) and per slide `web.controls` (what can be clicked) |
| `GET /api/import/slides/{iid}` / `GET /api/import/slides/{iid}/thumb/{i}?full=true` | the manifest again / a slide picture |
| `POST /api/import/slides/{iid}/create {"name", "selected": [1, 2, …], "language", "voice", "notes_mode": "verbatim", "missing": "empty", "auto_voice": false, "auto_generate": false}` | job: the project, one `slide` step per slide (plus `video` steps for videos in slides); `result.project_id`, `result.warning`. With `notes_mode` `verbatim` and `missing` `empty` no AI is used: notes in the narration language become the narration, the rest stays empty. Other values (`missing: "ai"`, `notes_mode` `reference` / `ignore`) need StepCast's AI. Optional: `reveal` (default true), `videos` (default true); HTML decks: `web_mode` (`demo` = record the animations and do the clicks the notes ask for, default / `animate` = animations only, no clicks; parts shown one at a time still come in / `still`), `web_cursor` (default true: a mouse pointer moves to what is clicked), `web_focus` (default true: what the narration names on the slide is highlighted; project setting `web_focus`) |

## HTML slides

A slide of an HTML deck (`step.web` is set) is recorded moving when the video is rendered: its own animations, and clicks on its controls while the narration plays. Stage directions in its notes — a bracket starting with a verb of clicking, `(click the "Run demo" button)`, `（点按钮）`, `（依次点 1 到 4 号热点）` — are never read aloud and become the clicks, each at the sentence it belongs to; without directions a control is clicked when the narration names it (its text or aria-label). A piece of content on the slide (card, list item, table row) lights up when the narration uses its words, so name them as the slide does. Write such directions into the narration's source when you want a click, or set the clicks yourself (the script's `web` command makes these calls):

| Call | Purpose |
|---|---|
| `GET /api/projects/{pid}` | `steps[].web`: `controls[{text, name, …}]` (index = control number), `blocks[{key, text, …}]` (pieces of content read from the page: cards, list items, table rows; index = block number), `actions[{kind, control, pos, unit}]`, `units[{a, b, text}]` (the narration's sentences), `manual`, `live` |
| `PUT /api/projects/{pid}/steps/{sid}/web {"actions": [{"kind": "click" \| "hover" \| "next" \| "focus", "control": n, "unit": sentence}]}` | set the actions by hand: each at the start of sentence `unit` (0-based; `len(units)` = after the narration, the slide then stays until what the click started has finished). `next` shows the slide's next part (fragments). `focus` highlights block `control`. They stay as set, following their sentence when the narration changes |
| `PUT … {"auto": true}` / `PUT … {"live": false}` | place them automatically again from the notes and narration, clicks included / make the slide a still image |

## Building a project

| Call | Purpose |
|---|---|
| `POST /api/projects {"name", "language"}` | new empty project |
| `PATCH /api/projects/{pid} {"title", "subtitle", "intro", "outro", "language", "voice", "settings": {…}}` | project texts and per-project settings (`intro_enabled`, `outro_enabled`, `burn_subtitles`, `second_sub_space`, `sidecar_subtitles`, `zoom_enabled`, `show_cursor` …) |
| `POST /api/projects/{pid}/steps/copy {"source", "steps": [ids or indexes], "crop", "position"}` | copy recorded steps from another project (screenshots cropped, coordinates shifted; no voice-over) |
| `POST /api/projects/{pid}/steps/crop {"crop", "steps": [ids]}` | cut pixels from every screenshot edge in place (all steps when `steps` is left out). Each call cuts again |
| `POST /api/projects/{pid}/steps/image {"image": base64 PNG/JPG, "title", "narration", "slide_text", "caption", "position"}` | insert a picture as a slide step (max 25 MB) |
| `PATCH /api/projects/{pid}/steps/{sid} {"title", "narration", "caption", "include", "zoom", "highlight", "duration_override", "target_rect": {x,y,w,h}}` | edit one step; `target_rect` is in CSS pixels of the recorded page |
| `PUT /api/projects/{pid}/steps {"steps": [{id, …}]}` | reorder / bulk edit: steps left out are removed |
| `DELETE /api/projects/{pid}/steps/{sid}` | remove a step |
| `GET /api/projects/{pid}/export/script` / `POST /api/projects/{pid}/import/script` | all texts as JSON (`title`, `subtitle`, `intro`, `outro`, `steps[{index, id, title, narration, caption, include}]`) and back. On import a step item with `id` is matched by id only, one without by `index`; items that match nothing are ignored (the script's `script-import` refuses such a file). A changed narration also becomes the subtitle unless `caption` was changed too or was a text of its own |

## Voice-over and video

| Call | Purpose |
|---|---|
| `POST /api/projects/{pid}/tts {"voice", "only_missing": true}` | job: voice-over of steps (and intro / outro) without voice |
| `POST /api/projects/{pid}/render {"width", "height", "fps", "burn_subtitles", "intro_enabled", "outro_enabled", "second_sub_space", "sidecar_subtitles"}` | job: render; `result.file` is the MP4 under `<data_dir>/<pid>/output/` |
| `GET /api/projects/{pid}/file/output/{name}?download=true` | the video, or `<video>.srt` / `.vtt` (made on request when not saved next to the video) |

## Second-language subtitles (translated by you)

| Call | Purpose |
|---|---|
| `GET /api/projects/{pid}/subtitles2/sentences` | `{"key", "sentences": [{i, start, end, text}]}` of the current video |
| `POST /api/projects/{pid}/subtitles2/{lang}/import {"key", "texts": [one per sentence]}` | save the translation as `<video>.<lang>.vtt / .srt`; `key` (from `sentences`) is required: a video rendered again since then is refused |
| `GET /api/projects/{pid}/subtitles2` / `DELETE /api/projects/{pid}/subtitles2/{lang}` | list / delete tracks |

## Don't call (they use StepCast's own AI and fail while it is off)

`POST /script`, `/auto`, `/steps/{sid}/rewrite`, `/translate`, `POST /subtitles2`, `/notes/narration`, `/steps/{sid}/voice/ai`, `/voice/all-ai`, `/magic-mic`, and slide import with anything but `notes_mode: "verbatim"` + `missing: "empty"`.

## Errors

`400` bad input (the `detail` says what), `404` project / step / file not found, `409` another heavy job is running on this project (wait), `413` upload too large.
