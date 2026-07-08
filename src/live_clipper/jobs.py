"""后台任务：把长耗时操作（如 AI 审阅）放到守护线程执行，状态存 jobs.json。

Web 服务器是 ThreadingHTTPServer，每个请求独立线程；这里再起 daemon 线程执行实际
工作，请求立即返回 job_id，前端轮询任务状态。多线程读改写 jobs.json 通过模块级锁 +
原子替换保证一致性。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir, read_json

_LOCK = threading.Lock()
_MAX_JOBS = 50
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}


def _jobs_path(service_dir: Path) -> Path:
    return service_dir / "jobs.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_jobs(service_dir: Path) -> list[dict[str, Any]]:
    path = _jobs_path(service_dir)
    if not path.exists():
        return []
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [job for job in jobs if isinstance(job, dict)]


def _atomic_write_jobs(service_dir: Path, jobs: list[dict[str, Any]]) -> None:
    ensure_dir(service_dir)
    path = _jobs_path(service_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"jobs": jobs[-_MAX_JOBS:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def read_job(service_dir: Path, job_id: str) -> dict[str, Any] | None:
    for job in _load_jobs(service_dir):
        if job.get("id") == job_id:
            return job
    return None


def active_job_for(service_dir: Path, run_id: str, kind: str) -> dict[str, Any] | None:
    for job in reversed(_load_jobs(service_dir)):
        if job.get("run_id") == run_id and job.get("kind") == kind and job.get("status") == "running":
            return job
    return None


def _finalize(service_dir: Path, job_id: str, *, status: str, result: dict[str, Any] | None, error: str | None) -> None:
    with _LOCK:
        jobs = _load_jobs(service_dir)
        for job in jobs:
            if job.get("id") == job_id:
                job["status"] = status
                job["result"] = result
                job["error"] = error
                job["finished_at"] = _now()
                break
        _atomic_write_jobs(service_dir, jobs)


def _run(service_dir: Path, job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
    try:
        result = fn()
        ok = isinstance(result, dict) and bool(result.get("ok"))
        status = "succeeded" if ok else "failed"
        if ok:
            error = None
        elif isinstance(result, dict):
            error = result.get("message") or result.get("error") or "任务失败"
        else:
            error = "任务失败"
    except Exception as exc:  # noqa: BLE001 - 后台任务必须记录任何失败。
        result = None
        status = "failed"
        error = str(exc)
    _finalize(
        service_dir,
        job_id,
        status=status,
        result=result if isinstance(result, dict) else None,
        error=error,
    )


def start_job(service_dir: Path, *, kind: str, run_id: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """启动一个后台任务。若同一 (run_id, kind) 已有 running 任务，返回它，不重复启动。"""
    with _LOCK:
        existing = active_job_for(service_dir, run_id, kind)
        if existing is not None:
            return existing
        job = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "run_id": run_id,
            "status": "running",
            "created_at": _now(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        jobs = _load_jobs(service_dir)
        jobs.append(job)
        _atomic_write_jobs(service_dir, jobs)
    thread = threading.Thread(target=_run, args=(service_dir, job["id"], fn), daemon=True)
    thread.start()
    return job


def sweep_interrupted(service_dir: Path) -> None:
    """把上次进程遗留的 running 任务标记为 interrupted（Web 启动时调用）。"""
    with _LOCK:
        jobs = _load_jobs(service_dir)
        changed = False
        for job in jobs:
            if job.get("status") == "running":
                job["status"] = "interrupted"
                job["error"] = "任务在服务重启时被中断。"
                job["finished_at"] = _now()
                changed = True
        if changed:
            _atomic_write_jobs(service_dir, jobs)
