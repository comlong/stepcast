"""项目文件存储：projects/<id>/{project.json, screenshots/, audio/, output/}"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import config, i18n
from .models import Project, Step

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

PID_RE = re.compile(r"^p_[0-9a-f]{12}$")

# 这组字段描述「这一步的配音」，必须整体一起写，否则会出现文字和音频对不上
AUDIO_FIELDS = ("audio", "audio_duration", "boundaries", "voice_source", "line_times")


def valid_pid(pid: str) -> bool:
    return bool(PID_RE.match(pid or ""))


def _lock_for(pid: str) -> threading.RLock:
    with _locks_guard:
        if pid not in _locks:
            _locks[pid] = threading.RLock()
        return _locks[pid]


def project_dir(pid: str) -> Path:
    return config.DATA_DIR / pid


def screenshots_dir(pid: str) -> Path:
    return project_dir(pid) / "screenshots"


def audio_dir(pid: str) -> Path:
    return project_dir(pid) / "audio"


def output_dir(pid: str) -> Path:
    return project_dir(pid) / "output"


def work_dir(pid: str) -> Path:
    return project_dir(pid) / "work"


def _json_path(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def create(name: str = "", language: str = "") -> Project:
    config.ensure_dirs()
    from .services import tts
    cfg = config.load()
    lang = language or cfg["language"]
    voice = cfg["voice"]
    # 指定了别的解说语言（比如「边录边讲」按麦克风的语言建项目）：配置里的音色不是这种语言就换成它的默认音色，
    # 不然德语解说会被英语音色念出来
    if not voice.lower().startswith(lang.split("-")[0].lower() + "-"):
        voice = tts.default_voice(lang)
    proj = Project(
        name=name or i18n.t("教程 {time}", time=time.strftime("%Y-%m-%d %H:%M")),
        language=lang,
        voice=voice,
    )
    for d in (project_dir(proj.id), screenshots_dir(proj.id), audio_dir(proj.id),
              output_dir(proj.id), work_dir(proj.id)):
        d.mkdir(parents=True, exist_ok=True)
    save(proj)
    return proj


def cleanup_temp(max_age_hours: float = 24) -> int:
    """启动时清理临时文件：渲染到一半被关掉留下的 work/render_*（进程刚启动，不可能有渲染在跑），
    以及 _tmp 里超过一天的上传。返回删掉了几个。"""
    import shutil
    n = 0
    now = time.time()
    if not config.DATA_DIR.is_dir():
        return 0
    for d in config.DATA_DIR.iterdir():
        try:
            if d.name == "_tmp" and d.is_dir():
                for f in d.iterdir():
                    if now - f.stat().st_mtime > max_age_hours * 3600:
                        if f.is_dir():
                            shutil.rmtree(f, ignore_errors=True)
                        else:
                            f.unlink(missing_ok=True)
                        n += 1
            elif valid_pid(d.name) and (d / "work").is_dir():
                for r in (d / "work").glob("render_*"):
                    if r.is_dir():
                        shutil.rmtree(r, ignore_errors=True)
                        n += 1
        except Exception:
            pass
    return n


def exists(pid: str) -> bool:
    return valid_pid(pid) and _json_path(pid).exists()


def load(pid: str) -> Optional[Project]:
    if not valid_pid(pid):
        return None
    p = _json_path(pid)
    # Windows 上另一个线程正在 replace 时，读会短暂遇到 PermissionError，稍等重试
    for attempt in range(40):
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return Project(**data)
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.01 + attempt * 0.005)
        except Exception:
            return None
    return None


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(60):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.01 + attempt * 0.005)


def save(proj: Project) -> Project:
    with _lock_for(proj.id):
        proj.updated_at = time.time()
        d = project_dir(proj.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"project.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(proj.model_dump_json(indent=1), encoding="utf-8")
        try:
            _replace_with_retry(tmp, _json_path(proj.id))
        finally:
            tmp.unlink(missing_ok=True)
    return proj


def update(pid: str, fn: Callable[[Project], object]):
    """加锁读改写。fn 的返回值不为 None 时原样返回，否则返回项目本身。"""
    with _lock_for(pid):
        proj = load(pid)
        if proj is None:
            raise KeyError(pid)
        result = fn(proj)
        save(proj)
        return result if result is not None else proj


def commit(pid: str, before: Project, after: Project,
           step_fields: Iterable[str] = (), project_fields: Iterable[str] = (),
           force_audio: bool = False) -> Project:
    """把后台任务的结果合并回项目（三方合并）。

    before = 任务开始时的快照，after = 任务算完的结果。
    只写任务真正改过的字段，而且只在用户这段时间没动过该字段时才写，
    所以任务运行期间在编辑器里做的修改不会被覆盖；任务期间新录的 / 被删的步骤也不受影响。

    配音字段是一组：只有当前的解说词正好是任务配音时用的那段文字，才把音频写回去。
    force_audio=True 用于「用户明确上传了录音」，这时音频无条件写回。
    """
    step_fields = tuple(step_fields)
    project_fields = tuple(project_fields)
    text_fields = [f for f in step_fields if f not in AUDIO_FIELDS]
    has_audio = any(f in AUDIO_FIELDS for f in step_fields)

    def _do(live: Project):
        for f in project_fields:
            bv, av = getattr(before, f), getattr(after, f)
            if bv != av and getattr(live, f) == bv:
                setattr(live, f, av)
        bmap = {s.id: s for s in before.steps}
        amap = {s.id: s for s in after.steps}
        for ls in live.steps:
            b, a = bmap.get(ls.id), amap.get(ls.id)
            if b is None or a is None:
                continue
            for f in text_fields:
                bv, av = getattr(b, f), getattr(a, f)
                if bv != av and getattr(ls, f) == bv:
                    setattr(ls, f, av)
            if not has_audio:
                continue
            if all(getattr(b, f) == getattr(a, f) for f in AUDIO_FIELDS):
                continue
            untouched = all(getattr(ls, f) == getattr(b, f) for f in AUDIO_FIELDS)
            matches_text = (ls.narration or "") == (a.narration or "")
            if force_audio or (untouched and (matches_text or not a.audio)):
                for f in AUDIO_FIELDS:
                    setattr(ls, f, getattr(a, f))
        return live

    return update(pid, _do)


def list_projects() -> List[dict]:
    config.ensure_dirs()
    out = []
    for d in config.DATA_DIR.iterdir():
        if not d.is_dir() or not valid_pid(d.name):
            continue
        proj = load(d.name)
        if proj is None:
            continue
        out.append({
            "id": proj.id,
            "name": proj.name,
            "created_at": proj.created_at,
            "updated_at": proj.updated_at,
            "steps": len(proj.steps),
            "language": proj.language,
            "recording": proj.recording,
            "output": proj.output,
            "source": proj.source,
            "thumbnail": proj.steps[0].screenshot if proj.steps else "",
        })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


def delete(pid: str) -> bool:
    if not valid_pid(pid):
        return False
    d = project_dir(pid)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def save_screenshot(pid: str, step_id: str, b64: str) -> tuple[str, int, int]:
    """保存 base64 截图，返回 (文件名, 宽, 高)。"""
    from PIL import Image
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    screenshots_dir(pid).mkdir(parents=True, exist_ok=True)
    fname = f"{step_id}.png"
    path = screenshots_dir(pid) / fname
    path.write_bytes(raw)
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        w = h = 0
    return fname, w, h


def add_step(pid: str, step: Step) -> int:
    """追加一步，返回追加后的步骤总数。"""
    def _do(proj: Project):
        step.index = len(proj.steps)
        proj.steps.append(step)
        return len(proj.steps)
    return update(pid, _do)


def reindex(proj: Project) -> None:
    for i, s in enumerate(proj.steps):
        s.index = i
