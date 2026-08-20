from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .project_domain import normalize_utc
from .project_storage import ProjectRepository, database_path


def _aware(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def next_project_scan_at(config: dict[str, Any], *, now: datetime | None = None) -> str | None:
    schedule = config["schedule"]
    if not schedule["enabled"]:
        return None
    timezone = ZoneInfo(str(schedule["timezone"]))
    current = _aware(now or datetime.now(UTC), timezone)
    if schedule["mode"] == "interval":
        candidate = current + timedelta(minutes=int(schedule["interval_minutes"]))
    else:
        hour, minute = (int(part) for part in str(schedule["daily_time"]).split(":"))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
    return normalize_utc(candidate)


def tick_due_projects(
    repository: ProjectRepository,
    *,
    now: datetime | None = None,
    scan_fn: Callable[..., Any],
) -> list[str]:
    current = now or datetime.now(UTC)
    current_utc = current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)
    due_projects: list[str] = []
    for project in repository.list_projects():
        if project.activation_state != "active":
            continue
        runtime = repository.get_runtime(project.project_id)
        if runtime is None or runtime.next_scan_at is None:
            continue
        due_at = datetime.fromisoformat(runtime.next_scan_at.replace("Z", "+00:00"))
        if due_at > current_utc:
            continue
        revision = repository.get_config_revision(project.project_id)
        assert revision is not None
        try:
            scan_fn(
                project.project_id,
                trigger_source="scheduled",
                recovery_scan=True,
                scheduled_at=runtime.next_scan_at,
            )
        except Exception as exc:  # noqa: BLE001 - one project schedule must not block another.
            repository.update_runtime(
                project.project_id,
                readiness_state="blocked",
                failure_code="scheduled_scan_failed",
                failure_summary=str(exc),
                schedule_cursor=runtime.next_scan_at,
                next_scan_at=next_project_scan_at(revision.config, now=current_utc),
            )
            repository.append_workspace_event(
                "scan_failed",
                project_id=project.project_id,
                payload={"error_code": "scheduled_scan_failed"},
            )
        else:
            repository.update_runtime(
                project.project_id,
                readiness_state="ready",
                failure_code=None,
                failure_summary=None,
                schedule_cursor=runtime.next_scan_at,
                next_scan_at=next_project_scan_at(revision.config, now=current_utc),
            )
        due_projects.append(project.project_id)
    return due_projects


def tick_project_schedules(
    settings: Settings,
    *,
    service_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not database_path(service_dir).exists():
        return {"ok": True, "mode": "legacy", "scanned_projects": []}
    with ProjectRepository(service_dir) as repository:
        if repository.get_data_mode() != "projects":
            return {"ok": True, "mode": "legacy", "scanned_projects": []}
        from .project_scan import scan_project

        scanned = tick_due_projects(
            repository,
            now=now,
            scan_fn=lambda project_id, **kwargs: scan_project(
                repository,
                project_id,
                settings=settings,
                service_dir=service_dir,
                **kwargs,
            ),
        )
        return {"ok": True, "mode": "projects", "scanned_projects": scanned}
