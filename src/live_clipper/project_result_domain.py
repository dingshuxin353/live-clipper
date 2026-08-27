from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any


class ReviewSessionStatus(StrEnum):
    RUNNING = "running"
    SELECTED = "selected"
    NO_CLIP = "no_clip"
    FAILED = "failed"
    INVALID = "invalid"


class CandidateDecisionType(StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"


class RunResultType(StrEnum):
    CLIPS_READY = "clips_ready"
    NO_CLIP = "no_clip"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class RunOutputStatus(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"
    MISSING = "missing"
    UNREADABLE = "unreadable"


class OutputStorageKind(StrEnum):
    PROJECT_OUTPUT = "project_output"
    RUN_WORKSPACE_COMPAT = "run_workspace_compat"


class OutputMaterialStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class IssueScope(StrEnum):
    PROJECT = "project"
    RUN = "run"
    OUTPUT = "output"
    MATERIAL = "material"


class IssueStatus(StrEnum):
    RETRYING = "retrying"
    ACTION_REQUIRED = "action_required"
    CHECKING = "checking"
    READY_TO_RECOVER = "ready_to_recover"
    RECOVERING = "recovering"
    RESOLVED = "resolved"


class RecoveryAttemptType(StrEnum):
    CONTINUE_RUN = "continue_run"
    RETRY_OUTPUT = "retry_output"
    RETRY_MATERIAL = "retry_material"
    OPERATIONAL_REPAIR = "operational_repair"


class RecoveryAttemptStatus(StrEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class AIReviewSession:
    review_session_id: str
    run_id: str
    attempt_number: int
    status: str
    resource_ref: str
    model_name: str
    strategy_version: str
    config_revision: int
    parameter_snapshot: dict[str, Any]
    format_version: int
    overall_summary: str
    warnings: list[dict[str, Any]]
    candidate_count: int
    selected_count: int
    rejected_count: int
    evidence_relative_path: str | None
    evidence_sha256: str | None
    started_at: str
    completed_at: str | None
    validated_at: str | None
    updated_at: str


@dataclass(frozen=True)
class CandidateDecision:
    decision_id: str
    review_session_id: str
    run_id: str
    candidate_id: str
    decision: str
    rank: int
    candidate_type: str
    source_start_ms: int
    source_end_ms: int
    selected_start_ms: int | None
    selected_end_ms: int | None
    remove_ranges: list[list[int]]
    hook: str
    core_value: str
    reason: str
    rejection_reason_code: str | None
    risks: list[dict[str, Any]]
    transcript_excerpt: str
    output_id: str | None
    internal_sort_value: float | None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    review_session_id: str
    result_type: str
    candidate_count: int
    selected_count: int
    rejected_count: int
    available_output_count: int
    failed_output_count: int
    total_duration_ms: int
    overall_summary: str
    warnings: list[dict[str, Any]]
    format_version: int
    result_revision: int
    seen_result_revision: int | None
    result_seen_at: str | None
    source_kind: str
    evidence_hash: str | None
    completed_at: str
    updated_at: str

    @property
    def unseen(self) -> bool:
        return self.seen_result_revision is None or self.seen_result_revision < self.result_revision


@dataclass(frozen=True)
class RunOutput:
    output_id: str
    run_id: str
    review_session_id: str
    candidate_id: str
    display_order: int
    status: str
    storage_kind: str
    relative_path: str
    file_name: str
    duration_ms: int | None
    width: int | None
    height: int | None
    container: str | None
    video_codec: str | None
    byte_size: int | None
    generated_at: str | None
    verified_at: str | None
    updated_at: str
    error_code: str | None
    error_summary: str | None


@dataclass(frozen=True)
class OutputMaterial:
    material_id: str
    output_id: str
    title_candidates: list[dict[str, Any]]
    preferred_title_id: str | None
    description: str
    tags: list[str]
    generation_source: str
    status: str
    material_revision: int
    created_at: str
    saved_at: str | None
    updated_at: str


@dataclass(frozen=True)
class Issue:
    issue_id: str
    issue_code: str
    category: str
    scope_type: str
    project_id: str
    run_id: str | None
    output_id: str | None
    material_id: str | None
    issue_group_key: str
    root_cause_ref: str | None
    status: str
    impact_level: str
    title: str
    summary: str
    impact: str
    preserved_content: str
    next_step: str
    recovery_capability: str
    safe_checkpoint: str | None
    reuse_stages: list[str]
    redo_stages: list[str]
    operational_overrides: dict[str, Any]
    automatic_attempt_count: int
    total_attempt_count: int
    next_retry_at: str | None
    retry_exhausted: bool
    diagnostic_id: str | None
    diagnostic_summary: str | None
    log_relative_path: str | None
    issue_revision: int
    occurred_at: str
    updated_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class IssueEvent:
    event_id: int
    issue_id: str
    event_type: str
    occurred_at: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class RecoveryAttempt:
    attempt_id: str
    issue_id: str
    request_id: str
    attempt_type: str
    run_id: str | None
    output_id: str | None
    material_id: str | None
    requested_by: str
    reuse_stages: list[str]
    redo_stages: list[str]
    operational_overrides: dict[str, Any]
    status: str
    requested_at: str
    accepted_at: str | None
    started_at: str | None
    completed_at: str | None
    failed_at: str | None
    result: dict[str, Any]


class RevisionConflictError(ValueError):
    """Raised when a compare-and-set write targets an obsolete revision."""


class RequestConflictError(ValueError):
    """Raised when an idempotency key is reused for a different operation."""


_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._-]+|sk-[a-z0-9_-]{6,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)
_ABSOLUTE_USER_PATH = re.compile(r"/(?:Users|home)/[^\s/]+(?:/[^\s]+)*")
_PUBLIC_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def sanitize_persisted_text(value: Any) -> str:
    """Return a functional, secret-free summary suitable for durable storage."""
    text = "" if value is None else str(value)
    text = _SECRET_VALUE.sub("[redacted]", text)
    return _ABSOLUTE_USER_PATH.sub("[redacted-path]", text)


def validate_relative_reference(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{field} must be a controlled relative path")
    return path.as_posix()


def validate_sha256(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", lowered):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return lowered


def validate_public_identifier(value: Any, *, field: str) -> str:
    normalized = str(value or "")
    if not _PUBLIC_IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} must be a stable, non-sensitive identifier")
    return normalized


def validate_titles(titles: Any, preferred_title_id: str | None) -> list[dict[str, Any]]:
    if not isinstance(titles, list):
        raise ValueError("title_candidates must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in titles:
        if not isinstance(item, dict) or set(item) != {"title_id", "text"}:
            raise ValueError("each title candidate must contain title_id and text")
        title_id = str(item["title_id"])
        if not title_id or title_id in seen:
            raise ValueError("title_id must be non-empty and unique")
        seen.add(title_id)
        normalized.append({"title_id": title_id, "text": sanitize_persisted_text(item["text"])})
    if preferred_title_id is not None and preferred_title_id not in seen:
        raise ValueError("preferred_title_id must reference a title candidate")
    return normalized


def validate_remove_ranges(value: Any, *, start_ms: int, end_ms: int) -> list[list[int]]:
    if not isinstance(value, list):
        raise ValueError("remove_ranges must be a list")
    normalized: list[list[int]] = []
    previous_end = start_ms
    removed_duration = 0
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or any(not isinstance(point, int) or isinstance(point, bool) for point in item)
        ):
            raise ValueError("remove ranges must contain integer millisecond pairs")
        remove_start, remove_end = item
        if remove_start < start_ms or remove_end > end_ms or remove_start >= remove_end:
            raise ValueError("remove range is outside the selected range")
        if remove_start < previous_end:
            raise ValueError("remove ranges must be ordered and non-overlapping")
        normalized.append([remove_start, remove_end])
        previous_end = remove_end
        removed_duration += remove_end - remove_start
    if removed_duration >= end_ms - start_ms:
        raise ValueError("remove ranges cannot remove the entire selected clip")
    return normalized
