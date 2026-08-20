from __future__ import annotations

import fnmatch
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import service
from .automation import SUPPORTED_VIDEO_EXTENSIONS
from .config import Settings
from .project_domain import normalize_utc
from .project_resources import ResourceUnavailableError, resolve_parameter_snapshot
from .project_storage import ProjectRepository


class ProjectScanError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class ScanReport:
    scan_id: str
    project_id: str
    status: str
    reused: bool
    matched_count: int
    created_count: int
    duplicate_count: int
    unstable_count: int
    unsupported_count: int
    excluded_count: int
    failed_count: int
    created_run_ids: tuple[str, ...] = ()
    failures: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    bytes: int
    modified_at: str
    selectable: bool
    reason: str | None = None


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)


def _contained(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def _selected_paths(root: Path, relative_paths: Iterable[str]) -> list[Path]:
    selected = []
    for value in relative_paths:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProjectScanError("source_path_outside_project", "选择的文件不在项目录像目录内", status=422)
        candidate = root / relative
        if not _contained(candidate, root):
            raise ProjectScanError("source_path_outside_project", "选择的文件不在项目录像目录内", status=422)
        selected.append(candidate)
    return selected


def _matches_patterns(relative: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(relative, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatch(relative, pattern) for pattern in exclude)


def _candidate_paths(
    root: Path,
    *,
    scope: str,
    selected_relative_paths: Iterable[str],
) -> list[Path]:
    if scope == "selected":
        return _selected_paths(root, selected_relative_paths)
    if scope != "new":
        raise ProjectScanError("validation_failed", "扫描范围只能是 new 或 selected", status=422)
    return [path for path in root.rglob("*") if path.is_file() and _contained(path, root)]


def scan_project(
    repository: ProjectRepository,
    project_id: str,
    *,
    scope: str = "new",
    selected_relative_paths: Iterable[str] = (),
    trigger_source: str = "manual",
    recovery_scan: bool = False,
    scheduled_at: str | None = None,
    settings: Settings,
    service_dir: Path,
    now: datetime | None = None,
    identity_fn: Callable[..., dict[str, Any]] = service.content_identity,
) -> ScanReport:
    project = repository.get_project(project_id)
    if project is None:
        raise ProjectScanError("project_not_found", "项目不存在", status=404)
    if project.activation_state == "inactive" or (trigger_source == "scheduled" and project.activation_state != "active"):
        raise ProjectScanError("project_inactive", "项目未启用")
    runtime = repository.get_runtime(project_id)
    if runtime is None or runtime.readiness_state != "ready":
        raise ProjectScanError("project_not_ready", "项目尚未就绪")
    running = repository.get_running_scan(project_id)
    if running is not None:
        return ScanReport(
            scan_id=running.scan_id,
            project_id=project_id,
            status=running.status,
            reused=True,
            matched_count=running.matched_count,
            created_count=running.created_count,
            duplicate_count=running.duplicate_count,
            unstable_count=running.unstable_count,
            unsupported_count=running.unsupported_count,
            excluded_count=running.excluded_count,
            failed_count=running.failed_count,
        )
    revision = repository.get_config_revision(project_id)
    if revision is None:
        raise ProjectScanError("data_integrity_error", "项目配置版本不存在", status=500)
    config = revision.config
    source_root = Path(str(config["source"]["directory"])).resolve(strict=False)
    if not source_root.is_dir():
        raise ProjectScanError("source_unavailable", "项目录像目录不可用")
    try:
        snapshot = resolve_parameter_snapshot(config, settings)
    except ResourceUnavailableError as exc:
        raise ProjectScanError("resource_unavailable", f"项目资源不可用：{exc.resource_id}") from exc
    candidates = _candidate_paths(source_root, scope=scope, selected_relative_paths=selected_relative_paths)
    try:
        scan = repository.create_scan_event(
            project_id,
            trigger_source=trigger_source,
            recovery_scan=recovery_scan,
            scheduled_at=scheduled_at,
            started_at=normalize_utc(_now(now)),
        )
    except sqlite3.IntegrityError:
        running = repository.get_running_scan(project_id)
        if running is None:
            raise
        return ScanReport(
            scan_id=running.scan_id,
            project_id=project_id,
            status=running.status,
            reused=True,
            matched_count=running.matched_count,
            created_count=running.created_count,
            duplicate_count=running.duplicate_count,
            unstable_count=running.unstable_count,
            unsupported_count=running.unsupported_count,
            excluded_count=running.excluded_count,
            failed_count=running.failed_count,
        )
    current = _now(now)
    supported = {str(extension).lower() for extension in config["source"]["supported_extensions"]}
    if not supported:
        supported = {str(extension).lower() for extension in SUPPORTED_VIDEO_EXTENSIONS}
    def candidate_order(path: Path) -> tuple[int, str]:
        try:
            modified_ns = path.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        return modified_ns, path.relative_to(source_root).as_posix()

    candidates.sort(key=candidate_order)
    counts = {
        "matched_count": 0,
        "created_count": 0,
        "duplicate_count": 0,
        "unstable_count": 0,
        "unsupported_count": 0,
        "excluded_count": 0,
        "failed_count": 0,
    }
    created_run_ids: list[str] = []
    failures: list[dict[str, str]] = []
    baseline = datetime.fromisoformat(runtime.discovery_baseline.replace("Z", "+00:00")) if runtime.discovery_baseline else None
    first_mode = config["source"]["first_scan_mode"]
    lookback = current - timedelta(days=int(config["source"].get("lookback_days") or 0))
    minimum_age = max(
        int(settings.recording_source_default.min_age_minutes) * 60,
        int(settings.recording_source_default.stable_check_seconds),
    )
    include_patterns = list(config["source"]["include_patterns"])
    exclude_patterns = list(config["source"]["exclude_patterns"])
    for index, candidate in enumerate(candidates):
        relative = candidate.relative_to(source_root).as_posix()
        try:
            if not candidate.is_file() or not _contained(candidate, source_root):
                counts["failed_count"] += 1
                failures.append({"path": relative, "code": "source_unavailable"})
                continue
            suffix = candidate.suffix.lower()
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        except OSError:
            counts["failed_count"] += 1
            failures.append({"path": relative, "code": "source_unavailable"})
            continue
        if suffix not in supported:
            counts["unsupported_count"] += 1
            continue
        if not _matches_patterns(relative, include_patterns, exclude_patterns):
            counts["excluded_count"] += 1
            continue
        if scope == "new" and baseline is not None and modified <= baseline:
            if not (first_mode == "recent" and runtime.first_scan_state == "pending" and modified >= lookback):
                counts["excluded_count"] += 1
                continue
        if (current - modified).total_seconds() < minimum_age:
            counts["unstable_count"] += 1
            continue
        counts["matched_count"] += 1
        try:
            identity = identity_fn(candidate, service_dir=service_dir)
            run_snapshot = {
                **snapshot,
                "project_id": project_id,
                "config_revision": revision.revision,
                "source": {"relative_path": relative, "bytes": int(identity["bytes"])},
                "work_dir": str(Path(settings.paths.work_dir) / "projects" / project_id),
            }
            created = repository.create_normal_run(
                project_id=project_id,
                content_id=str(identity["content_id"]),
                source_scan_id=scan.scan_id,
                trigger_source=trigger_source,
                first_seen_path=str(candidate.resolve()),
                latest_seen_path=str(candidate.resolve()),
                parameter_snapshot=run_snapshot,
                config_revision=revision.revision,
                queued_at=normalize_utc(current + timedelta(microseconds=index)),
            )
            if created.created:
                counts["created_count"] += 1
                created_run_ids.append(created.run.run_id)
            else:
                counts["duplicate_count"] += 1
        except Exception:  # noqa: BLE001 - a single source must not abort the scan.
            counts["failed_count"] += 1
            failures.append({"path": relative, "code": "source_read_failed"})
    status = "partial" if counts["failed_count"] else "success"
    repository.complete_scan_event(scan.scan_id, status=status, counts=counts)
    auto_state = "paused" if project.activation_state == "paused" else (
        "scheduled" if config["schedule"]["enabled"] else "off"
    )
    runtime_changes: dict[str, Any] = {"last_scan_at": normalize_utc(current), "auto_scan_state": auto_state}
    if first_mode == "recent" and status == "success":
        runtime_changes["first_scan_state"] = "completed"
    repository.update_runtime(project_id, **runtime_changes)
    repository.append_workspace_event(
        f"scan_{status}",
        project_id=project_id,
        scan_id=scan.scan_id,
        payload={**counts, "created_run_ids": created_run_ids},
        occurred_at=normalize_utc(current),
    )
    return ScanReport(
        scan_id=scan.scan_id,
        project_id=project_id,
        status=status,
        reused=False,
        created_run_ids=tuple(created_run_ids),
        failures=tuple(failures),
        **counts,
    )


def list_source_files(repository: ProjectRepository, project_id: str) -> list[SourceFile]:
    revision = repository.get_config_revision(project_id)
    if revision is None:
        raise ProjectScanError("project_not_found", "项目不存在", status=404)
    root = Path(str(revision.config["source"]["directory"])).resolve(strict=False)
    if not root.is_dir():
        raise ProjectScanError("source_unavailable", "项目录像目录不可用")
    supported = {str(extension).lower() for extension in revision.config["source"]["supported_extensions"]}
    files = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and _contained(item, root)),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        stat = path.stat()
        selectable = path.suffix.lower() in supported and _contained(path, root)
        files.append(
            SourceFile(
                relative_path=path.relative_to(root).as_posix(),
                bytes=stat.st_size,
                modified_at=normalize_utc(datetime.fromtimestamp(stat.st_mtime, UTC)),
                selectable=selectable,
                reason=None if selectable else "unsupported",
            )
        )
    return files


def scan_preview(
    source_directory: str | Path,
    *,
    supported_extensions: Iterable[str],
    first_scan_mode: str = "new_only",
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(source_directory).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise ProjectScanError("source_unavailable", "录像目录不可用", status=422)
    if first_scan_mode not in {"new_only", "recent", "choose_existing"}:
        raise ProjectScanError("validation_failed", "首次扫描模式无效", status=422)
    if first_scan_mode == "recent" and lookback_days not in {3, 7, 30}:
        raise ProjectScanError("validation_failed", "最近扫描天数只能是 3、7 或 30", status=422)
    if first_scan_mode != "recent" and lookback_days is not None:
        raise ProjectScanError("validation_failed", "仅最近扫描模式可以设置回溯天数", status=422)
    supported = {str(extension).lower() for extension in supported_extensions}
    paths = [path for path in root.rglob("*") if path.is_file() and _contained(path, root)]
    supported_paths = [path for path in paths if path.suffix.lower() in supported]
    if first_scan_mode == "new_only":
        processable = []
    elif first_scan_mode == "choose_existing":
        processable = supported_paths
    else:
        cutoff = _now(now) - timedelta(days=int(lookback_days or 0))
        processable = [path for path in supported_paths if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff]
    warnings = []
    if len(processable) >= 100:
        warnings.append("预计会创建较多剪辑记录")
    return {
        "estimated_files": len(paths),
        "supported_files": len(supported_paths),
        "processable_files": len(processable),
        "warnings": warnings,
    }
