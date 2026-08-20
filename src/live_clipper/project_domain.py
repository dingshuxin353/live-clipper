from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEGACY_ID_NAMESPACE = uuid.UUID("4ca0dcf0-a4dd-5ff9-b7bd-f4af56d78bb8")
_CREDENTIAL_KEY = re.compile(r"api[_-]?key|token|secret|password", re.IGNORECASE)
PROJECT_VIDEO_EXTENSIONS = (".m4v", ".mkv", ".mov", ".mp4", ".webm")


class ActivationState(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    PAUSED = "paused"


class DataMode(StrEnum):
    LEGACY = "legacy"
    PROJECTS = "projects"


class FirstScanState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETED = "completed"


class ScanStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    FAILED = "failed"
    COMPLETED = "completed"


class RunStage(StrEnum):
    READ_SOURCE = "read_source"
    TRANSCRIBE = "transcribe"
    ANALYZE = "analyze"
    ARBITRATE = "arbitrate"
    REVIEW = "review"
    RENDER = "render"


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    description: str
    activation_state: str
    current_config_revision: int
    created_at: str
    updated_at: str
    activated_at: str | None = None
    paused_at: str | None = None


@dataclass(frozen=True)
class ProjectConfigRevision:
    project_id: str
    revision: int
    config: dict[str, Any]
    schema_version: int
    created_at: str


@dataclass(frozen=True)
class ProjectRuntime:
    project_id: str
    readiness_state: str
    auto_scan_state: str
    last_scan_at: str | None
    next_scan_at: str | None
    failure_code: str | None
    failure_summary: str | None
    discovery_baseline: str | None
    first_scan_state: str
    schedule_cursor: str | None


@dataclass(frozen=True)
class ScanEvent:
    scan_id: str
    project_id: str
    trigger_source: str
    recovery_scan: bool
    scheduled_at: str | None
    started_at: str
    completed_at: str | None
    status: str
    matched_count: int
    created_count: int
    duplicate_count: int
    unstable_count: int
    unsupported_count: int
    excluded_count: int
    failed_count: int
    error_summary: str | None


@dataclass(frozen=True)
class Run:
    run_id: str
    project_id: str
    content_id: str
    processing_sequence: int
    origin_run_id: str | None
    source_scan_id: str | None
    trigger_source: str
    first_seen_path: str
    latest_seen_path: str
    status: str
    current_stage: str | None
    config_revision: int
    parameter_snapshot: dict[str, Any]
    queued_at: str
    started_at: str | None
    review_at: str | None
    completed_at: str | None
    updated_at: str
    error_code: str | None
    error_summary: str | None


@dataclass(frozen=True)
class RunStageEvent:
    event_id: int
    run_id: str
    stage: str
    event_type: str
    occurred_at: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class WorkspaceEvent:
    event_id: int
    event_type: str
    project_id: str | None
    run_id: str | None
    scan_id: str | None
    occurred_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class NormalRunCreationResult:
    created: bool
    duplicate: bool
    run: Run


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_utc(value: str | datetime | None = None) -> str:
    if value is None:
        return utc_now()
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def legacy_id(source_fingerprint: str, object_identity: str) -> str:
    return str(uuid.uuid5(LEGACY_ID_NAMESPACE, f"{source_fingerprint}:{object_identity}"))


def assert_secret_free(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _CREDENTIAL_KEY.search(str(key)):
                raise ValueError(f"credential field is not persistable: {path}.{key}")
            assert_secret_free(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_secret_free(item, path=f"{path}[{index}]")


def stable_json(value: Any) -> str:
    assert_secret_free(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(value: str) -> Any:
    return json.loads(value)


def default_project_config(source_directory: str | Path, output_directory: str | Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": {
            "directory": str(Path(source_directory).expanduser().resolve()),
            "supported_extensions": list(PROJECT_VIDEO_EXTENSIONS),
            "include_patterns": [],
            "exclude_patterns": [],
            "first_scan_mode": "new_only",
            "lookback_days": None,
        },
        "schedule": {
            "enabled": False,
            "mode": "daily",
            "daily_time": "22:00",
            "interval_minutes": None,
            "timezone": "Asia/Tokyo",
        },
        "resources": {
            "asr_ref": "legacy.asr.default",
            "analysis_ref": "legacy.analysis.default",
            "arbitration_mode": "reuse_analysis",
            "arbitration_ref": None,
        },
        "processing": {
            "review_strategy": "manual",
            "output_profile": "current_renderer",
            "naming_policy": "system_safe",
        },
        "output": {
            "directory": str(Path(output_directory).expanduser().resolve()),
            "intermediate_retention": "remind_after_7_days",
            "original_media_policy": "never_delete",
            "final_media_policy": "keep",
        },
    }


def validate_project_config(config: Mapping[str, Any]) -> dict[str, Any]:
    assert_secret_free(config)
    expected = {"schema_version", "source", "schedule", "resources", "processing", "output"}
    if set(config) != expected or config.get("schema_version") != 1:
        raise ValueError("project config must use the frozen schema_version 1 structure")
    required = {
        "source": {
            "directory",
            "supported_extensions",
            "include_patterns",
            "exclude_patterns",
            "first_scan_mode",
            "lookback_days",
        },
        "schedule": {"enabled", "mode", "daily_time", "interval_minutes", "timezone"},
        "resources": {"asr_ref", "analysis_ref", "arbitration_mode", "arbitration_ref"},
        "processing": {"review_strategy", "output_profile", "naming_policy"},
        "output": {
            "directory",
            "intermediate_retention",
            "original_media_policy",
            "final_media_policy",
        },
    }
    for section, keys in required.items():
        if not isinstance(config.get(section), Mapping) or set(config[section]) != keys:
            raise ValueError(f"project config section {section!r} does not match schema v1")
    source = config["source"]
    if source["supported_extensions"] != list(PROJECT_VIDEO_EXTENSIONS):
        raise ValueError("supported_extensions must use the current Venus video formats")
    if source["include_patterns"] or source["exclude_patterns"]:
        raise ValueError("include and exclude patterns are fixed in schema v1")
    if source["first_scan_mode"] not in {"new_only", "recent", "choose_existing"}:
        raise ValueError("invalid first_scan_mode")
    if source["first_scan_mode"] == "recent":
        if source["lookback_days"] not in {3, 7, 30}:
            raise ValueError("recent first scans require lookback_days of 3, 7, or 30")
    elif source["lookback_days"] is not None:
        raise ValueError("lookback_days is only valid for recent first scans")
    schedule = config["schedule"]
    if not isinstance(schedule["enabled"], bool):
        raise ValueError("schedule.enabled must be a boolean")
    if schedule["mode"] not in {"daily", "interval"}:
        raise ValueError("invalid schedule mode")
    if schedule["mode"] == "interval":
        if schedule["interval_minutes"] not in {30, 60, 180, 360, 720}:
            raise ValueError("invalid interval_minutes")
    elif schedule["interval_minutes"] is not None:
        raise ValueError("interval_minutes is only valid for interval schedules")
    if schedule["mode"] == "daily":
        if not isinstance(schedule["daily_time"], str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", schedule["daily_time"]
        ):
            raise ValueError("daily schedules require daily_time in HH:MM format")
    elif schedule["daily_time"] is not None:
        raise ValueError("daily_time is only valid for daily schedules")
    try:
        ZoneInfo(str(schedule["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid schedule timezone") from exc
    resources = config["resources"]
    if not all(isinstance(resources[field], str) and resources[field] for field in ("asr_ref", "analysis_ref")):
        raise ValueError("ASR and analysis resource references are required")
    if resources["arbitration_mode"] != "reuse_analysis" or resources["arbitration_ref"] is not None:
        raise ValueError("schema v1 arbitration must reuse the analysis resource")
    if config["output"]["intermediate_retention"] not in {
        "remind_immediately",
        "remind_after_7_days",
        "keep",
    }:
        raise ValueError("invalid intermediate_retention")
    if config["output"]["original_media_policy"] != "never_delete":
        raise ValueError("original media must never be deleted")
    if config["output"]["final_media_policy"] != "keep":
        raise ValueError("final media must be retained")
    # A JSON round-trip also rejects non-serializable objects and returns an owned copy.
    return json.loads(stable_json(config))
