from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import SchedulerJobConfig, Settings
from .utils import ensure_dir, read_json, write_json

JOB_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
JOB_TYPES = {"scan_recordings", "review_due_check", "maintenance_check", "ai_review"}
SCHEDULES = {"weekly", "daily", "interval_minutes"}
DAYS_OF_WEEK = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _scheduler_path(service_dir: Path) -> Path:
    return service_dir / "scheduler.json"


def _scheduler_runs_path(service_dir: Path) -> Path:
    return service_dir / "scheduler_runs.json"


def _scheduler_events_path(service_dir: Path) -> Path:
    return service_dir / "scheduler_events.jsonl"


def load_scheduler_runs(service_dir: Path) -> dict[str, Any]:
    path = _scheduler_runs_path(service_dir)
    if not path.exists():
        return {"jobs": {}}
    data = read_json(path)
    jobs = data.get("jobs", {})
    return {"jobs": jobs if isinstance(jobs, dict) else {}}


def save_scheduler_runs(service_dir: Path, state: dict[str, Any]) -> None:
    write_json(_scheduler_runs_path(ensure_dir(service_dir)), state)


def append_scheduler_event(service_dir: Path, event_type: str, **payload: Any) -> None:
    ensure_dir(service_dir)
    event = {"type": event_type, "created_at": _now_utc().isoformat(), **payload}
    with _scheduler_events_path(service_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        from . import service

        service.append_event(service_dir, event_type, **payload)
    except Exception:
        pass


def read_scheduler_events(service_dir: Path, *, max_events: int = 50) -> list[dict[str, Any]]:
    path = _scheduler_events_path(service_dir)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events[-max_events:]


def validate_scheduler_job(job: dict[str, Any] | SchedulerJobConfig) -> dict[str, Any]:
    data = _job_dict(job)
    errors: list[dict[str, str]] = []
    job_id = str(data.get("id") or "")
    if not JOB_ID_RE.match(job_id):
        errors.append(_error("id", "任务 id 只能使用小写字母、数字、下划线或短横线，最长 64 个字符。"))
    name = str(data.get("name") or "")
    if not (1 <= len(name) <= 40):
        errors.append(_error("name", "任务名称必须填写，且不超过 40 个字符。"))
    job_type = str(data.get("type") or "")
    if job_type not in JOB_TYPES:
        errors.append(_error("type", "任务类型只能是 scan_recordings、review_due_check、maintenance_check 或 ai_review。"))
    schedule = str(data.get("schedule") or "")
    if schedule not in SCHEDULES:
        errors.append(_error("schedule", "频率只能是 weekly、daily 或 interval_minutes。"))
    if schedule == "weekly" and data.get("day_of_week") not in DAYS_OF_WEEK:
        errors.append(_error("day_of_week", "星期必须是 mon 到 sun。"))
    if schedule in {"weekly", "daily"} and not _valid_hhmm(str(data.get("time") or "")):
        errors.append(_error("time", "时间必须使用 HH:MM 格式。"))
    if schedule == "interval_minutes":
        try:
            interval = int(data.get("interval_minutes"))
        except (TypeError, ValueError):
            interval = 0
        if interval < 5 or interval > 1440:
            errors.append(_error("interval_minutes", "间隔分钟数必须在 5 到 1440 之间。"))
    return {"ok": not errors, "errors": errors}


def scheduler_job_from_dict(data: dict[str, Any]) -> SchedulerJobConfig:
    return SchedulerJobConfig(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        enabled=bool(data.get("enabled", True)),
        type=str(data.get("type", "")),
        schedule=str(data.get("schedule", "")),
        day_of_week=str(data["day_of_week"]) if data.get("day_of_week") is not None else None,
        time=str(data["time"]) if data.get("time") is not None else None,
        interval_minutes=int(data["interval_minutes"]) if data.get("interval_minutes") not in (None, "") else None,
        skip_if_running=bool(data.get("skip_if_running", True)),
    )


def next_run_at(
    job: SchedulerJobConfig,
    *,
    now: datetime,
    timezone: str,
    last_run_at: datetime | None = None,
) -> datetime:
    tz = ZoneInfo(timezone)
    current = _as_timezone(now, tz)
    if job.schedule == "interval_minutes":
        base = _as_timezone(last_run_at, tz) if last_run_at else current
        return base + timedelta(minutes=int(job.interval_minutes or 5))
    hour, minute = _parse_time(str(job.time))
    if job.schedule == "daily":
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate
    target_weekday = DAYS_OF_WEEK[str(job.day_of_week)]
    days_ahead = (target_weekday - current.weekday()) % 7
    candidate = (current + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=7)
    return candidate


def previous_run_at(job: SchedulerJobConfig, *, now: datetime, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    current = _as_timezone(now, tz)
    if job.schedule == "interval_minutes":
        return current
    hour, minute = _parse_time(str(job.time))
    if job.schedule == "daily":
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > current:
            candidate -= timedelta(days=1)
        return candidate
    target_weekday = DAYS_OF_WEEK[str(job.day_of_week)]
    days_back = (current.weekday() - target_weekday) % 7
    candidate = (current - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > current:
        candidate -= timedelta(days=7)
    return candidate


def get_scheduler_status(
    settings: Settings,
    *,
    service_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(ZoneInfo(settings.scheduler.timezone))
    runs_state = load_scheduler_runs(service_dir)
    jobs = [_job_status(job, settings=settings, service_dir=service_dir, runs_state=runs_state, now=current) for job in settings.scheduler.jobs]
    enabled_jobs = [job for job in jobs if job.get("enabled") and not job.get("paused")]
    next_job = min(enabled_jobs, key=lambda item: item["next_run_at"] or "9999") if enabled_jobs else None
    scheduler_state = {
        "enabled": settings.scheduler.enabled,
        "timezone": settings.scheduler.timezone,
        "last_tick_at": _read_scheduler_json(service_dir).get("last_tick_at"),
        "next_due_job_id": next_job["id"] if next_job else None,
        "next_due_at": next_job["next_run_at"] if next_job else None,
        "last_error": _read_scheduler_json(service_dir).get("last_error"),
        "current_time": _as_timezone(current, ZoneInfo(settings.scheduler.timezone)).isoformat(),
    }
    return {
        "ok": True,
        "scheduler": scheduler_state,
        "jobs": jobs,
        "next_due_job_id": scheduler_state["next_due_job_id"],
        "next_due_at": scheduler_state["next_due_at"],
        "events": read_scheduler_events(service_dir),
    }


def tick_scheduler(
    settings: Settings,
    *,
    service_dir: Path,
    now: datetime | None = None,
    skip_job_types: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    current = now or datetime.now(ZoneInfo(settings.scheduler.timezone))
    ensure_dir(service_dir)
    if not settings.scheduler.enabled:
        _write_scheduler_summary(settings, service_dir, current)
        return {"ok": True, "ran_jobs": [], "skipped_jobs": [], "enabled": False}

    append_scheduler_event(service_dir, "scheduler_tick")
    runs_state = load_scheduler_runs(service_dir)
    ran_jobs = []
    skipped_jobs = []
    for job in settings.scheduler.jobs:
        state = _job_state(runs_state, job.id)
        if not job.enabled or state.get("paused"):
            continue
        due_at = _ensure_next_run_at(job, settings=settings, state=state, now=current)
        save_scheduler_runs(service_dir, runs_state)
        if due_at > current:
            continue
        if job.type in skip_job_types:
            state.update(
                {
                    "status": "skipped",
                    "last_error": "delegated_to_project_scheduler",
                    "last_scheduled_for": due_at.isoformat(),
                    "next_run_at": _advance_next_run(job, due_at=due_at, now=current).isoformat(),
                }
            )
            save_scheduler_runs(service_dir, runs_state)
            append_scheduler_event(
                service_dir,
                "scheduler_job_skipped",
                job_id=job.id,
                reason="delegated_to_project_scheduler",
            )
            skipped_jobs.append(job.id)
            continue
        if settings.scheduler.missed_policy == "skip":
            state.update(
                {
                    "status": "skipped",
                    "last_error": "missed_policy_skip",
                    "last_scheduled_for": due_at.isoformat(),
                    "next_run_at": _advance_next_run(job, due_at=due_at, now=current).isoformat(),
                }
            )
            save_scheduler_runs(service_dir, runs_state)
            append_scheduler_event(service_dir, "scheduler_job_skipped", job_id=job.id, reason="missed_policy_skip")
            skipped_jobs.append(job.id)
            continue
        append_scheduler_event(
            service_dir,
            "scheduler_job_missed_run_once",
            job_id=job.id,
            scheduled_for=due_at.isoformat(),
        )
        if job.skip_if_running and state.get("status") == "running":
            _mark_job_skipped(service_dir, runs_state, job, "job_already_running")
            state["next_run_at"] = _advance_next_run(job, due_at=due_at, now=current).isoformat()
            save_scheduler_runs(service_dir, runs_state)
            skipped_jobs.append(job.id)
            continue
        run_job_now(job, settings, service_dir=service_dir, now=current, scheduled_due_at=due_at)
        runs_state = load_scheduler_runs(service_dir)
        ran_jobs.append(job.id)
    _write_scheduler_summary(settings, service_dir, current)
    return {"ok": True, "ran_jobs": ran_jobs, "skipped_jobs": skipped_jobs, "enabled": True}


def run_job_now(
    job: SchedulerJobConfig,
    settings: Settings,
    *,
    service_dir: Path,
    now: datetime | None = None,
    scheduled_due_at: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(ZoneInfo(settings.scheduler.timezone))
    runs_state = load_scheduler_runs(service_dir)
    entry = _job_state(runs_state, job.id)
    previous_next_run_at = entry.get("next_run_at")
    if scheduled_due_at is None and previous_next_run_at is None:
        previous_next_run_at = _ensure_next_run_at(job, settings=settings, state=entry, now=current).isoformat()
    due_to_advance = scheduled_due_at
    if due_to_advance is None:
        persisted_due = _parse_datetime(previous_next_run_at)
        if persisted_due is not None and _as_timezone(persisted_due, ZoneInfo(settings.scheduler.timezone)) <= current:
            due_to_advance = _as_timezone(persisted_due, ZoneInfo(settings.scheduler.timezone))
    entry.update({"status": "running", "last_started_at": current.isoformat()})
    if scheduled_due_at is not None:
        entry["last_scheduled_for"] = scheduled_due_at.isoformat()
    save_scheduler_runs(service_dir, runs_state)
    append_scheduler_event(service_dir, "scheduler_job_started", job_id=job.id, job_type=job.type)
    try:
        result = _run_job_action(job, settings, service_dir=service_dir, now=current)
    except Exception as exc:  # noqa: BLE001 - record long-running scheduler failure as state.
        entry.update({"status": "failed", "last_run_at": current.isoformat(), "last_error": str(exc)})
        if due_to_advance is not None:
            entry["next_run_at"] = _advance_next_run(job, due_at=due_to_advance, now=current).isoformat()
        elif previous_next_run_at is not None:
            entry["next_run_at"] = previous_next_run_at
        save_scheduler_runs(service_dir, runs_state)
        append_scheduler_event(service_dir, "scheduler_job_failed", job_id=job.id, error=str(exc))
        _write_scheduler_summary(settings, service_dir, current)
        return {"ok": False, "job_id": job.id, "error": str(exc)}
    entry.update({
        "status": "success",
        "last_run_at": current.isoformat(),
        "last_successful_run_at": current.isoformat(),
        "last_result": result,
        "last_error": None,
    })
    if due_to_advance is not None:
        entry["next_run_at"] = _advance_next_run(job, due_at=due_to_advance, now=current).isoformat()
    elif previous_next_run_at is not None:
        entry["next_run_at"] = previous_next_run_at
    save_scheduler_runs(service_dir, runs_state)
    append_scheduler_event(service_dir, "scheduler_job_completed", job_id=job.id, result=result)
    _write_scheduler_summary(settings, service_dir, current)
    return {"ok": True, "job_id": job.id, "result": result}


def pause_job(job_id: str, *, service_dir: Path) -> dict[str, Any]:
    runs_state = load_scheduler_runs(service_dir)
    entry = _job_state(runs_state, job_id)
    entry["paused"] = True
    save_scheduler_runs(service_dir, runs_state)
    append_scheduler_event(service_dir, "scheduler_job_paused", job_id=job_id)
    return {"ok": True, "job_id": job_id, "paused": True}


def resume_job(job_id: str, *, service_dir: Path) -> dict[str, Any]:
    runs_state = load_scheduler_runs(service_dir)
    entry = _job_state(runs_state, job_id)
    entry["paused"] = False
    save_scheduler_runs(service_dir, runs_state)
    append_scheduler_event(service_dir, "scheduler_job_resumed", job_id=job_id)
    return {"ok": True, "job_id": job_id, "paused": False}


def _run_job_action(job: SchedulerJobConfig, settings: Settings, *, service_dir: Path, now: datetime) -> dict[str, Any]:
    if job.type == "scan_recordings":
        from . import service

        return service.run_service_once(settings, service_dir=service_dir)
    if job.type == "review_due_check":
        return _review_due_check(service_dir, now=now)
    if job.type == "maintenance_check":
        return _maintenance_check(settings, service_dir)
    if job.type == "ai_review":
        from . import review_automation

        return review_automation.run_due_ai_reviews(settings, service_dir=service_dir)
    raise ValueError(f"Unsupported scheduler job type: {job.type}")


def _review_due_check(service_dir: Path, *, now: datetime) -> dict[str, Any]:
    from . import service

    runs = service.load_runs(service_dir)
    due_runs = []
    for run in runs:
        if run.get("phase") != "needs_review":
            continue
        run_dir = Path(str(run.get("run_dir")))
        if (run_dir / "selected_clips.json").exists():
            continue
        run["review_due_at"] = now.isoformat()
        run["updated_at"] = now.isoformat()
        due_runs.append(str(run.get("run_id")))
        _write_review_task(run_dir, str(run.get("run_id")))
        service.append_event(service_dir, "review_due", run_id=run.get("run_id"), run_dir=str(run_dir))
    service.save_runs(runs, service_dir)
    message = f"发现 {len(due_runs)} 个待审阅任务" if due_runs else "当前没有待审阅任务"
    return {"ok": True, "due_runs": due_runs, "message": message}


def _maintenance_check(settings: Settings, service_dir: Path) -> dict[str, Any]:
    from . import service

    runs = service.load_runs(service_dir)
    changed = 0
    for run in runs:
        if service.reconcile_run(run, settings, service_dir=service_dir):
            changed += 1
    service.save_runs(runs, service_dir)
    counts: dict[str, int] = {}
    for run in runs:
        phase = str(run.get("phase", "unknown"))
        counts[phase] = counts.get(phase, 0) + 1
    return {"ok": True, "changed_runs": changed, "phase_counts": counts}


def _job_status(
    job: SchedulerJobConfig,
    *,
    settings: Settings,
    service_dir: Path,
    runs_state: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    state = _job_state(runs_state, job.id)
    next_run_at_value = _ensure_next_run_at(job, settings=settings, state=state, now=now)
    paused = bool(state.get("paused"))
    status = state.get("status")
    if paused:
        status = "paused"
    elif status != "running" and next_run_at_value <= now:
        status = "overdue"
    return {
        "id": job.id,
        "name": job.name,
        "type": job.type,
        "schedule": job.schedule,
        "day_of_week": job.day_of_week,
        "time": job.time,
        "interval_minutes": job.interval_minutes,
        "enabled": job.enabled,
        "skip_if_running": job.skip_if_running,
        "paused": paused,
        "status": status,
        "last_run_at": state.get("last_run_at"),
        "next_run_at": next_run_at_value.isoformat(),
        "last_result": state.get("last_result"),
        "last_error": state.get("last_error"),
    }


def _job_due(job: SchedulerJobConfig, *, settings: Settings, runs_state: dict[str, Any], now: datetime) -> bool:
    state = _job_state(runs_state, job.id)
    return _ensure_next_run_at(job, settings=settings, state=state, now=now) <= now


def _ensure_next_run_at(
    job: SchedulerJobConfig,
    *,
    settings: Settings,
    state: dict[str, Any],
    now: datetime,
) -> datetime:
    persisted = _parse_datetime(state.get("next_run_at"))
    if persisted is not None:
        return _as_timezone(persisted, ZoneInfo(settings.scheduler.timezone))
    last_success = _parse_datetime(state.get("last_successful_run_at") or state.get("last_run_at"))
    if job.schedule == "interval_minutes":
        base = _as_timezone(last_success, ZoneInfo(settings.scheduler.timezone)) if last_success else now
        initialized = base + timedelta(minutes=int(job.interval_minutes or 5))
    elif last_success is not None:
        initialized = next_run_at(
            job,
            now=last_success,
            timezone=settings.scheduler.timezone,
            last_run_at=last_success,
        )
    else:
        initialized = next_run_at(job, now=now, timezone=settings.scheduler.timezone)
    state["next_run_at"] = initialized.isoformat()
    return initialized


def _advance_next_run(job: SchedulerJobConfig, *, due_at: datetime, now: datetime) -> datetime:
    if job.schedule == "interval_minutes":
        return now + timedelta(minutes=int(job.interval_minutes or 5))
    step = timedelta(days=1 if job.schedule == "daily" else 7)
    candidate = due_at
    while candidate <= now:
        candidate += step
    return candidate


def _mark_job_skipped(service_dir: Path, runs_state: dict[str, Any], job: SchedulerJobConfig, reason: str) -> None:
    entry = _job_state(runs_state, job.id)
    entry.update({"status": "skipped", "last_error": reason})
    save_scheduler_runs(service_dir, runs_state)
    append_scheduler_event(service_dir, "scheduler_job_skipped", job_id=job.id, reason=reason)


def _write_scheduler_summary(settings: Settings, service_dir: Path, now: datetime) -> None:
    status = get_scheduler_status(settings, service_dir=service_dir, now=now)
    write_json(_scheduler_path(ensure_dir(service_dir)), status["scheduler"])


def _read_scheduler_json(service_dir: Path) -> dict[str, Any]:
    path = _scheduler_path(service_dir)
    return read_json(path) if path.exists() else {}


def _job_state(runs_state: dict[str, Any], job_id: str) -> dict[str, Any]:
    runs_state.setdefault("jobs", {})
    jobs = runs_state["jobs"]
    jobs.setdefault(job_id, {})
    return jobs[job_id]


def _write_review_task(run_dir: Path, run_id: str) -> None:
    ensure_dir(run_dir)
    task_path = run_dir / "codex_task.md"
    if task_path.exists():
        return
    task_path.write_text(
        "\n".join([
            f"# 待审阅任务 {run_id}",
            "",
            "Scheduler 已标记该 run 需要审阅。",
            "V5 只负责提醒和标记，不会自动生成 selected_clips.json。",
        ]),
        encoding="utf-8",
    )


def _job_dict(job: dict[str, Any] | SchedulerJobConfig) -> dict[str, Any]:
    if isinstance(job, dict):
        return job
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "type": job.type,
        "schedule": job.schedule,
        "day_of_week": job.day_of_week,
        "time": job.time,
        "interval_minutes": job.interval_minutes,
        "skip_if_running": job.skip_if_running,
    }


def _parse_time(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


def _valid_hhmm(value: str) -> bool:
    hour, sep, minute = value.partition(":")
    if sep != ":" or len(hour) != 2 or len(minute) != 2 or not hour.isdigit() or not minute.isdigit():
        return False
    return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _as_timezone(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _now_utc() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def _error(field: str, message: str) -> dict[str, str]:
    return {"field": field, "message": message}
