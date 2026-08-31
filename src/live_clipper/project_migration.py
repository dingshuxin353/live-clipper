from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .project_domain import PROJECT_VIDEO_EXTENSIONS, assert_secret_free

PLAN_VERSION = 3
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
BACKUP_SAFETY_MARGIN_BYTES = 64 * 1024 * 1024
_LEGACY_METADATA_NAMES = (
    "runs.json",
    "service.json",
    "scheduler.json",
    "scheduler_runs.json",
    "events.jsonl",
)
_INTERVALS = frozenset({30, 60, 180, 360, 720})
_DAILY_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_SAFE_RESULT_EXTENSIONS = frozenset({".m4v", ".mov", ".mp4", ".webm"})


class LegacySourceError(ValueError):
    """Stable, secret-free rejection for an untrusted legacy source."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceManifestEntry:
    logical_type: str
    source_identity: str
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_type": self.logical_type,
            "source_identity": self.source_identity,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class LegacyInspection:
    service_dir: Path = field(repr=False)
    config_path: Path | None = field(repr=False)
    source_files: tuple[Path, ...] = field(repr=False)
    source_manifest: tuple[SourceManifestEntry, ...]
    source_fingerprint: str
    source_directory: str
    output_directory: str
    timezone: str
    weekly_scan: bool
    runs: tuple[Mapping[str, Any], ...]
    resource_facts: Mapping[str, Any]


@dataclass(frozen=True)
class MigrationPlan:
    plan_version: int
    source_fingerprint: str
    source_manifest: tuple[SourceManifestEntry, ...]
    project_preview: Mapping[str, Any]
    resource_summary: Mapping[str, Any]
    discovery_summary: Mapping[str, Any]
    history_summary: Mapping[str, Any]
    backup_summary: Mapping[str, Any]
    readiness_summary: Mapping[str, Any]
    requires_user_choices: tuple[str, ...]
    choices: Mapping[str, Any]
    plan_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "source_fingerprint": self.source_fingerprint,
            "source_manifest": [item.to_dict() for item in self.source_manifest],
            "project_preview": _thaw(self.project_preview),
            "resource_summary": _thaw(self.resource_summary),
            "discovery_summary": _thaw(self.discovery_summary),
            "history_summary": _thaw(self.history_summary),
            "backup_summary": _thaw(self.backup_summary),
            "readiness_summary": _thaw(self.readiness_summary),
            "requires_user_choices": list(self.requires_user_choices),
            "choices": _thaw(self.choices),
            "plan_hash": self.plan_hash,
        }

    def stable_json(self) -> str:
        return _stable_json(self.to_dict())


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _stable_json(value: Any) -> str:
    plain = _thaw(value)
    assert_secret_free(plain)
    return json.dumps(plain, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _read_approved_file(
    path: Path, *, logical_type: str, source_identity: str
) -> tuple[SourceManifestEntry, bytes]:
    try:
        before_path = path.lstat()
    except FileNotFoundError as exc:
        raise LegacySourceError("legacy_source_missing") from exc
    except OSError as exc:
        raise LegacySourceError("legacy_source_unreadable") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise LegacySourceError("legacy_source_unsafe")
    if before_path.st_mode & 0o444 == 0:
        raise LegacySourceError("legacy_source_unreadable")
    if before_path.st_size > MAX_SOURCE_FILE_BYTES:
        raise LegacySourceError("legacy_source_too_large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LegacySourceError("legacy_source_unreadable") from exc
    try:
        before_fd = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_SOURCE_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SOURCE_FILE_BYTES:
                raise LegacySourceError("legacy_source_too_large")
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise LegacySourceError("legacy_source_changed") from exc
    identity_before = (before_path.st_dev, before_path.st_ino, before_path.st_size, before_path.st_mtime_ns)
    identity_fd = (before_fd.st_dev, before_fd.st_ino, before_fd.st_size, before_fd.st_mtime_ns)
    identity_after_fd = (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns)
    identity_after = (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns)
    if not identity_before == identity_fd == identity_after_fd == identity_after or total != before_path.st_size:
        raise LegacySourceError("legacy_source_changed")
    data = b"".join(chunks)
    return (
        SourceManifestEntry(
            logical_type=logical_type,
            source_identity=source_identity,
            size=total,
            mtime_ns=before_path.st_mtime_ns,
            sha256=hashlib.sha256(data).hexdigest(),
        ),
        data,
    )


def _safe_run(raw: Mapping[str, Any], index: int) -> Mapping[str, Any]:
    return _freeze(
        {
            "legacy_run_id": str(raw.get("run_id") or f"legacy-index-{index}"),
            "content_id": str(raw["content_id"]) if raw.get("content_id") else None,
            "source_path": str(raw.get("first_source_path") or raw.get("source_path") or ""),
            "latest_source_path": str(raw.get("last_source_path") or raw.get("source_path") or ""),
            "phase": str(raw.get("phase") or ""),
            "created_at": raw.get("discovered_at") or raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "result_path": str(raw.get("result_path") or raw.get("output_path") or ""),
            "result_sha256": str(raw.get("result_sha256") or ""),
        }
    )


def _resource_facts(config: Mapping[str, Any]) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    for key, config_key, label in (("asr", "asr", "语音识别"), ("ai", "llm", "AI 服务")):
        raw = config.get(config_key, {})
        if not isinstance(raw, Mapping):
            raw = {}
        model = raw.get("model") or raw.get("model_id")
        credential = raw.get("api_key")
        result[key] = {
            "label": label,
            "model": str(model) if isinstance(model, str) and model.strip() else None,
            "credential_present": isinstance(credential, str) and bool(credential.strip()),
        }
    return _freeze(result)


def inspect_legacy_state(
    service_dir: str | Path,
    *,
    config_path: str | Path | None = None,
) -> LegacyInspection:
    """Inspect only approved 0.3.x metadata through stable read-only file descriptors."""
    service = _absolute(service_dir)
    config = _absolute(config_path) if config_path is not None else None
    if service.is_symlink() or (config is not None and config.is_symlink()):
        raise LegacySourceError("legacy_source_unsafe")
    approved: list[tuple[Path, str, str]] = []
    if config is not None and config.exists():
        approved.append((config, "config", "config/live-clipper.toml"))
    for name in _LEGACY_METADATA_NAMES:
        path = service / name
        if path.exists() or path.is_symlink():
            logical = "runs" if name == "runs.json" else name.removesuffix(".json").removesuffix(".jsonl")
            approved.append((path, logical, f"service/{name}"))
    if not approved:
        raise LegacySourceError("legacy_source_missing")
    entries: list[SourceManifestEntry] = []
    contents: dict[str, bytes] = {}
    source_files: list[Path] = []
    for path, logical, identity in sorted(approved, key=lambda item: (item[1], item[2])):
        entry, data = _read_approved_file(path, logical_type=logical, source_identity=identity)
        entries.append(entry)
        contents[identity] = data
        source_files.append(path)
    if sum(item.size for item in entries) > MAX_SOURCE_BYTES:
        raise LegacySourceError("legacy_source_too_large")

    config_data: dict[str, Any] = {}
    config_bytes = contents.get("config/live-clipper.toml")
    if config_bytes is not None:
        try:
            config_data = tomllib.loads(config_bytes.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise LegacySourceError("legacy_config_invalid") from exc
    metadata: dict[str, Any] = {}
    for identity, data in contents.items():
        if identity == "config/live-clipper.toml":
            continue
        try:
            text = data.decode("utf-8")
            metadata[identity] = (
                [json.loads(line) for line in text.splitlines() if line.strip()]
                if identity.endswith("events.jsonl")
                else json.loads(text)
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LegacySourceError("legacy_metadata_invalid") from exc

    source_group = config_data.get("recording_source", {})
    if isinstance(source_group, Mapping) and isinstance(source_group.get("default"), Mapping):
        source_group = source_group["default"]
    if not isinstance(source_group, Mapping):
        source_group = {}
    config_base = config.parent if config is not None else service.parent

    def normalized_directory(value: Any, fallback: str) -> str:
        candidate = Path(os.path.expanduser(str(value or fallback)))
        return str(_absolute(candidate if candidate.is_absolute() else config_base / candidate))

    source_directory = normalized_directory(source_group.get("source_dir"), "recordings")
    output_directory = normalized_directory(source_group.get("output_root"), "output")
    scheduler = config_data.get("scheduler", {})
    if not isinstance(scheduler, Mapping):
        scheduler = {}
    timezone = str(scheduler.get("timezone") or "Asia/Tokyo")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise LegacySourceError("legacy_timezone_invalid") from exc
    jobs = scheduler.get("jobs", [])
    weekly_scan = any(
        isinstance(job, Mapping)
        and job.get("type") == "scan_recordings"
        and job.get("schedule") == "weekly"
        for job in (jobs if isinstance(jobs, list) else [])
    )
    raw_runs: list[Any] = []
    runs_payload = metadata.get("service/runs.json")
    if isinstance(runs_payload, Mapping) and isinstance(runs_payload.get("runs"), list):
        raw_runs = runs_payload["runs"]
    elif runs_payload is not None:
        raise LegacySourceError("legacy_metadata_invalid")
    runs = tuple(_safe_run(raw, index) for index, raw in enumerate(raw_runs) if isinstance(raw, Mapping))

    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: (item.logical_type, item.source_identity)):
        digest.update(entry.logical_type.encode())
        digest.update(b"\0")
        digest.update(entry.source_identity.encode())
        digest.update(b"\0")
        digest.update(contents[entry.source_identity])
        digest.update(b"\0")
    return LegacyInspection(
        service_dir=service,
        config_path=config,
        source_files=tuple(source_files),
        source_manifest=tuple(entries),
        source_fingerprint=digest.hexdigest(),
        source_directory=source_directory,
        output_directory=output_directory,
        timezone=timezone,
        weekly_scan=weekly_scan,
        runs=runs,
        resource_facts=_resource_facts(config_data),
    )


def _normalized_utc(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _inside(path: str, root: str, *, extensions: frozenset[str] | tuple[str, ...]) -> bool:
    if not path:
        return False
    candidate = _absolute(path)
    base = _absolute(root)
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return candidate.suffix.lower() in extensions


def _history_summary(inspection: LegacyInspection) -> Mapping[str, Any]:
    content_counts = Counter(str(item["content_id"]) for item in inspection.runs if item.get("content_id"))
    entries: list[dict[str, Any]] = []
    for raw in inspection.runs:
        legacy_run_id = str(raw["legacy_run_id"])
        content_id = str(raw["content_id"]) if raw.get("content_id") else None
        phase = str(raw.get("phase") or "")
        created = _normalized_utc(raw.get("created_at"))
        updated = _normalized_utc(raw.get("updated_at")) if raw.get("updated_at") else created
        reason: str | None = None
        category = "importable"
        target_state = "completed" if phase in {"rendered", "completed"} else "failed"
        if content_id is None:
            reason = "content_identity_missing"
        elif content_counts[content_id] > 1:
            reason = "duplicate_content_identity"
        elif not _inside(str(raw.get("source_path") or ""), inspection.source_directory, extensions=PROJECT_VIDEO_EXTENSIONS):
            reason = "source_identity_unsupported"
        elif created is None or updated is None or updated < created:
            reason = "timestamp_untrusted"
        elif phase in {"needs_review", "ready_to_render", "awaiting_review"}:
            category = "compatibility"
            target_state = "failed"
        elif phase in {"queued", "staging", "processing", "rendering"}:
            target_state = "failed"
        elif phase not in {"rendered", "completed", "failed"}:
            reason = "state_unrecognized"
        if reason is not None:
            entries.append({"category": "quarantined", "legacy_run_id": legacy_run_id, "reason_code": reason})
            continue
        item: dict[str, Any] = {
            "category": category,
            "legacy_run_id": legacy_run_id,
            "content_id": content_id,
            "source_identity": str(raw.get("source_path") or ""),
            "phase": phase,
            "created_at": created,
            "updated_at": updated,
            "target_state": target_state,
            "failure_code": "legacy_processing_interrupted"
            if phase in {"queued", "staging", "processing", "rendering"}
            else None,
        }
        result_path = str(raw.get("result_path") or "")
        result_hash = str(raw.get("result_sha256") or "")
        if (
            category == "importable"
            and target_state == "completed"
            and _inside(result_path, inspection.output_directory, extensions=_SAFE_RESULT_EXTENSIONS)
            and re.fullmatch(r"[0-9a-f]{64}", result_hash)
        ):
            item["safe_result"] = {
                "category": "safe_result",
                "path_identity": result_path,
                "sha256": result_hash,
            }
        entries.append(item)
    counts = Counter(item["category"] for item in entries)
    return _freeze(
        {
            "entries": entries,
            "counts": {
                "importable": counts["importable"],
                "compatibility": counts["compatibility"],
                "quarantined": counts["quarantined"],
                "safe_result": sum("safe_result" in item for item in entries),
            },
        }
    )


def _directory_status(path: str) -> str:
    candidate = _absolute(path)
    if candidate.is_symlink():
        return "unsafe"
    if not candidate.exists():
        return "unavailable"
    return "ready" if candidate.is_dir() else "unsafe"


def _normalize_choices(inspection: LegacyInspection, choices: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = dict(choices or {})
    allowed = {
        "project_name",
        "source_directory",
        "output_directory",
        "trigger_mode",
        "schedule_mode",
        "daily_time",
        "interval_minutes",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported migration choice: {sorted(unknown)[0]}")
    project_name = str(raw.get("project_name") or "默认项目").strip()
    if not project_name or len(project_name) > 120:
        raise ValueError("project_name must contain 1 to 120 characters")
    source = str(_absolute(raw.get("source_directory") or inspection.source_directory))
    output = str(_absolute(raw.get("output_directory") or inspection.output_directory))
    trigger = str(raw.get("trigger_mode") or "manual")
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("trigger_mode must be manual or scheduled")
    schedule_mode: str | None = None
    daily_time: str | None = None
    interval_minutes: int | None = None
    if trigger == "scheduled":
        schedule_mode = str(raw.get("schedule_mode") or "")
        if schedule_mode == "daily":
            daily_time = str(raw.get("daily_time") or "")
            if not _DAILY_TIME.fullmatch(daily_time):
                raise ValueError("daily_time must be HH:MM")
        elif schedule_mode == "interval":
            interval_minutes = raw.get("interval_minutes")
            if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or interval_minutes not in _INTERVALS:
                raise ValueError("interval_minutes is unsupported")
        else:
            raise ValueError("schedule_mode must be daily or interval")
    return _freeze(
        {
            "project_name": project_name,
            "source_directory": source,
            "output_directory": output,
            "trigger_mode": trigger,
            "schedule_mode": schedule_mode,
            "daily_time": daily_time,
            "interval_minutes": interval_minutes,
        }
    )


def _available_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return int(shutil.disk_usage(probe).free)


def build_migration_plan(
    inspection: LegacyInspection,
    *,
    choices: Mapping[str, Any] | None = None,
    backup_root: str | Path | None = None,
    available_bytes: int | None = None,
) -> MigrationPlan:
    """Build an immutable, deterministic and secret-free migration plan without writing."""
    normalized_choices = _normalize_choices(inspection, choices)
    source_status = _directory_status(str(normalized_choices["source_directory"]))
    output_status = _directory_status(str(normalized_choices["output_directory"]))
    resources: dict[str, Any] = {}
    for key, value in inspection.resource_facts.items():
        item = dict(value)
        ready = bool(item.get("model")) and bool(item.get("credential_present"))
        resources[str(key)] = {**item, "status": "ready" if ready else "problem"}
    history = _history_summary(inspection)
    source_bytes = sum(item.size for item in inspection.source_manifest)
    required_bytes = source_bytes * 2 + BACKUP_SAFETY_MARGIN_BYTES
    backup_path = _absolute(backup_root or inspection.service_dir.parent / "migration-backups")
    free = int(available_bytes) if available_bytes is not None else _available_bytes(backup_path)
    if free < 0:
        raise ValueError("available_bytes must not be negative")
    required_choices: list[str] = []
    raw_choices = choices or {}
    if source_status != "ready" and "source_directory" not in raw_choices:
        required_choices.append("source_directory")
    if output_status != "ready" and "output_directory" not in raw_choices:
        required_choices.append("output_directory")
    if inspection.weekly_scan and "trigger_mode" not in raw_choices:
        required_choices.append("trigger_mode")
    problems = [key for key, item in resources.items() if item["status"] == "problem"]
    if source_status != "ready":
        problems.append("source_directory")
    if output_status != "ready":
        problems.append("output_directory")
    if free < required_bytes:
        problems.append("backup_space")
    payload: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "source_fingerprint": inspection.source_fingerprint,
        "source_manifest": [item.to_dict() for item in inspection.source_manifest],
        "project_preview": {
            "name": normalized_choices["project_name"],
            "source_directory": normalized_choices["source_directory"],
            "output_directory": normalized_choices["output_directory"],
            "trigger_mode": normalized_choices["trigger_mode"],
            "schedule_mode": normalized_choices["schedule_mode"],
            "daily_time": normalized_choices["daily_time"],
            "interval_minutes": normalized_choices["interval_minutes"],
            "timezone": inspection.timezone,
        },
        "resource_summary": resources,
        "discovery_summary": {
            "legacy_weekly_detected": inspection.weekly_scan,
            "default_trigger_mode": "manual",
            "existing_recordings_scanned": False,
        },
        "history_summary": _thaw(history),
        "backup_summary": {
            "target_path": str(backup_path),
            "target_display": backup_path.name,
            "source_bytes": source_bytes,
            "required_bytes": required_bytes,
            "available_bytes": free,
            "space_status": "ready" if free >= required_bytes else "insufficient",
        },
        "readiness_summary": {
            "source_status": source_status,
            "output_status": output_status,
            "resource_problems": sorted(problems),
            "can_start": not required_choices and free >= required_bytes,
        },
        "requires_user_choices": sorted(required_choices),
        "choices": _thaw(normalized_choices),
    }
    plan_hash = hashlib.sha256(_stable_json(payload).encode()).hexdigest()
    return MigrationPlan(
        plan_version=PLAN_VERSION,
        source_fingerprint=inspection.source_fingerprint,
        source_manifest=inspection.source_manifest,
        project_preview=_freeze(payload["project_preview"]),
        resource_summary=_freeze(payload["resource_summary"]),
        discovery_summary=_freeze(payload["discovery_summary"]),
        history_summary=_freeze(payload["history_summary"]),
        backup_summary=_freeze(payload["backup_summary"]),
        readiness_summary=_freeze(payload["readiness_summary"]),
        requires_user_choices=tuple(payload["requires_user_choices"]),
        choices=_freeze(payload["choices"]),
        plan_hash=plan_hash,
    )
