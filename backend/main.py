"""FastAPI service: receives recordings from the extension, serves the editor and generates scripts / voice-over / videos."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import config, i18n, storage
from .models import (CardStyle, CaptureStepReq, DialogueLine, Project, Rect, RedactionsReq,
                     RedactScanReq, RenderReq, ScriptReq, SlidesCreateReq, StartCaptureReq, Step,
                     Target, TranslateReq, TTSReq, drop_stale_lines, join_lines)
from .services import (asr, cards, clips, dialogue, ffmpeg_util, jobs, llm, redact, script_gen, second_subs, slides,
                       tts, tts_cloud, video, voice)

app = FastAPI(title="StepCast", version="1.8.0")

# ---- local access only ---------------------------------------------------
# The service holds screenshots of the internal systems you recorded and can use your LLM API keys. With open CORS any web page
# you visit could call this local service in the background: read screenshots, or point the API endpoint to someone else's server to capture keys.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip() for h in os.environ.get("VT_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _origin_allowed(origin: str, host: str) -> bool:
    if origin.startswith("chrome-extension://"):
        return True                                  # this project's recording extension
    try:
        o = urlparse(origin)
        return o.netloc == host                      # the editor page itself (same origin)
    except Exception:
        return False


class LocalOnly:
    """Reject cross-site requests (Origin is neither this service nor the extension) and DNS rebinding (Host isn't local)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        hostname = urlparse("//" + host).hostname or ""
        origin = headers.get("origin", "")
        reason = ""
        if hostname not in LOCAL_HOSTS:
            reason = i18n.t("只允许通过 127.0.0.1 / localhost 访问")
        elif origin and origin != "null" and not _origin_allowed(origin, host):
            reason = i18n.t("拒绝来自其他网站的请求")
        if reason:
            resp = JSONResponse({"detail": reason}, status_code=403)
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LocalOnly)

# Background jobs that rewrite a project only merge these fields (see storage.commit)
STEP_TEXT = ("title", "narration", "caption", "lines")
AUDIO = storage.AUDIO_FIELDS
CARD = ("title", "subtitle", "intro", "outro", "summary")


def _snapshot(pid: str):
    """Project snapshot at the start of a job + a copy that can be modified freely."""
    before = _need(pid)
    return before, before.model_copy(deep=True)


@contextmanager
def _keep_done_on_stop(pid: str, before: Project, proj: Project, **commit_kw):
    """When a job is stopped, merge the finished part (e.g. steps already voiced) back into the project, then exit."""
    try:
        yield
    except jobs.JobCancelled:
        storage.commit(pid, before, proj, **commit_kw)
        raise


def _submit(kind: str, fn, pid: str = "", exclusive=None):
    try:
        return jobs.submit(kind, fn, pid, exclusive=exclusive)
    except jobs.JobConflict as e:
        raise HTTPException(409, str(e))

# Current recording state (shared by the extension and the editor)
_recording: Dict[str, Any] = {"project_id": "", "started_at": 0.0, "last_step_at": 0.0}


# ---- basics ---------------------------------------------------------------

def ui_language() -> str:
    """Current interface language. English if never set (or unsupported); switched with the dropdown at the top right."""
    return i18n.normalize(config.get("ui_language")) or i18n.FALLBACK


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    f = config.STATIC_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>static/index.html is missing</h1>", status_code=500)
    lang = ui_language()
    html = i18n.translate_html(f.read_text(encoding="utf-8"), lang)
    # Version scripts and styles by modification time: after an update the browser always gets the new files without a forced reload
    for name in ("app.js", "style.css", "i18n.js"):
        path = config.STATIC_DIR / name
        if path.exists():
            html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int(path.stat().st_mtime)}\"")
    # Embed the translations in the page: t() works as soon as the script loads, with no flash of Chinese text
    boot = json.dumps({"lang": lang, "langs": i18n.LANGS, "dict": i18n.catalog(lang)},
                      ensure_ascii=False).replace("</", "<\\/")
    html = html.replace('<html lang="zh-CN">', f'<html lang="{lang}">', 1)
    html = html.replace("<script src=\"/static/i18n.js",
                        f"<script>window.VT_I18N = {boot};</script>\n<script src=\"/static/i18n.js", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/i18n")
def get_i18n(lang: str = ""):
    """For the Chrome extension: current interface language and dictionary."""
    code = i18n.normalize(lang) or ui_language()
    return {"lang": code, "langs": i18n.LANGS, "dict": i18n.catalog(code)}


@app.get("/api/health")
def health():
    cfg = config.load()
    return {
        "ok": True,
        "version": app.version,
        "ffmpeg": ffmpeg_util.available(),
        "ui_language": ui_language(),
        "llm": {k: v for k, v in llm.public_state(cfg).items() if k != "providers"},
        "recording": _recording["project_id"],
        "data_dir": str(config.DATA_DIR),
        "asr": asr.status(),
        "powerpoint": slides.powerpoint_available(),
        "languages": language_list(),        # the extension's narrate-while-recording language list comes from here
    }


@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    safe = {k: v for k, v in cfg.items() if k not in ("deepseek_api_key", "llm_providers", "tts_services")}
    safe["llm"] = llm.public_state(cfg)     # keys are only returned masked
    safe["tts_services"] = tts_cloud.public_state(cfg)
    return safe


@app.post("/api/settings")
def set_settings(patch: Dict[str, Any] = Body(...)):
    if patch.get("deepseek_api_key") in ("", None):
        patch.pop("deepseek_api_key", None)
    if "ui_language" in patch:
        patch["ui_language"] = i18n.normalize(patch["ui_language"]) or ui_language()
    config.save(tts_cloud.merge_settings_patch(llm.merge_settings_patch(patch)))
    return get_settings()


@app.post("/api/settings/test-tts")
def settings_test_tts(body: Dict[str, Any] = Body(default={})):
    """Test a paid voice service by synthesising a short sentence. Uses the saved key if none is given."""
    return tts_cloud.test(str(body.get("service") or ""), str(body.get("api_key") or ""))


@app.post("/api/settings/test-key")
def settings_test_key(body: Dict[str, Any] = Body(default={})):
    """Test the connection. Empty fields use the saved values, so changing just the model can be tested too."""
    return llm.test_connection(str(body.get("provider") or ""), str(body.get("api_key") or ""),
                               str(body.get("base_url") or ""), str(body.get("model") or ""))


@app.post("/api/settings/llm-models")
def settings_llm_models(body: Dict[str, Any] = Body(default={})):
    return llm.list_models(str(body.get("provider") or ""), str(body.get("api_key") or ""),
                           str(body.get("base_url") or ""))


@app.get("/api/voices")
def get_voices(locale: str = "", refresh: bool = False):
    """Free Edge voices + voices of the paid voice services that have a key."""
    try:
        data = list(tts.list_voices(force=refresh))
    except Exception as e:
        data, err = [], e
    else:
        err = None
    cloud = tts_cloud.voices()
    if not data and not cloud and err is not None:
        raise HTTPException(503, str(err))
    data += cloud
    if locale:
        data = [v for v in data if v["locale"].lower().startswith(locale.lower())]
    return {"voices": data, "defaults": tts.DEFAULT_VOICES, "male_defaults": dialogue.MALE_VOICES}


@app.get("/api/languages")
def get_languages():
    return {"languages": language_list()}


def language_list() -> List[Dict[str, str]]:
    """Languages available for narration / second subtitles; group: zh (Chinese), europe (European), other."""
    return [{"code": k, "name": v, "group": script_gen.LANG_GROUP[k]} for k, v in script_gen.LANG_NAMES.items()]


# ---- recording (called by the Chrome extension) ----------------------------

@app.post("/api/capture/start")
def capture_start(req: StartCaptureReq):
    proj = storage.create(req.name, req.language)
    proj.recording = True
    storage.save(proj)
    _recording.update({"project_id": proj.id, "started_at": time.time(), "last_step_at": 0.0})
    return {"project_id": proj.id, "name": proj.name, "editor_url": f"http://127.0.0.1:{config.get('server_port')}/?p={proj.id}"}


@app.get("/api/capture/status")
def capture_status():
    pid = _recording["project_id"]
    if not pid or not storage.exists(pid):
        return {"recording": False, "project_id": "", "steps": 0}
    proj = storage.load(pid)
    return {
        "recording": bool(proj and proj.recording),
        "project_id": pid,
        "name": proj.name if proj else "",
        "steps": len(proj.steps) if proj else 0,
        "started_at": _recording["started_at"],
    }


@app.post("/api/capture/step")
def capture_step(req: CaptureStepReq):
    pid = req.project_id or _recording["project_id"]
    if not pid or not storage.exists(pid):
        raise HTTPException(404, i18n.t("项目不存在，请先开始录制。"))
    step = Step(
        kind=req.kind,
        url=req.url,
        page_title=req.page_title,
        target=req.target,
        point=req.point,
        value=req.value,
        viewport_w=req.viewport_w,
        viewport_h=req.viewport_h,
        redactions=req.redactions,
        text_nodes=req.text_nodes[:400],
    )
    # If the extension already found sensitive data on the page, mask it in the text fields as well
    if step.redactions:
        words = [r.label for r in step.redactions if r.label]
        if words:
            redact.scan_step(step, keywords=words, builtin=True, names=True,
                             also_mask_text=True)
    if req.screenshot_b64:
        try:
            fname, w, h = storage.save_screenshot(pid, step.id, req.screenshot_b64)
            step.screenshot, step.img_w, step.img_h = fname, w, h
        except Exception as e:
            raise HTTPException(400, i18n.t("截图保存失败：{error}", error=e))
    if not step.viewport_w and step.img_w and req.device_pixel_ratio:
        step.viewport_w = int(step.img_w / max(0.1, req.device_pixel_ratio))
    if req.client_ts:
        step.ts = req.client_ts / 1000.0      # event time in the browser; narrate-while-recording aligns by it
    count = storage.add_step(pid, step)
    _recording["last_step_at"] = time.time()
    return {"ok": True, "step_id": step.id, "index": step.index, "steps": count}


@app.post("/api/capture/stop")
def capture_stop(body: Dict[str, Any] = Body(default={})):
    pid = body.get("project_id") or _recording["project_id"]
    _recording["project_id"] = ""
    if not pid or not storage.exists(pid):
        return {"ok": False}

    def _do(p: Project):
        p.recording = False
    storage.update(pid, _do)
    proj = storage.load(pid)
    return {"ok": True, "project_id": pid, "steps": len(proj.steps) if proj else 0,
            "editor_url": f"http://127.0.0.1:{config.get('server_port')}/?p={pid}"}


# ---- projects -------------------------------------------------------------

@app.get("/api/projects")
def list_projects():
    return {"projects": storage.list_projects()}


@app.post("/api/projects")
def create_project(body: Dict[str, Any] = Body(default={})):
    proj = storage.create(body.get("name", ""), body.get("language", ""))
    return _public(proj)


def _public(proj: Project) -> Dict[str, Any]:
    """Project data for the editor. The text index is only used for server-side redaction scans and can be several MB, so it isn't sent."""
    data = proj.model_dump(exclude={"steps": {"__all__": {"text_nodes"}}})
    for d, s in zip(data["steps"], proj.steps):
        d["text_nodes_count"] = len(s.text_nodes)
    return data


def _need(pid: str) -> Project:
    proj = storage.load(pid)
    if proj is None:
        raise HTTPException(404, i18n.t("项目不存在"))
    return proj


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _public(_need(pid))


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: Dict[str, Any] = Body(...)):
    fields = {"name", "title", "subtitle", "intro", "outro", "summary",
              "language", "voice"}

    def _do(p: Project):
        for k, v in body.items():
            if k in fields:
                if k == "voice" and p.is_dialogue():
                    continue              # Q&A voices are set per speaker under "🎭 Speakers", not by the global default voice
                if k == "language" and p.is_dialogue() and v and v != p.language:
                    dialogue.retarget(p, v)
                # intro / outro text changed: the old voice-over is outdated, otherwise the next synthesis would skip it
                if k in ("intro", "outro") and (v or "") != (getattr(p, k) or ""):
                    (storage.audio_dir(pid) / f"__{k}__.mp3").unlink(missing_ok=True)
                setattr(p, k, v)
        if isinstance(body.get("settings"), dict):
            p.settings = {**(p.settings or {}), **body["settings"]}
    storage.update(pid, _do)
    return _public(_need(pid))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    return {"ok": storage.delete(pid)}


@app.put("/api/projects/{pid}/steps")
def replace_steps(pid: str, body: Dict[str, Any] = Body(...)):
    """Submit the whole step list: reordering, bulk edits, deletion."""
    incoming: List[Dict[str, Any]] = body.get("steps", [])

    def _do(p: Project):
        by_id = {s.id: s for s in p.steps}
        new_list: List[Step] = []
        editable = {"title", "narration", "caption", "note", "include", "zoom",
                    "highlight", "duration_override", "value", "slide_notes"}
        for item in incoming:
            sid = item.get("id")
            s = by_id.get(sid)
            if not s:
                continue
            for k in editable:
                if k in item:
                    if k == "narration" and (item[k] or "") != (s.narration or "") \
                            and s.voice_source != "own":
                        s.audio = ""
                        s.audio_duration = 0.0
                        s.boundaries = []
                    setattr(s, k, item[k])
            drop_stale_lines(s)
            if item.get("target_rect"):
                if s.target is None:
                    s.target = Target()
                s.target.rect = Rect(**item["target_rect"])
            new_list.append(s)
        if new_list:
            p.steps = new_list
            storage.reindex(p)
    storage.update(pid, _do)
    return _public(_need(pid))


@app.patch("/api/projects/{pid}/steps/{sid}")
def patch_step(pid: str, sid: str, body: Dict[str, Any] = Body(...)):
    editable = {"title", "narration", "caption", "note", "include", "zoom",
                "highlight", "duration_override", "value", "slide_notes"}

    def _do(p: Project):
        for s in p.steps:
            if s.id == sid:
                for k, v in body.items():
                    if k in editable:
                        if k == "narration" and (v or "") != (s.narration or "") \
                                and s.voice_source != "own":
                            s.audio = ""
                            s.audio_duration = 0.0
                            s.boundaries = []
                        setattr(s, k, v)
                if "narration" in body and "lines" not in body:
                    drop_stale_lines(s)
                if isinstance(body.get("lines"), list):          # Q&A lines: narration = all lines joined
                    lines = dialogue.normalize_lines(body["lines"])
                    text = join_lines([DialogueLine(**x) for x in lines])
                    if text != (s.narration or "") and s.voice_source != "own":
                        s.audio, s.audio_duration, s.boundaries, s.line_times = "", 0.0, [], []
                    s.lines = [DialogueLine(**x) for x in lines]
                    if s.caption_follows_narration() and (s.caption or "") in ("", s.narration or ""):
                        s.caption = text
                    s.narration = text
                if "reveal_enabled" in body and s.reveal is not None:
                    s.reveal.enabled = bool(body["reveal_enabled"])      # whether this slide reveals points one by one
                if body.get("target_rect"):
                    if s.target is None:
                        s.target = Target()
                    s.target.rect = Rect(**body["target_rect"])
                return s
        raise HTTPException(404, i18n.t("步骤不存在"))
    s = storage.update(pid, _do)
    return s.model_dump(exclude={"text_nodes"}) if hasattr(s, "model_dump") else {"ok": True}


@app.delete("/api/projects/{pid}/steps/{sid}")
def delete_step(pid: str, sid: str):
    gone: List[Step] = []

    def _do(p: Project):
        gone.extend(s for s in p.steps if s.id == sid)
        p.steps = [s for s in p.steps if s.id != sid]
        storage.reindex(p)
    storage.update(pid, _do)
    for s in gone:                      # a video step's file can be hundreds of MB, delete it too
        src = clips.resolve(pid, s.clip)
        if src is not None:
            src.unlink(missing_ok=True)
    return {"ok": True}


@app.put("/api/projects/{pid}/speakers")
def put_speakers(pid: str, body: Dict[str, Any] = Body(...)):
    """The two Q&A speakers: rename, change voices. Lines spoken by someone whose voice changed need new voice-over (the old one is outdated)."""
    _need(pid)

    def _do(p: Project):
        if not p.is_dialogue():
            raise HTTPException(400, i18n.t("这个项目不是双人问答模式"))
        changed = set()
        for sp in dialogue.ensure_speakers(p):
            new = next((x for x in body.get("speakers") or [] if isinstance(x, dict) and x.get("role") == sp.role), None)
            if not new:
                continue
            if "name" in new:
                sp.name = str(new["name"] or "").strip()[:20] or dialogue.voice_name(sp.voice)
            if new.get("voice") and new["voice"] != sp.voice:
                sp.voice = str(new["voice"])
                changed.add(sp.role)
        for s in p.steps:
            spoken = {ln.who for ln in s.lines} if s.lines else ({"host"} if s.narration else set())
            if spoken & changed and s.voice_source == "tts":
                s.audio, s.audio_duration, s.boundaries, s.line_times = "", 0.0, [], []
        if "host" in changed:
            p.voice = dialogue.speaker(p, "host").voice
            for k in ("intro", "outro"):                  # intro and outro are read by the host
                (storage.audio_dir(pid) / f"__{k}__.mp3").unlink(missing_ok=True)
        return {"speakers": [sp.model_dump() for sp in p.speakers], "changed": sorted(changed)}
    return storage.update(pid, _do)


@app.post("/api/projects/{pid}/steps/{sid}/rewrite")
def rewrite(pid: str, sid: str, body: Dict[str, Any] = Body(...)):
    proj = _need(pid)
    step = next((s for s in proj.steps if s.id == sid), None)
    if not step:
        raise HTTPException(404, i18n.t("步骤不存在"))
    instruction = body.get("instruction") or i18n.t("更简洁自然一些")
    if proj.is_dialogue() and step.lines:
        try:
            lines = script_gen.rewrite_dialogue(proj, step, instruction)
        except Exception as e:
            raise HTTPException(502, str(e))
        if body.get("apply", True):
            def _set(p: Project):
                for s in p.steps:
                    if s.id == sid:
                        script_gen.set_lines(s, lines)
                        s.voice_source = "tts"
            storage.update(pid, _set)
        return {"text": join_lines([DialogueLine(**x) for x in lines]), "lines": lines}
    try:
        text = script_gen.rewrite_step(proj, step, instruction)
    except Exception as e:
        raise HTTPException(502, str(e))
    if body.get("apply", True):
        def _do(p: Project):
            for s in p.steps:
                if s.id == sid:
                    if s.caption_follows_narration():
                        s.caption = text
                    s.narration = text
                    # the rewritten text no longer matches the recording, so the AI reads it (the recording stays on disk)
                    s.voice_source = "tts"
                    s.audio = ""
                    s.audio_duration = 0.0
                    s.boundaries = []
        storage.update(pid, _do)
    return {"text": text}


@app.post("/api/projects/{pid}/steps/{sid}/tts")
def step_tts(pid: str, sid: str, body: Dict[str, Any] = Body(default={})):
    proj = _need(pid)
    step = next((s for s in proj.steps if s.id == sid), None)
    if not step:
        raise HTTPException(404, i18n.t("步骤不存在"))
    if not (step.narration or "").strip():
        raise HTTPException(400, i18n.t("这一步还没有解说词"))
    path = storage.audio_dir(pid) / f"{step.id}.mp3"
    times: List[List[float]] = []
    try:
        if proj.is_dialogue() and step.lines:          # Q&A: synthesise line by line with each speaker's voice
            dur, bounds, times = dialogue.synth_lines(proj, step, path)
        else:
            voice = body.get("voice") or proj.voice or tts.default_voice(proj.language)
            if proj.is_dialogue():
                voice = dialogue.speaker(proj, "host").voice
            dur, bounds = tts.synth(step.narration, voice, path)
    except Exception as e:
        raise HTTPException(502, str(e))

    def _do(p: Project):
        for s in p.steps:
            if s.id == sid:
                s.audio = path.name
                s.voice_source = "tts"
                s.audio_duration = dur
                s.boundaries = bounds
                s.line_times = times
    storage.update(pid, _do)
    return {"audio": path.name, "duration": dur}


@app.get("/api/projects/{pid}/steps/{sid}/preview")
def step_preview(pid: str, sid: str, t: float = 1.2, scale: float = 0.5):
    proj = _need(pid)
    step = next((s for s in proj.steps if s.id == sid), None)
    if not step:
        raise HTTPException(404, i18n.t("步骤不存在"))
    try:
        data = video.render_step_preview(proj, step, t=t, scale=scale)
    except Exception as e:
        raise HTTPException(500, i18n.t("预览渲染失败：{error}", error=e))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---- redaction --------------------------------------------------------------

@app.put("/api/projects/{pid}/steps/{sid}/redactions")
def set_redactions(pid: str, sid: str, req: RedactionsReq):
    """Replace all redaction boxes of a step (drawing / deleting boxes in the editor goes through here)."""
    def _do(p: Project):
        for s in p.steps:
            if s.id == sid:
                s.redactions = req.redactions
                return s
        raise HTTPException(404, i18n.t("步骤不存在"))
    s = storage.update(pid, _do)
    return {"ok": True, "count": len(s.redactions)}


@app.post("/api/projects/{pid}/redact/scan")
def redact_scan(pid: str, req: RedactScanReq):
    """Scan all (or the given) steps with the built-in rules + keywords and add redaction boxes automatically."""
    _need(pid)

    def _do(p: Project):
        return redact.scan_project(
            p, keywords=req.keywords, builtin=req.builtin, names=req.names,
            names_guess=getattr(req, "names_guess", False),
            mode=req.mode, only_steps=req.steps or None)
    result = storage.update(pid, _do)
    result.update(redact.stats(_need(pid)))
    return result


@app.post("/api/projects/{pid}/redact/clear-auto")
def redact_clear_auto(pid: str, body: Dict[str, Any] = Body(default={})):
    """Remove automatically detected redaction boxes; hand-drawn ones stay."""
    _need(pid)
    removed = storage.update(pid, lambda p: redact.clear_auto(p, body.get("steps")))
    return {"removed": removed, **redact.stats(_need(pid))}


@app.post("/api/projects/{pid}/redact/clear-index")
def redact_clear_index(pid: str):
    """Clear the locally stored page text index. Existing redactions are not affected."""
    _need(pid)
    removed = storage.update(pid, lambda p: redact.clear_index(p))
    return {"removed": removed, **redact.stats(_need(pid))}


@app.get("/api/projects/{pid}/redact/stats")
def redact_stats(pid: str):
    return redact.stats(_need(pid))



# ---- voice input (record your voice / dictate / narrate while recording) ----

@app.get("/api/asr/status")
def asr_status():
    return asr.status()


@app.post("/api/asr/prepare")
def asr_prepare():
    """Download / load the speech recognition model in advance."""
    def work(job: jobs.Job):
        asr.get_model(job.progress)
        job.progress(1.0, i18n.t("语音识别模型已就绪"))
        return asr.status()
    return jobs.submit("asr_prepare", work)


def _step_or_404(proj: Project, sid: str) -> Step:
    step = next((s for s in proj.steps if s.id == sid), None)
    if not step:
        raise HTTPException(404, i18n.t("步骤不存在"))
    return step


# ---- video steps -------------------------------------------------------------

def _video_upload(file: UploadFile, prefix: str) -> Path:
    import shutil
    name = file.filename or "video.mp4"
    if Path(name).suffix.lower() not in clips.VIDEO_EXT:
        raise HTTPException(400, i18n.t("不支持的视频格式：{ext}（支持 mp4 / mov / wmv / avi / mkv / webm 等）",
                                        ext=Path(name).suffix or "?"))
    tmp_dir = config.DATA_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (_tmp_name(prefix) + Path(name).suffix.lower())
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return tmp


def _submit_upload(kind: str, work, pid: str, tmp: Path):
    try:
        return _submit(kind, work, pid, exclusive={"render", "auto"})
    except HTTPException:
        tmp.unlink(missing_ok=True)          # refused because a render is running: delete the uploaded temp file too
        raise


@app.patch("/api/projects/{pid}/steps/{sid}/video")
def video_options(pid: str, sid: str, body: Dict[str, Any] = Body(...)):
    """Video step: playback mode, trim, sound."""
    err: Dict[str, str] = {}

    def _do(p: Project):
        s = _step_or_404(p, sid)
        if s.kind != "video" or s.clip is None:
            err["msg"] = i18n.t("这一步不是视频")
            return
        c = s.clip.model_copy()
        try:
            if "mode" in body:
                if body["mode"] not in ("inset", "fullscreen", "poster"):
                    raise ValueError(i18n.t("不认识的播放方式"))
                if body["mode"] == "inset" and c.rect is None:
                    raise ValueError(i18n.t("这个视频不是从幻灯片里来的，没有原位置，只能全屏播放"))
                c.mode = body["mode"]
            if "audio" in body:
                if body["audio"] not in ("original", "mute"):
                    raise ValueError(i18n.t("不认识的声音设置"))
                c.audio = body["audio"]
            if "start" in body:
                c.start = max(0.0, float(body["start"] or 0))
            if "end" in body:
                c.end = max(0.0, float(body["end"] or 0))
            if c.duration:
                c.start = min(c.start, c.duration)
                c.end = min(c.end, c.duration) if c.end else 0.0
            if c.end and c.start >= c.end:
                raise ValueError(i18n.t("截取的起点要在终点之前"))
        except (TypeError, ValueError) as e:
            err["msg"] = str(e)
            return
        s.clip = c
    storage.update(pid, _do)
    if err:
        raise HTTPException(400, err["msg"])
    return _step_or_404(_need(pid), sid).model_dump(exclude={"text_nodes"})


@app.post("/api/projects/{pid}/steps/{sid}/video")
def video_upload(pid: str, sid: str, file: UploadFile = File(...)):
    """Upload / replace a video step's file (linked and online videos in a deck can't be retrieved and must be uploaded)."""
    step = _step_or_404(_need(pid), sid)
    if step.kind != "video":
        raise HTTPException(400, i18n.t("这一步不是视频"))
    tmp = _video_upload(file, "video")
    name = file.filename or "video.mp4"

    def work(job: jobs.Job):
        try:
            job.progress(0.2, i18n.t("读取视频…"))
            s = _step_or_404(_need(pid), sid)
            try:
                clip = clips.store(pid, s, tmp, name)
            except clips.ClipError as e:
                raise RuntimeError(str(e))
            poster = clips.save_poster(pid, clip) if clip.rect is None else None

            def _do(p: Project):
                st = _step_or_404(p, sid)
                st.clip = clip
                st.include = True               # it was excluded while the file was missing; with a video it goes into the final cut
                clips.use_poster(pid, st, poster)
            storage.update(pid, _do)
            job.progress(1.0, i18n.t("视频已更新（{sec} 秒）", sec=f"{clip.duration:.1f}"))
            return {"step": sid, "duration": clip.duration}
        finally:
            tmp.unlink(missing_ok=True)
    return _submit_upload("video", work, pid, tmp)


@app.post("/api/projects/{pid}/steps/video")
def video_insert(pid: str, file: UploadFile = File(...), after: str = Form("")):
    """Insert a video into the tutorial (after the step `after`, or at the end). Plays full screen."""
    _need(pid)
    tmp = _video_upload(file, "video")
    name = file.filename or "video.mp4"

    def work(job: jobs.Job):
        try:
            job.progress(0.2, i18n.t("读取视频…"))
            step = Step(kind="video", title=Path(name).stem[:60], zoom=False, highlight=False)
            try:
                step.clip = clips.store(pid, step, tmp, name)
            except clips.ClipError as e:
                raise RuntimeError(str(e))
            clips.use_poster(pid, step, clips.save_poster(pid, step.clip))

            def _do(p: Project):
                pos = next((i + 1 for i, s in enumerate(p.steps) if s.id == after), len(p.steps))
                p.steps.insert(pos, step)
                storage.reindex(p)
            storage.update(pid, _do)
            job.progress(1.0, i18n.t("已插入视频（{sec} 秒）", sec=f"{step.clip.duration:.1f}"))
            return {"step": step.id}
        finally:
            tmp.unlink(missing_ok=True)
    return _submit_upload("video", work, pid, tmp)


@app.post("/api/projects/{pid}/steps/{sid}/video/transcribe")
def video_transcribe(pid: str, sid: str):
    """Transcribe the speech in the video into this step's subtitle (aligned to when it is spoken)."""
    step = _step_or_404(_need(pid), sid)
    src = clips.resolve(pid, step.clip)
    if src is None:
        raise HTTPException(400, i18n.t("这一步还没有视频文件"))
    if not step.clip.has_audio:
        raise HTTPException(400, i18n.t("这个视频没有声音"))

    def work(job: jobs.Job):
        s = _step_or_404(_need(pid), sid)
        wav = storage.work_dir(pid) / f"{_tmp_name('clip_audio')}.wav"
        try:
            job.progress(0.03, i18n.t("取出视频里的声音…"))
            if clips.extract_audio(src, s.clip, wav) is None:
                raise RuntimeError(i18n.t("这个视频没有声音"))
            r = asr.transcribe(wav, _need(pid).language, progress=lambda f, m: job.progress(0.05 + f * 0.9, m))
            text = (r.get("text") or "").strip()
            # the extracted audio starts at the trim start: add it back to the word times so they count from the video's start and survive later trim changes
            start = clips.clip_range(s.clip)[0]
            words = [{**w, "t": round(w["t"] + start, 3)}
                     for w in asr.words_to_boundaries(r["segments"], 0.0, r["language"])]

            def _do(p: Project):
                st = _step_or_404(p, sid)
                st.caption = text
                if st.clip is not None:
                    st.clip.transcript, st.clip.words = text, words
            storage.update(pid, _do)
            job.progress(1.0, i18n.t("已识别成字幕") if text else i18n.t("没有识别到说话内容"))
            return {"text": text}
        finally:
            wav.unlink(missing_ok=True)
    return _submit("voice", work, pid, exclusive={"render", "auto"})


def _tmp_name(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@app.post("/api/transcribe")
def transcribe_upload(file: UploadFile = File(...), language: str = Form("")):
    """Dictation: return the text only, don't keep the audio."""
    src = voice.save_upload(file, config.DATA_DIR / "_tmp", _tmp_name("dictate"))

    def work(job: jobs.Job):
        try:
            return voice.dictate(src, language, job.progress)
        finally:
            src.unlink(missing_ok=True)
    return jobs.submit("dictate", work)


@app.post("/api/projects/{pid}/steps/{sid}/voice")
def step_voice_upload(pid: str, sid: str, file: UploadFile = File(...),
                      transcribe: bool = Form(True), replace_text: bool = Form(True),
                      language: str = Form("")):
    """Use the recorded or uploaded audio as this step's voice-over."""
    _step_or_404(_need(pid), sid)
    src = voice.save_upload(file, storage.work_dir(pid), _tmp_name(f"voice_{sid}"))

    def work(job: jobs.Job):
        try:
            before, proj = _snapshot(pid)
            r = voice.set_step_voice(proj, _step_or_404(proj, sid), src, transcribe,
                                     replace_text, language, job.progress)
            # the recording was uploaded on purpose, so the audio is always written back; the text is only replaced if you didn't edit it meanwhile
            storage.commit(pid, before, proj, step_fields=("narration", "caption") + AUDIO,
                           force_audio=True)
            return r
        finally:
            src.unlink(missing_ok=True)
    return _submit("voice", work, pid)


@app.post("/api/projects/{pid}/steps/{sid}/voice/ai")
def step_voice_to_ai(pid: str, sid: str):
    """Keep the text and let the AI read it."""
    _need(pid)
    storage.update(pid, lambda p: voice.switch_to_ai(p, _step_or_404(p, sid)))
    return {"ok": True}


@app.delete("/api/projects/{pid}/steps/{sid}/voice")
def step_voice_delete(pid: str, sid: str):
    _need(pid)
    storage.update(pid, lambda p: voice.remove_voice(p, _step_or_404(p, sid)))
    return {"ok": True}


@app.post("/api/projects/{pid}/voice/all-ai")
def all_steps_to_ai(pid: str):
    _need(pid)

    def _do(p: Project):
        n = 0
        for s in p.steps:
            if s.voice_source == "own":
                voice.switch_to_ai(p, s)
                n += 1
        return n
    return {"switched": storage.update(pid, _do)}


@app.post("/api/projects/{pid}/voice/remove-all")
def remove_all_voice(pid: str):
    _need(pid)

    def _do(p: Project):
        for s in p.steps:
            voice.remove_voice(p, s)
        for k in ("intro", "outro"):
            (storage.audio_dir(pid) / f"__{k}__.mp3").unlink(missing_ok=True)
        return len(p.steps)
    return {"cleared": storage.update(pid, _do)}


@app.post("/api/projects/{pid}/magic-mic")
def magic_mic_upload(pid: str, file: UploadFile = File(...), rec_start: float = Form(...),
                     mode: str = Form("ai"), language: str = Form("")):
    """Narrate while recording: the extension uploads the whole recording when recording stops; transcribe it and split it across the steps by action time."""
    _need(pid)
    src = voice.save_upload(file, storage.work_dir(pid), _tmp_name("session"))

    def work(job: jobs.Job):
        try:
            before, proj = _snapshot(pid)
            r = voice.import_session(proj, src, rec_start, mode, language,
                                     lambda f, m: job.progress(f * 0.9, m))
            storage.commit(pid, before, proj, step_fields=STEP_TEXT + AUDIO)
            if mode != "own":
                before, proj = _snapshot(pid)
                with _keep_done_on_stop(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers")):
                    tts.synth_project(proj, only_missing=True,
                                      progress=lambda f, m: job.progress(0.9 + f * 0.09, m))
                storage.commit(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers"))
            job.progress(1.0, i18n.t("讲解已写入 {n} 个步骤（保留原声）", n=r.get("steps", 0)) if mode == "own"
                         else i18n.t("讲解已写入 {n} 个步骤，并已生成 AI 配音", n=r.get("steps", 0)))
            return r
        finally:
            src.unlink(missing_ok=True)
    return _submit("magic_mic", work, pid)


# ---- PPT / PDF to video -------------------------------------------------------

@app.post("/api/import/slides")
def slides_upload(file: UploadFile = File(...)):
    import shutil
    name = file.filename or "slides.pptx"
    ext = Path(name).suffix.lower()
    if ext not in (".pptx", ".ppt", ".pdf"):
        raise HTTPException(400, i18n.t("只支持 .pptx / .ppt / .pdf"))
    tmp_dir = config.DATA_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (_tmp_name("slides") + ext)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    slides.cleanup_old_imports()

    def work(job: jobs.Job):
        return slides.analyze(tmp, name, job.progress)
    return jobs.submit("slides_analyze", work)


@app.get("/api/import/slides/{iid}")
def slides_manifest(iid: str):
    m = slides.load_manifest(iid)
    if not m:
        raise HTTPException(404, i18n.t("导入记录不存在"))
    return m


@app.get("/api/import/slides/{iid}/thumb/{n}")
def slides_thumb(iid: str, n: int, full: bool = False):
    if not slides.IID_RE.match(iid):
        raise HTTPException(404, i18n.t("没有这一页"))
    p = slides.import_dir(iid) / (f"slide_{n:03d}.png" if full else f"thumb_{n:03d}.jpg")
    if not p.exists():
        raise HTTPException(404, i18n.t("没有这一页"))
    return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/import/slides/{iid}/create")
def slides_create(iid: str, req: SlidesCreateReq):
    if not slides.load_manifest(iid):
        raise HTTPException(404, i18n.t("导入记录不存在，请重新上传"))

    def work(job: jobs.Job):
        return slides.create_project(iid, req, job.progress)
    return jobs.submit("slides_create", work)


# ---- intro / outro ----------------------------------------------------------

@app.get("/api/projects/{pid}/card/{kind}/preview")
def card_preview(pid: str, kind: str, t: float = 1.2, scale: float = 0.5):
    if kind not in ("intro", "outro"):
        raise HTTPException(404, i18n.t("只支持 intro / outro"))
    proj = _need(pid)
    try:
        data = video.render_card_preview(proj, kind, t=t, scale=scale)
    except Exception as e:
        raise HTTPException(500, i18n.t("预览渲染失败：{error}", error=e))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _card_kind(kind: str) -> str:
    if kind not in cards.KINDS:
        raise HTTPException(404, i18n.t("只支持 intro / outro"))
    return kind


@app.post("/api/projects/{pid}/card/{kind}/background")
def card_background_upload(pid: str, kind: str, file: UploadFile = File(...)):
    """Use your own intro / outro background: an image or one page of a PPT / PDF (PPT needs conversion, done as a background job)."""
    import shutil
    _card_kind(kind)
    _need(pid)
    name = file.filename or "background.png"
    ext = Path(name).suffix.lower()
    if ext not in cards.IMAGE_EXT | cards.DOC_EXT:
        raise HTTPException(400, i18n.t("只支持图片（PNG / JPG / WebP）、PPT 或 PDF"))
    tmp_dir = config.DATA_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (_tmp_name("card") + ext)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    def work(job: jobs.Job):
        try:
            fields = cards.import_background(pid, kind, tmp, name, job.progress)
        except (cards.CardError, slides.SlidesError) as e:
            raise RuntimeError(str(e))
        finally:
            tmp.unlink(missing_ok=True)
        old: Dict[str, Any] = {}

        def _do(p: Project):
            style = getattr(p, f"{kind}_card")
            old["style"] = style.model_copy()
            duration = style.duration                     # the display time was set by the user; changing the image doesn't reset it
            setattr(p, f"{kind}_card", CardStyle(**fields, duration=duration))
        storage.update(pid, _do)
        cards.remove_folder(pid, old.get("style"))
        job.progress(1.0, i18n.t("背景已更新"))
        return getattr(_need(pid), f"{kind}_card").model_dump()
    # Changing the background during a render would leave the half-rendered intro / outro without an image: wait for the render first
    try:
        return _submit("card_bg", work, pid, exclusive={"render", "auto"})
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise


@app.patch("/api/projects/{pid}/card/{kind}")
def card_options(pid: str, kind: str, body: Dict[str, Any] = Body(...)):
    """Intro / outro: page, fit, text overlay, display time."""
    _card_kind(kind)
    _need(pid)
    err: Dict[str, str] = {}

    def _do(p: Project):
        try:
            cards.apply_options(pid, getattr(p, f"{kind}_card"), body)
        except (cards.CardError, ValueError, TypeError) as e:
            err["msg"] = str(e)
    storage.update(pid, _do)
    if err:
        raise HTTPException(400, err["msg"])
    return _public(_need(pid))


@app.delete("/api/projects/{pid}/card/{kind}/background")
def card_background_remove(pid: str, kind: str):
    """Restore the default background (the display time is kept)."""
    _card_kind(kind)
    _need(pid)
    old: Dict[str, Any] = {}

    def _do(p: Project):
        style = getattr(p, f"{kind}_card")
        old["style"] = style.model_copy()
        setattr(p, f"{kind}_card", CardStyle(duration=style.duration))
    storage.update(pid, _do)
    cards.remove_folder(pid, old.get("style"))
    return _public(_need(pid))


@app.post("/api/projects/{pid}/card/{kind}/tts")
def card_tts(pid: str, kind: str, body: Dict[str, Any] = Body(default={})):
    if kind not in ("intro", "outro"):
        raise HTTPException(404, i18n.t("只支持 intro / outro"))
    proj = _need(pid)
    text = (proj.intro if kind == "intro" else proj.outro) or ""
    if not text.strip():
        raise HTTPException(400, i18n.t("片头还没有旁白文案") if kind == "intro" else i18n.t("片尾还没有旁白文案"))
    voice = body.get("voice") or proj.voice or tts.default_voice(proj.language)
    try:
        dur = tts.synth_card(storage.audio_dir(pid), kind, text, voice)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"audio": f"__{kind}__.mp3", "duration": dur}


@app.post("/api/tts/preview")
def tts_preview(body: Dict[str, Any] = Body(default={})):
    """Voice preview: synthesise a short sample without touching any project data."""
    voice = body.get("voice") or config.get("voice")
    text = (body.get("text") or "").strip()[:200]
    if not text:
        loc = (tts_cloud.voice_info(voice).get("locale", "zh") if tts_cloud.is_cloud(voice)
               else voice.split("-")[0] if voice else "en").split("-")[0]
        # the preview sentence is in the voice's own language (independent of the interface language)
        text = {
            "zh": "你好，这是当前音色的试听效果。",  # i18n: ignore
            "ja": "こんにちは、これは音声のサンプルです。",  # i18n: ignore
            "ko": "안녕하세요, 음성 미리 듣기입니다.",
            "de": "Hallo, so klingt diese Stimme.",
            "fr": "Bonjour, voici un aperçu de cette voix.",
            "pl": "Dzień dobry, tak brzmi ten głos.",
            "it": "Ciao, questa è un'anteprima di questa voce.",
            "es": "Hola, así suena esta voz.",
            "nl": "Hallo, zo klinkt deze stem.",
            "pt": "Olá, esta é uma amostra desta voz.",
            "ca": "Hola, així sona aquesta veu.",
            "gl": "Ola, así soa esta voz.",
            "sv": "Hej, så här låter den här rösten.",
            "da": "Hej, sådan lyder denne stemme.",
            "nb": "Hei, slik høres denne stemmen ut.",
            "fi": "Hei, tältä tämä ääni kuulostaa.",
            "is": "Halló, svona hljómar þessi rödd.",
            "et": "Tere, nii kõlab see hääl.",
            "lv": "Sveiki, lūk, kā skan šī balss.",
            "lt": "Sveiki, taip skamba šis balsas.",
            "cs": "Dobrý den, takhle zní tento hlas.",
            "sk": "Dobrý deň, takto znie tento hlas.",
            "hu": "Helló, így szól ez a hang.",
            "ro": "Bună ziua, așa sună această voce.",
            "hr": "Pozdrav, ovako zvuči ovaj glas.",
            "bs": "Zdravo, ovako zvuči ovaj glas.",
            "sl": "Pozdravljeni, tako zveni ta glas.",
            "sq": "Përshëndetje, kështu tingëllon ky zë.",
            "mt": "Bongu, dan huwa kampjun ta' din il-vuċi.",
            "ga": "Dia duit, seo sampla den ghuth seo.",
            "cy": "Helo, dyma sut mae'r llais hwn yn swnio.",
            "tr": "Merhaba, bu sesin bir önizlemesidir.",
            "el": "Γεια σας, έτσι ακούγεται αυτή η φωνή.",
            "bg": "Здравейте, така звучи този глас.",
            "mk": "Здраво, вака звучи овој глас.",
            "ru": "Здравствуйте, так звучит этот голос.",
            "sr": "Здраво, овако звучи овај глас.",
            "uk": "Привіт, ось як звучить цей голос.",
            "vi": "Xin chào, đây là bản nghe thử của giọng nói này.",
            "th": "สวัสดี นี่คือตัวอย่างเสียงนี้",
            "ar": "مرحبًا، هذه معاينة لهذا الصوت.",
            "hi": "नमस्ते, यह इस आवाज़ का नमूना है।",
        }.get(loc, "Hello, this is a preview of this voice.")
    tmp_dir = config.DATA_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"preview_{uuid.uuid4().hex[:8]}.mp3"
    try:
        tts.synth(text, voice, path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(502, str(e))
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


# ---- generation jobs ------------------------------------------------------

@app.post("/api/projects/{pid}/script")
def gen_script(pid: str, req: ScriptReq):
    _need(pid)

    choice = {k: v for k, v in (("slides_notes_mode", req.notes_mode), ("slides_missing", req.missing),
                                 ("slides_detail", req.detail)) if v}
    if choice:
        def _remember(p: Project):
            if p.source == "slides":
                p.settings = {**(p.settings or {}), **choice}
        storage.update(pid, _remember)

    def work(job: jobs.Job):
        before, proj = _snapshot(pid)
        res = script_gen.generate_script(
            proj, style=req.style, audience=req.audience, extra=req.extra,
            overwrite=req.overwrite, progress=job.progress,
        )
        storage.commit(pid, before, proj, step_fields=STEP_TEXT + AUDIO, project_fields=CARD)
        return res
    return _submit("script", work, pid, exclusive=jobs.HEAVY)


@app.post("/api/projects/{pid}/translate")
def gen_translate(pid: str, req: TranslateReq):
    _need(pid)

    def work(job: jobs.Job):
        before, proj = _snapshot(pid)
        if req.apply:     # slide projects are rewritten from the deck, recorded projects are translated
            res = script_gen.switch_language(proj, req.target_language, progress=job.progress, voice=req.voice)
        else:
            res = script_gen.translate_project(proj, req.target_language, apply=False, progress=job.progress)
        if req.apply and not proj.is_dialogue():        # in Q&A mode switch_language already changed the voices together with the speakers
            proj.voice = req.voice or tts.default_voice(req.target_language)
        storage.commit(pid, before, proj, step_fields=STEP_TEXT + AUDIO,
                       project_fields=CARD + ("language", "voice", "translations", "speakers"))
        return res
    return _submit("translate", work, pid, exclusive=jobs.HEAVY)


@app.post("/api/projects/{pid}/tts")
def gen_tts(pid: str, req: TTSReq):
    _need(pid)

    def work(job: jobs.Job):
        before, proj = _snapshot(pid)
        with _keep_done_on_stop(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers")):
            res = tts.synth_project(
                proj, voice=req.voice, rate=req.rate, volume=req.volume,
                only_missing=req.only_missing, progress=job.progress)
        storage.commit(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers"))
        return res
    return _submit("tts", work, pid, exclusive=jobs.HEAVY)


def _align_reveal(pid: str, job: jobs.Job, lo: float, hi: float) -> None:
    """Before rendering: for slides whose narration changed, let the AI mark which narration sentence starts each item (works across languages and paraphrases).
    Slides with unchanged narration reuse the previous result; without AI, or on AI errors, this is skipped and rendering matches the text instead."""
    from .services import slide_reveal
    _, proj = _snapshot(pid)
    steps = [s for s in proj.steps if s.include]
    if not (proj.settings or {}).get("slides_reveal") or not any(slide_reveal.needs_align(s) for s in steps):
        return
    try:
        res = slide_reveal.align_steps(steps, progress=lambda f, m: job.progress(lo + f * (hi - lo), m),
                                       label=lambda i, n: i18n.t("对齐解说和页面内容 {i}/{n}", i=i, n=n))
    except Exception:  # noqa: BLE001 — alignment only improves timing; on failure render anyway (Stop raises JobCancelled, which isn't swallowed here)
        return
    if not res:
        return

    def _do(p: Project):
        for st in p.steps:
            r = res.get(st.id)
            if r and st.reveal is not None and \
                    slide_reveal.align_key(st.narration, [it.text for it in st.reveal.items]) == r[1]:
                st.reveal.align, st.reveal.align_key = r
    storage.update(pid, _do)


@app.post("/api/projects/{pid}/render")
def gen_video(pid: str, req: RenderReq):
    _need(pid)
    overrides = {
        "burn_subtitles": req.burn_subtitles,
        "intro_enabled": req.intro_enabled,
        "outro_enabled": req.outro_enabled,
        "video_width": req.width,
        "video_height": req.height,
        "video_fps": req.fps,
    }

    def work(job: jobs.Job):
        _align_reveal(pid, job, 0.0, 0.03)
        before, proj = _snapshot(pid)
        res = video.render_project(proj, progress=lambda f, m: job.progress(0.03 + f * 0.97, m),
                                   overrides=overrides)
        storage.commit(pid, before, proj, step_fields=("duration",), project_fields=("output",))
        return res
    return _submit("render", work, pid, exclusive=jobs.HEAVY)


@app.post("/api/projects/{pid}/auto")
def auto_pipeline(pid: str, body: Dict[str, Any] = Body(default={})):
    """One click: write the script -> voice-over -> render the video."""
    _need(pid)
    style = body.get("style", "friendly")
    overwrite = bool(body.get("overwrite", False))

    def work(job: jobs.Job):
        out: Dict[str, Any] = {}
        # take a fresh snapshot for every stage: changes made in the editor between stages (e.g. unticking a step) are seen by the next stage
        job.progress(0.02, i18n.t("第 1/3 步：生成解说脚本"))
        before, proj = _snapshot(pid)
        out["script"] = script_gen.generate_script(
            proj, style=style, overwrite=overwrite,
            progress=lambda f, m: job.progress(0.02 + f * 0.23, m))
        storage.commit(pid, before, proj, step_fields=STEP_TEXT + AUDIO, project_fields=CARD)

        job.progress(0.26, i18n.t("第 2/3 步：合成语音"))
        before, proj = _snapshot(pid)
        with _keep_done_on_stop(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers")):
            out["tts"] = tts.synth_project(
                proj, only_missing=not overwrite,
                progress=lambda f, m: job.progress(0.26 + f * 0.24, m))
        storage.commit(pid, before, proj, step_fields=AUDIO, project_fields=("voice", "speakers"))

        job.progress(0.5, i18n.t("第 3/3 步：渲染视频"))
        _align_reveal(pid, job, 0.5, 0.52)
        before, proj = _snapshot(pid)
        out["video"] = video.render_project(
            proj, progress=lambda f, m: job.progress(0.52 + f * 0.48, m))
        storage.commit(pid, before, proj, step_fields=("duration",), project_fields=("output",))
        out["warning"] = out["video"].get("warning", "")
        return out
    return _submit("auto", work, pid, exclusive=jobs.HEAVY)


# ---- second-language subtitles (external) --------------------------------------

@app.get("/api/projects/{pid}/subtitles2")
def subtitles2_state(pid: str):
    """Main subtitle status of the current video and the generated second-language subtitles (outdated ones marked)."""
    return second_subs.state(_need(pid))


@app.post("/api/projects/{pid}/subtitles2")
def subtitles2_generate(pid: str, body: Dict[str, Any] = Body(default={})):
    """Generate one or more second-language subtitles for the current video (AI translation)."""
    proj = _need(pid)
    langs = [str(x) for x in body.get("languages") or [] if str(x) in script_gen.LANG_NAMES and str(x) != proj.language]
    if not langs:
        raise HTTPException(400, i18n.t("选一种和视频不同的语言"))
    if not second_subs.load_primary(proj):
        raise HTTPException(400, i18n.t("还没有渲染好的视频（或者视频没有字幕），先点「③ 渲染视频」。"))

    def work(job: jobs.Job):
        _, snap = _snapshot(pid)
        tracks = second_subs.generate(snap, langs, progress=job.progress)
        storage.update(pid, lambda p: second_subs.merge_tracks(p, tracks))
        return second_subs.state(_need(pid))
    return _submit("subtitles2", work, pid, exclusive=jobs.HEAVY)


@app.get("/api/projects/{pid}/subtitles2/{lang}")
def subtitles2_cues(pid: str, lang: str):
    """The timeline of one second-language subtitle (for the editor preview)."""
    return {"cues": second_subs.track_cues(_need(pid), lang)}


@app.delete("/api/projects/{pid}/subtitles2/{lang}")
def subtitles2_delete(pid: str, lang: str):
    _need(pid)
    storage.update(pid, lambda p: second_subs.delete_track(p, lang))
    return second_subs.state(_need(pid))


@app.get("/api/projects/{pid}/export/player")
def export_player(pid: str):
    """Web player package (zip): video + second-language subtitles + player page; drop it into a folder of your website to watch with selectable subtitles."""
    proj = _need(pid)
    try:
        path = second_subs.export_package(proj)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    name = Path(proj.output).stem + "_web.zip"
    return FileResponse(path, filename=name, media_type="application/zip", content_disposition_type="attachment",
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


# ---- job status ------------------------------------------------------------

@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, i18n.t("任务不存在"))
    return j


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    """Stop a job. Completed stages are kept (e.g. narration written, some steps voiced), so you can edit and generate again."""
    j = jobs.cancel(jid)
    if not j:
        raise HTTPException(404, i18n.t("任务不存在"))
    return j


@app.get("/api/jobs")
def list_jobs(project_id: str = Query("")):
    return {"jobs": jobs.list_jobs(project_id)}


# ---- files -------------------------------------------------------------------

_KIND_DIR = {
    "screenshots": storage.screenshots_dir,
    "audio": storage.audio_dir,
    "output": storage.output_dir,
    "media": clips.media_dir,          # videos of video steps (played in the editor preview)
}


@app.get("/api/projects/{pid}/file/{kind}/{name}")
def get_file(pid: str, kind: str, name: str, download: bool = False):
    if kind not in _KIND_DIR:
        raise HTTPException(404, i18n.t("未知文件类型"))
    if not storage.valid_pid(pid):
        raise HTTPException(404, i18n.t("文件不存在"))
    base = _KIND_DIR[kind](pid).resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, i18n.t("文件不存在"))
    # screenshots are named by step id and never change, so they can be cached long; voice-overs and videos are overwritten in place and must be revalidated
    headers = {"Cache-Control": "public, max-age=604800, immutable" if kind == "screenshots"
               else "no-cache"}
    if download:
        # File names are often Chinese (video titles). HTTP headers only allow latin-1, so writing them directly gives a 500;
        # FileResponse encodes them as filename*=utf-8''… (RFC 5987), which browsers decode back to the original name
        return FileResponse(path, headers=headers, filename=path.name, content_disposition_type="attachment")
    return FileResponse(path, headers=headers)


@app.get("/api/projects/{pid}/export/markdown", response_class=PlainTextResponse)
def export_markdown(pid: str):
    """Export a how-to document with images (can be pasted into any wiki or document tool)."""
    proj = _need(pid)
    port = config.get("server_port")
    lines = [f"# {proj.title or proj.name}", ""]
    if proj.subtitle:
        lines += [f"> {proj.subtitle}", ""]
    if proj.summary:
        lines += [proj.summary, ""]
    n = 0
    for s in proj.steps:
        if not s.include:
            continue
        n += 1
        head = s.title or i18n.t("步骤 {n}", _lang=i18n.content_lang(proj.language), n=n)
        lines.append(f"## {n}. {head}")
        text = s.caption if s.plays_clip_audio() else s.narration     # videos with their own sound: write what is said in the video
        if s.lines and proj.is_dialogue():
            names = {sp.role: sp.name for sp in dialogue.ensure_speakers(proj)}
            lines += [f"**{names.get(ln.who, ln.who)}**：{ln.text}  " for ln in s.lines if ln.text.strip()]
        elif text:
            lines.append(text)
        if s.screenshot:
            url = f"http://127.0.0.1:{port}/api/projects/{pid}/file/screenshots/{s.screenshot}"
            lines.append("")
            lines.append(f"![{head}]({url})")
        lines.append("")
    if proj.outro and video.effective_config(proj).get("outro_enabled", True):
        lines += ["---", "", proj.outro, ""]
    return "\n".join(lines)


@app.get("/api/projects/{pid}/export/script")
def export_script(pid: str):
    """Export an editable plain-text script."""
    proj = _need(pid)
    out = {
        "title": proj.title, "subtitle": proj.subtitle,
        "intro": proj.intro, "outro": proj.outro, "language": proj.language,
        "steps": [{"index": s.index, "title": s.title, "narration": s.narration,
                   "caption": s.caption, "include": s.include,
                   **({"lines": [ln.model_dump() for ln in s.lines]} if s.lines else {})}
                  for s in proj.steps],
    }
    return JSONResponse(out, headers={
        "Content-Disposition": f'attachment; filename="{pid}_script.json"'})


@app.post("/api/projects/{pid}/import/script")
def import_script(pid: str, body: Dict[str, Any] = Body(...)):
    """Import a script JSON edited elsewhere."""
    def _do(p: Project):
        for k in ("title", "subtitle", "intro", "outro"):
            if k in body:
                setattr(p, k, body[k])
        by_index = {int(x["index"]): x for x in body.get("steps", []) if "index" in x}
        for s in p.steps:
            item = by_index.get(s.index)
            if not item:
                continue
            if isinstance(item.get("lines"), list) and item["lines"]:      # Q&A lines
                item = {**item, "narration": join_lines([DialogueLine(**x) for x in dialogue.normalize_lines(item["lines"])])}
                s.lines = [DialogueLine(**x) for x in dialogue.normalize_lines(item["lines"])]
            if "narration" in item and (item["narration"] or "") != (s.narration or ""):
                s.narration = item["narration"] or ""
                drop_stale_lines(s)
                if s.voice_source != "own":          # for own-voice steps a text edit only fixes the subtitle; the recording is kept
                    s.audio = ""
                    s.audio_duration = 0.0
                    s.boundaries = []
            s.title = item.get("title", s.title)
            s.caption = item.get("caption", s.caption)
            s.include = item.get("include", s.include)
    storage.update(pid, _do)
    return _public(_need(pid))


# ---- static files -----------------------------------------------------------

if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup():
    config.ensure_dirs()

    def _cleanup():
        # imported PPT / PDF files used to be cleaned up only at the next import, so without another import they kept taking space (hundreds of MB)
        slides.cleanup_old_imports()
        storage.cleanup_temp()
    import threading
    threading.Thread(target=_cleanup, daemon=True, name="cleanup").start()
