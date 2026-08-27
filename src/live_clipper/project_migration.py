from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project_domain import default_project_config, legacy_id, utc_now
from .project_storage import ProjectRepository

_LEGACY_METADATA_NAMES = ("runs.json", "service.json", "scheduler.json", "events.jsonl")
_STATUS_MAP = {
    "queued": "queued",
    "staging": "processing",
    "processing": "processing",
    "rendering": "processing",
    "needs_review": "awaiting_review",
    "ready_to_render": "awaiting_review",
    "rendered": "completed",
    "failed": "failed",
}


@dataclass(frozen=True)
class LegacyInspection:
    service_dir: Path
    config_path: Path | None
    source_files: tuple[Path, ...]
    source_fingerprint: str
    source_directory: str
    output_directory: str
    timezone: str
    weekly_scan: bool
    runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MigrationPlan:
    service_dir: Path
    source_files: tuple[Path, ...]
    source_fingerprint: str
    project_id: str
    project_name: str
    project_config: dict[str, Any]
    runs: tuple[dict[str, Any], ...]
    quarantined_runs: tuple[dict[str, str], ...]
    needs_user_review: bool

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_fingerprint": self.source_fingerprint,
            "project_id": self.project_id,
            "planned_run_count": len(self.runs),
            "quarantined_runs": list(self.quarantined_runs),
            "needs_user_review": self.needs_user_review,
        }


@dataclass(frozen=True)
class MigrationResult:
    completed: bool
    already_applied: bool
    source_fingerprint: str
    project_id: str
    backup_path: Path
    imported_run_count: int
    quarantined_runs: tuple[dict[str, str], ...]
    needs_user_review: bool


@dataclass(frozen=True)
class MigrationV2Plan:
    """An explicit extension describing which imported Runs may later be result-indexed."""

    foundation: MigrationPlan
    result_index_run_ids: tuple[str, ...]
    compatibility_run_ids: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "foundation": self.foundation.summary(),
            "result_index_run_ids": list(self.result_index_run_ids),
            "compatibility_run_ids": list(self.compatibility_run_ids),
            "automatic_result_index": False,
        }


def _fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: (item.name, str(item))):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_run(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    # Deliberately whitelist legacy fields: errors and arbitrary snapshots may contain credentials.
    return {
        "legacy_run_id": str(raw.get("run_id") or f"legacy-index-{index}"),
        "content_id": str(raw["content_id"]) if raw.get("content_id") else None,
        "source_path": str(raw.get("first_source_path") or raw.get("source_path") or ""),
        "latest_source_path": str(raw.get("last_source_path") or raw.get("source_path") or ""),
        "phase": str(raw.get("phase") or "failed"),
        "created_at": raw.get("discovered_at") or raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


def inspect_legacy_state(
    service_dir: str | Path,
    *,
    config_path: str | Path | None = None,
) -> LegacyInspection:
    """Read and fingerprint 0.3.x metadata. This function never writes or scans media."""
    service = Path(service_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve() if config_path is not None else None
    files: list[Path] = []
    config_data: dict[str, Any] = {}
    if config is not None and config.is_file():
        files.append(config)
        with config.open("rb") as handle:
            config_data = tomllib.load(handle)
    for name in _LEGACY_METADATA_NAMES:
        path = service / name
        if path.is_file():
            files.append(path)
    if not files:
        raise FileNotFoundError("no legacy metadata files found")

    source_group = config_data.get("recording_source", {})
    if isinstance(source_group, Mapping) and isinstance(source_group.get("default"), Mapping):
        source_group = source_group["default"]
    if not isinstance(source_group, Mapping):
        source_group = {}
    config_base = config.parent if config is not None else service.parent

    def resolved(value: Any, fallback: str) -> str:
        raw = str(value or fallback)
        path = Path(raw).expanduser()
        return str((path if path.is_absolute() else config_base / path).resolve())

    source_directory = resolved(source_group.get("source_dir"), "recordings")
    output_directory = resolved(source_group.get("output_root"), "output")
    scheduler = config_data.get("scheduler", {})
    if not isinstance(scheduler, Mapping):
        scheduler = {}
    timezone = str(scheduler.get("timezone") or "Asia/Tokyo")
    jobs = scheduler.get("jobs", [])
    weekly_scan = any(
        isinstance(job, Mapping)
        and job.get("type") == "scan_recordings"
        and job.get("schedule") == "weekly"
        for job in (jobs if isinstance(jobs, list) else [])
    )
    raw_runs: list[Any] = []
    runs_path = service / "runs.json"
    if runs_path.is_file():
        loaded = json.loads(runs_path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping) and isinstance(loaded.get("runs"), list):
            raw_runs = loaded["runs"]
    runs = tuple(_safe_run(run, index) for index, run in enumerate(raw_runs) if isinstance(run, Mapping))
    return LegacyInspection(
        service_dir=service,
        config_path=config,
        source_files=tuple(files),
        source_fingerprint=_fingerprint(files),
        source_directory=source_directory,
        output_directory=output_directory,
        timezone=timezone,
        weekly_scan=weekly_scan,
        runs=runs,
    )


def _utc(value: Any, *, fallback: str = "1970-01-01T00:00:00Z") -> str:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_migration_plan(inspection: LegacyInspection) -> MigrationPlan:
    """Build a deterministic, secret-free plan without touching SQLite or backups."""
    fingerprint = inspection.source_fingerprint
    config = default_project_config(inspection.source_directory, inspection.output_directory)
    config["schedule"]["timezone"] = inspection.timezone
    # Weekly has no v1 equivalent. Keep automatic discovery off pending an explicit user decision.
    config["schedule"]["enabled"] = False
    planned_runs: list[dict[str, Any]] = []
    quarantined: list[dict[str, str]] = []
    seen_content_ids: set[str] = set()
    for raw in inspection.runs:
        legacy_run_id = raw["legacy_run_id"]
        content_id = raw.get("content_id")
        if not content_id:
            quarantined.append(
                {
                    "legacy_run_id": legacy_run_id,
                    "reason": "content_identity_unrecoverable_without_media_scan",
                }
            )
            continue
        if str(content_id) in seen_content_ids:
            quarantined.append(
                {
                    "legacy_run_id": legacy_run_id,
                    "reason": "duplicate_legacy_content_identity",
                }
            )
            continue
        seen_content_ids.add(str(content_id))
        status = _STATUS_MAP.get(str(raw.get("phase")), "failed")
        queued_at = _utc(raw.get("created_at"))
        updated_at = _utc(raw.get("updated_at"), fallback=queued_at)
        planned_runs.append(
            {
                "run_id": legacy_id(fingerprint, f"run:{legacy_run_id}"),
                "content_id": str(content_id),
                "first_seen_path": str(raw.get("source_path") or ""),
                "latest_seen_path": str(raw.get("latest_source_path") or raw.get("source_path") or ""),
                "status": status,
                "current_stage": None,
                "parameter_snapshot": {"legacy_run_id": legacy_run_id},
                "queued_at": queued_at,
                "started_at": queued_at if status == "processing" else None,
                "review_at": updated_at if status == "awaiting_review" else None,
                "completed_at": updated_at if status == "completed" else None,
                "updated_at": updated_at,
                "error_code": "legacy_failed" if status == "failed" else None,
                "error_summary": None,
            }
        )
    return MigrationPlan(
        service_dir=inspection.service_dir,
        source_files=inspection.source_files,
        source_fingerprint=fingerprint,
        project_id=legacy_id(fingerprint, "project:default"),
        project_name="默认项目",
        project_config=config,
        runs=tuple(planned_runs),
        quarantined_runs=tuple(quarantined),
        needs_user_review=inspection.weekly_scan,
    )


def build_migration_v2_plan(inspection: LegacyInspection) -> MigrationV2Plan:
    """Extend the conversion plan without reading result artifacts or changing data mode."""
    foundation = build_migration_plan(inspection)
    return MigrationV2Plan(
        foundation=foundation,
        result_index_run_ids=tuple(
            item["run_id"] for item in foundation.runs if item["status"] == "completed"
        ),
        compatibility_run_ids=tuple(
            item["run_id"] for item in foundation.runs if item["status"] == "awaiting_review"
        ),
    )


def _backup_files(plan: MigrationPlan, backup_path: Path) -> None:
    backup_path.mkdir(parents=True, exist_ok=False)
    try:
        for source in plan.source_files:
            shutil.copy2(source, backup_path / source.name)
    except BaseException:
        # A partial backup is not valid evidence. It contains only copies and is safe to remove.
        shutil.rmtree(backup_path)
        raise


def apply_migration_plan(plan: MigrationPlan, repository: ProjectRepository) -> MigrationResult:
    """Back up then atomically apply one explicit plan; legacy source bytes are never modified."""
    existing = repository.get_legacy_import(plan.source_fingerprint)
    if existing is not None:
        summary = existing["summary"]
        return MigrationResult(
            completed=existing["status"] == "completed",
            already_applied=True,
            source_fingerprint=plan.source_fingerprint,
            project_id=str(summary["project_id"]),
            backup_path=Path(existing["backup_path"]),
            imported_run_count=int(summary.get("imported_run_count", 0)),
            quarantined_runs=plan.quarantined_runs,
            needs_user_review=plan.needs_user_review,
        )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = plan.service_dir / "migration-backups" / f"{timestamp}-{plan.source_fingerprint}"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_files(plan, backup_path)
    occurred_at = utc_now()
    created, project_id = repository.apply_legacy_import(
        source_fingerprint=plan.source_fingerprint,
        import_id=legacy_id(plan.source_fingerprint, "import"),
        project_id=plan.project_id,
        project_name=plan.project_name,
        project_config=plan.project_config,
        runs=plan.runs,
        plan_summary=plan.summary(),
        backup_path=str(backup_path),
        occurred_at=occurred_at,
    )
    if not created:
        existing = repository.get_legacy_import(plan.source_fingerprint)
        assert existing is not None
        backup_path = Path(existing["backup_path"])
    return MigrationResult(
        completed=True,
        already_applied=not created,
        source_fingerprint=plan.source_fingerprint,
        project_id=project_id,
        backup_path=backup_path,
        imported_run_count=len(plan.runs),
        quarantined_runs=plan.quarantined_runs,
        needs_user_review=plan.needs_user_review,
    )
