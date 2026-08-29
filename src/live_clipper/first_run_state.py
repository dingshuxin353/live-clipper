from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .project_domain import assert_secret_free

FIRST_RUN_SESSION_ID = "primary"
FIRST_RUN_STATES = frozenset({"in_progress", "paused", "activation_pending", "completed"})
FIRST_RUN_STEPS = frozenset({"welcome", "asr", "ai", "project", "complete"})

_NON_PERSISTABLE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|apikey|token|secret|authorization|bearer|password|credential|raw[_-]?response|prompt|hidden[_-]?reasoning)"
)
_DAILY_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "asr": frozenset({"mode", "local_model_id", "model_source", "api_base", "model"}),
    "ai": frozenset({"provider_id", "api_base", "model"}),
    "project": frozenset(
        {
            "name",
            "source_directory",
            "trigger_mode",
            "schedule_mode",
            "daily_time",
            "interval_minutes",
            "output_directory",
        }
    ),
}

_STRING_LIMITS: dict[tuple[str, str], int] = {
    ("asr", "mode"): 32,
    ("asr", "local_model_id"): 256,
    ("asr", "model_source"): 64,
    ("asr", "api_base"): 2048,
    ("asr", "model"): 256,
    ("ai", "provider_id"): 128,
    ("ai", "api_base"): 2048,
    ("ai", "model"): 256,
    ("project", "name"): 120,
    ("project", "source_directory"): 4096,
    ("project", "trigger_mode"): 32,
    ("project", "schedule_mode"): 32,
    ("project", "daily_time"): 5,
    ("project", "output_directory"): 4096,
}


class FirstRunStateError(ValueError):
    """Raised when a first-run state transition violates the durable contract."""


@dataclass(frozen=True)
class FirstRunSession:
    session_id: str
    state: str
    current_step: str
    revision: int
    draft: dict[str, Any]
    project_request_id: str | None
    project_request_hash: str | None
    first_project_id: str | None
    failure_code: str | None
    failure_summary: str | None
    started_at: str
    updated_at: str
    paused_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class StartupDetection:
    has_legacy_evidence: bool = False
    evidence_codes: tuple[str, ...] = ()
    has_project_database: bool = False
    data_mode: str = "absent"
    project_count: int = 0
    has_first_run_session: bool = False


@dataclass(frozen=True)
class StartupDecision:
    entry: str
    onboarding: str | None = None
    reason_code: str | None = None


def _reject_non_persistable_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _NON_PERSISTABLE_KEY.search(str(key)):
                raise ValueError("draft field is not persistable")
            _reject_non_persistable_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_persistable_keys(item)


def _validated_string(section: str, field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{section}.{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{section}.{field} must not be empty")
    if len(normalized) > _STRING_LIMITS[(section, field)]:
        raise ValueError(f"{section}.{field} is too long")
    if "\x00" in normalized:
        raise ValueError(f"{section}.{field} contains invalid characters")
    return normalized


def normalize_first_run_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("draft must be an object")
    _reject_non_persistable_keys(value)
    assert_secret_free(value)
    normalized: dict[str, Any] = {}
    for section, fields in value.items():
        section_name = str(section)
        if section_name not in _ALLOWED_FIELDS:
            raise ValueError(f"unknown draft section: {section_name}")
        if not isinstance(fields, Mapping):
            raise ValueError(f"{section_name} must be an object")
        section_value: dict[str, Any] = {}
        for field, raw_value in fields.items():
            field_name = str(field)
            if field_name not in _ALLOWED_FIELDS[section_name]:
                raise ValueError(f"unknown draft field: {section_name}.{field_name}")
            if (section_name, field_name) in _STRING_LIMITS:
                item: Any = _validated_string(section_name, field_name, raw_value)
            elif (section_name, field_name) == ("project", "interval_minutes"):
                if not isinstance(raw_value, int) or isinstance(raw_value, bool) or not 1 <= raw_value <= 10080:
                    raise ValueError("project.interval_minutes must be an integer between 1 and 10080")
                item = raw_value
            else:  # pragma: no cover - every allowed field is specified above
                raise ValueError(f"unsupported draft field: {section_name}.{field_name}")
            section_value[field_name] = item

        mode = section_value.get("mode")
        if section_name == "asr" and mode is not None and mode not in {"local", "cloud"}:
            raise ValueError("asr.mode must be local or cloud")
        source = section_value.get("model_source")
        if section_name == "asr" and source is not None and source not in {"modelscope", "huggingface"}:
            raise ValueError("asr.model_source is unsupported")
        trigger = section_value.get("trigger_mode")
        if section_name == "project" and trigger is not None and trigger not in {"manual", "scheduled"}:
            raise ValueError("project.trigger_mode must be manual or scheduled")
        schedule = section_value.get("schedule_mode")
        if section_name == "project" and schedule is not None and schedule not in {"daily", "interval"}:
            raise ValueError("project.schedule_mode must be daily or interval")
        daily_time = section_value.get("daily_time")
        if section_name == "project" and daily_time is not None and not _DAILY_TIME.fullmatch(daily_time):
            raise ValueError("project.daily_time must be HH:MM")
        if section_value:
            normalized[section_name] = section_value
    return normalized


def merge_first_run_draft(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    normalized_current = normalize_first_run_draft(current)
    normalized_patch = normalize_first_run_draft(patch)
    merged = {section: dict(fields) for section, fields in normalized_current.items()}
    for section, fields in normalized_patch.items():
        merged.setdefault(section, {}).update(fields)
    return normalize_first_run_draft(merged)


def decide_startup(
    detection: StartupDetection,
    *,
    session: FirstRunSession | None,
    existing_project_ids: Collection[str],
) -> StartupDecision:
    project_ids = frozenset(existing_project_ids)
    evidence_codes = frozenset(detection.evidence_codes)
    has_new_facts = detection.project_count > 0 or session is not None

    if detection.project_count != len(project_ids):
        return StartupDecision("diagnostic_required", reason_code="project_count_mismatch")
    if detection.has_first_run_session != (session is not None):
        return StartupDecision("diagnostic_required", reason_code="first_run_session_mismatch")
    if session is not None:
        request_pair_matches = (session.project_request_id is None) == (session.project_request_hash is None)
        state_shape_matches = (
            session.session_id == FIRST_RUN_SESSION_ID
            and session.state in FIRST_RUN_STATES
            and session.current_step in FIRST_RUN_STEPS
            and session.revision >= 1
            and request_pair_matches
            and (session.state == "paused") == (session.paused_at is not None)
            and (session.state == "completed") == (session.completed_at is not None)
            and (
                session.state not in {"activation_pending", "completed"}
                or (
                    session.project_request_id is not None
                    and session.first_project_id is not None
                    and session.current_step == "complete"
                )
            )
            and (session.state in {"activation_pending", "completed"} or session.first_project_id is None)
        )
        if not state_shape_matches:
            return StartupDecision("diagnostic_required", reason_code="invalid_first_run_session")

    if detection.has_legacy_evidence:
        if evidence_codes & {"project_database_unreadable", "legacy_config_unreadable"}:
            return StartupDecision("diagnostic_required", reason_code="unreadable_startup_evidence")
        if has_new_facts:
            return StartupDecision("diagnostic_required", reason_code="legacy_projects_conflict")
        if evidence_codes <= {"legacy_data_mode"}:
            return StartupDecision("diagnostic_required", reason_code="unexplained_legacy_mode")
        return StartupDecision("migration_required", reason_code="legacy_evidence")
    if detection.data_mode == "legacy":
        return StartupDecision("diagnostic_required", reason_code="unexplained_legacy_mode")
    if detection.data_mode not in {"absent", "projects"}:
        return StartupDecision("diagnostic_required", reason_code="invalid_data_mode")
    if detection.has_project_database and detection.data_mode == "absent":
        return StartupDecision("diagnostic_required", reason_code="project_database_mode_missing")

    if session is None:
        if detection.project_count > 0 or project_ids:
            return StartupDecision("workbench")
        return StartupDecision("onboarding", onboarding="new")

    if session.state == "completed":
        if session.first_project_id and session.first_project_id in project_ids:
            return StartupDecision("workbench")
        return StartupDecision("diagnostic_required", reason_code="completed_project_missing")
    if session.state == "activation_pending":
        if session.first_project_id and session.first_project_id in project_ids:
            return StartupDecision("onboarding", onboarding="activation_pending")
        return StartupDecision("diagnostic_required", reason_code="activation_project_missing")
    if session.state == "in_progress":
        return StartupDecision("onboarding", onboarding="resume")
    if session.state == "paused":
        return StartupDecision("onboarding", onboarding="paused")
    return StartupDecision("diagnostic_required", reason_code="invalid_first_run_state")
