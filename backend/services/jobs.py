"""极简后台任务队列：生成脚本 / 语音 / 视频都跑在工作线程里，前端轮询进度。"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from .. import i18n
from ..i18n import N_

_jobs: Dict[str, Dict[str, Any]] = {}
_objects: Dict[str, "Job"] = {}          # 运行中的任务对象（用来发停止信号）
_lock = threading.RLock()
MAX_KEEP = 60

# 这些任务会整体改写同一个项目，同一项目同时只允许跑一个
HEAVY = {"script", "translate", "tts", "render", "auto", "magic_mic", "subtitles2"}

KIND_NAME = {
    "script": N_("生成解说"), "translate": N_("翻译"), "tts": N_("合成语音"), "render": N_("渲染视频"),
    "auto": N_("一键生成"), "magic_mic": N_("识别讲解录音"), "voice": N_("处理录音"),
    "card_bg": N_("处理片头片尾背景"),
    "video": N_("处理视频"),
    "subtitles2": N_("第二语言字幕"),
}


class JobConflict(RuntimeError):
    def __init__(self, job: Dict[str, Any]):
        self.job = job
        kind = KIND_NAME.get(job["kind"])
        super().__init__(i18n.t("这个项目正在{task}，请等它完成后再试", task=i18n.t(kind) if kind else job["kind"]))


class JobCancelled(BaseException):
    """用户点了停止。

    继承 BaseException：各个服务里大量 `except Exception` 做重试 / 兜底，
    不能让它们把「停止」当成普通错误吞掉或转换掉。
    """


_local = threading.local()


def check_cancel() -> None:
    """在任务线程里的任意位置调用：用户点了停止就立即抛出 JobCancelled。"""
    job = getattr(_local, "job", None)
    if job is not None:
        job.check()


class Job:
    def __init__(self, kind: str, project_id: str = ""):
        self.id = "j_" + uuid.uuid4().hex[:10]
        self.data: Dict[str, Any] = {
            "id": self.id,
            "kind": kind,
            "project_id": project_id,
            "status": "pending",     # pending | running | done | error
            "progress": 0.0,
            "message": i18n.t("排队中…"),
            "log": [],
            "result": None,
            "error": "",
            "created_at": time.time(),
            "finished_at": 0.0,
            "cancel_requested": False,
        }
        self.cancel_event = threading.Event()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def progress(self, frac: float, message: str = "") -> None:
        # 各个服务都会频繁汇报进度（渲染每 12 帧、配音每一步、识别每一句），
        # 在这里检查停止标记，就不用在每个服务里单独埋检查点
        self.check()
        with _lock:
            self.data["progress"] = max(0.0, min(1.0, float(frac)))
            if message:
                self.data["message"] = message
                log: List[str] = self.data["log"]
                if not log or log[-1] != message:
                    log.append(message)
                    if len(log) > 200:
                        del log[:-200]


def _snapshot(j: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(j)
    out["log"] = list(j["log"])      # 别把正在被写的 list 交给 JSON 序列化
    return out


def _gc() -> None:
    if len(_jobs) <= MAX_KEEP:
        return
    done = sorted(
        (j for j in _jobs.values() if j["status"] in ("done", "error", "cancelled")),
        key=lambda j: j["finished_at"] or j["created_at"],
    )
    for j in done[: len(_jobs) - MAX_KEEP]:
        _jobs.pop(j["id"], None)


def submit(kind: str, fn: Callable[[Job], Any], project_id: str = "",
           exclusive: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """启动任务。fn 接收 Job 对象，返回值写进 result。

    exclusive：同一项目里与之冲突的任务种类。已有同种任务在跑 -> 直接返回那个任务（防连点）；
    有其他冲突任务在跑 -> 抛 JobConflict。
    """
    with _lock:
        if project_id and exclusive:
            group = set(exclusive)
            for j in _jobs.values():
                if (j["project_id"] == project_id and j["kind"] in group
                        and j["status"] in ("pending", "running")):
                    if j["kind"] == kind:
                        return _snapshot(j)
                    raise JobConflict(_snapshot(j))
        job = Job(kind, project_id)
        _jobs[job.id] = job.data
        _objects[job.id] = job
        _gc()

    def runner():
        _local.job = job
        with _lock:
            job.data["status"] = "running"
            job.data["message"] = i18n.t("开始…")
        try:
            job.check()                       # 排队期间就被停止了
            result = fn(job)
            with _lock:
                job.data["status"] = "done"
                job.data["result"] = result
                job.data["progress"] = 1.0
                job.data["finished_at"] = time.time()
        except JobCancelled:
            with _lock:
                job.data["status"] = "cancelled"
                job.data["message"] = i18n.t("已停止")
                job.data["finished_at"] = time.time()
                job.data["log"].append("cancelled")
        except Exception as e:
            with _lock:
                job.data["status"] = "error"
                job.data["error"] = str(e)
                job.data["message"] = i18n.t("失败：{error}", error=e)
                job.data["finished_at"] = time.time()
                job.data["log"].append(traceback.format_exc()[-1500:])
        finally:
            _local.job = None
            with _lock:
                _objects.pop(job.id, None)

    threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
    with _lock:
        return _snapshot(job.data)


def cancel(job_id: str) -> Optional[Dict[str, Any]]:
    """请求停止一个任务。任务会在下一个检查点（通常一秒以内）真正停下。"""
    with _lock:
        data = _jobs.get(job_id)
        if data is None:
            return None
        job = _objects.get(job_id)
        if job is not None and data["status"] in ("pending", "running"):
            job.cancel_event.set()
            data["cancel_requested"] = True
            data["message"] = i18n.t("正在停止…")
        return _snapshot(data)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        j = _jobs.get(job_id)
        return _snapshot(j) if j else None


def list_jobs(project_id: str = "") -> List[Dict[str, Any]]:
    with _lock:
        out = [_snapshot(j) for j in _jobs.values()
               if not project_id or j["project_id"] == project_id]
    out.sort(key=lambda j: j["created_at"], reverse=True)
    return out[:30]


def active(project_id: str = "") -> List[Dict[str, Any]]:
    return [j for j in list_jobs(project_id) if j["status"] in ("pending", "running")]
