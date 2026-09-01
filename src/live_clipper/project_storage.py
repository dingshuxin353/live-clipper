from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .first_run_state import (
    FIRST_RUN_SESSION_ID,
    FirstRunSession,
    FirstRunStateError,
    merge_first_run_draft,
    normalize_first_run_draft,
)
from .project_domain import (
    NormalRunCreationResult,
    Project,
    ProjectConfigRevision,
    ProjectRuntime,
    Run,
    RunStageEvent,
    ScanEvent,
    WorkspaceEvent,
    legacy_id,
    new_id,
    normalize_utc,
    parse_json,
    stable_json,
    validate_project_config,
)
from .project_projection import project_result_projection
from .project_result_domain import (
    AIReviewSession,
    CandidateDecision,
    Issue,
    IssueEvent,
    OutputMaterial,
    RecoveryAttempt,
    RequestConflictError,
    RevisionConflictError,
    RunOutput,
    RunResult,
    sanitize_persisted_text,
    validate_public_identifier,
    validate_relative_reference,
    validate_remove_ranges,
    validate_sha256,
    validate_titles,
)

SCHEMA_VERSION = 4

MIGRATION_STATES = frozenset(
    {
        "backing_up",
        "migrating",
        "validating",
        "completed_ready",
        "completed_attention",
        "failed_rolled_back",
        "diagnostic_required",
    }
)
_MIGRATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "backing_up": frozenset({"backing_up", "migrating", "failed_rolled_back", "diagnostic_required"}),
    "migrating": frozenset({"migrating", "validating", "failed_rolled_back", "diagnostic_required"}),
    "validating": frozenset(
        {"validating", "completed_ready", "completed_attention", "failed_rolled_back", "diagnostic_required"}
    ),
    "failed_rolled_back": frozenset({"backing_up", "diagnostic_required"}),
    "completed_ready": frozenset(),
    "completed_attention": frozenset(),
    "diagnostic_required": frozenset(),
}


class MigrationSchemaError(RuntimeError):
    """Stable fail-closed error for ambiguous pre-v4 migration state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MigrationStateError(ValueError):
    """Raised when a durable migration transition violates the frozen contract."""


@dataclass(frozen=True)
class MigrationSession:
    migration_id: str
    source_fingerprint: str
    plan_version: int
    plan_hash: str
    source_manifest: list[dict[str, Any]]
    choices: dict[str, Any]
    state: str
    stage: str | None
    revision: int
    request_id: str | None
    request_hash: str | None
    project_id: str | None
    backup_path: str | None
    backup_status: str
    failure_code: str | None
    failure_summary: str | None
    report: dict[str, Any] | None
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    acknowledged_at: str | None


def database_path(service_dir: str | Path) -> Path:
    return Path(service_dir).expanduser().resolve() / "venus.sqlite3"


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (key <> 'data_mode' OR value IN ('legacy', 'projects'))
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    activation_state TEXT NOT NULL CHECK (activation_state IN ('inactive', 'active', 'paused')),
    current_config_revision INTEGER NOT NULL CHECK (current_config_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    activated_at TEXT,
    paused_at TEXT,
    FOREIGN KEY (project_id, current_config_revision)
      REFERENCES project_config_revisions(project_id, revision)
      ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS project_config_revisions (
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    config_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, revision),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER IF NOT EXISTS project_config_revisions_immutable_update
BEFORE UPDATE ON project_config_revisions BEGIN
    SELECT RAISE(ABORT, 'project config revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS project_config_revisions_immutable_delete
BEFORE DELETE ON project_config_revisions BEGIN
    SELECT RAISE(ABORT, 'project config revisions are immutable');
END;
CREATE TABLE IF NOT EXISTS project_runtime (
    project_id TEXT PRIMARY KEY,
    readiness_state TEXT NOT NULL CHECK (readiness_state IN ('ready', 'blocked')),
    auto_scan_state TEXT NOT NULL CHECK (auto_scan_state IN ('off', 'scheduled', 'scanning', 'paused', 'blocked')),
    last_scan_at TEXT,
    next_scan_at TEXT,
    failure_code TEXT,
    failure_summary TEXT,
    discovery_baseline TEXT,
    first_scan_state TEXT NOT NULL CHECK (first_scan_state IN ('not_required', 'pending', 'completed')),
    schedule_cursor TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS scan_events (
    scan_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    trigger_source TEXT NOT NULL CHECK (trigger_source IN ('manual', 'scheduled')),
    recovery_scan INTEGER NOT NULL DEFAULT 0 CHECK (recovery_scan IN (0, 1)),
    scheduled_at TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    matched_count INTEGER NOT NULL DEFAULT 0 CHECK (matched_count >= 0),
    created_count INTEGER NOT NULL DEFAULT 0 CHECK (created_count >= 0),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    unstable_count INTEGER NOT NULL DEFAULT 0 CHECK (unstable_count >= 0),
    unsupported_count INTEGER NOT NULL DEFAULT 0 CHECK (unsupported_count >= 0),
    excluded_count INTEGER NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    error_summary TEXT,
    UNIQUE (scan_id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_scan_per_project
ON scan_events(project_id) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    processing_sequence INTEGER NOT NULL CHECK (processing_sequence >= 1),
    origin_run_id TEXT,
    source_scan_id TEXT,
    trigger_source TEXT NOT NULL CHECK (trigger_source IN ('manual', 'scheduled', 'legacy_import')),
    first_seen_path TEXT NOT NULL,
    latest_seen_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'awaiting_review', 'failed', 'completed')),
    current_stage TEXT CHECK (current_stage IS NULL OR current_stage IN
      ('read_source', 'transcribe', 'analyze', 'arbitrate', 'review', 'render')),
    config_revision INTEGER NOT NULL CHECK (config_revision >= 1),
    parameter_snapshot_json TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    review_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    UNIQUE (project_id, content_id, processing_sequence),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, config_revision)
      REFERENCES project_config_revisions(project_id, revision) ON DELETE RESTRICT,
    FOREIGN KEY (source_scan_id, project_id)
      REFERENCES scan_events(scan_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (origin_run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS runs_queue_order ON runs(status, queued_at, run_id);
CREATE TABLE IF NOT EXISTS run_stage_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('read_source', 'transcribe', 'analyze', 'arbitrate', 'review', 'render')),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS workspace_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT,
    run_id TEXT,
    scan_id TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (scan_id) REFERENCES scan_events(scan_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS workspace_views (
    view_id TEXT PRIMARY KEY,
    last_seen_event_id INTEGER NOT NULL DEFAULT 0 CHECK (last_seen_event_id >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, request_id)
);
CREATE TABLE IF NOT EXISTS legacy_imports (
    import_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE,
    plan_json TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    summary_json TEXT NOT NULL,
    failure_summary TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


SCHEMA_V2 = """
SELECT migration_fault('before_config_rebuild');
DROP TRIGGER IF EXISTS project_config_revisions_immutable_update;
DROP TRIGGER IF EXISTS project_config_revisions_immutable_delete;
CREATE TABLE project_config_revisions_v2 (
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    config_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, revision),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
INSERT INTO project_config_revisions_v2
SELECT project_id, revision, config_json, schema_version, created_at FROM project_config_revisions;
DROP TABLE project_config_revisions;
ALTER TABLE project_config_revisions_v2 RENAME TO project_config_revisions;
CREATE TRIGGER project_config_revisions_immutable_update
BEFORE UPDATE ON project_config_revisions BEGIN
    SELECT RAISE(ABORT, 'project config revisions are immutable');
END;
CREATE TRIGGER project_config_revisions_immutable_delete
BEFORE DELETE ON project_config_revisions BEGIN
    SELECT RAISE(ABORT, 'project config revisions are immutable');
END;
CREATE UNIQUE INDEX IF NOT EXISTS runs_identity_with_project ON runs(run_id, project_id);

SELECT migration_fault('before_result_tables');
CREATE TABLE ai_review_sessions (
    review_session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('running', 'selected', 'no_clip', 'failed', 'invalid')),
    resource_ref TEXT NOT NULL,
    model_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_revision INTEGER NOT NULL CHECK (config_revision >= 1),
    parameter_snapshot_json TEXT NOT NULL,
    format_version INTEGER NOT NULL CHECK (format_version >= 1),
    overall_summary TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    selected_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    evidence_relative_path TEXT,
    evidence_sha256 TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    validated_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, attempt_number),
    UNIQUE (review_session_id, run_id),
    CHECK (candidate_count = selected_count + rejected_count),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
);
CREATE INDEX ai_review_by_run ON ai_review_sessions(run_id, attempt_number);
CREATE UNIQUE INDEX one_running_review_per_run ON ai_review_sessions(run_id) WHERE status = 'running';

CREATE TABLE run_outputs (
    output_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    review_session_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    display_order INTEGER NOT NULL CHECK (display_order >= 1),
    status TEXT NOT NULL CHECK (status IN ('pending', 'rendering', 'ready', 'failed', 'missing', 'unreadable')),
    storage_kind TEXT NOT NULL CHECK (storage_kind IN ('project_output', 'run_workspace_compat')),
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    width INTEGER CHECK (width IS NULL OR width >= 0),
    height INTEGER CHECK (height IS NULL OR height >= 0),
    container TEXT,
    video_codec TEXT,
    byte_size INTEGER CHECK (byte_size IS NULL OR byte_size >= 0),
    generated_at TEXT,
    verified_at TEXT,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    UNIQUE (run_id, candidate_id),
    UNIQUE (run_id, display_order),
    UNIQUE (output_id, run_id),
    UNIQUE (output_id, run_id, candidate_id),
    CHECK (status <> 'ready' OR (
      duration_ms IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
      AND container IS NOT NULL AND video_codec IS NOT NULL AND byte_size IS NOT NULL
      AND generated_at IS NOT NULL AND verified_at IS NOT NULL
    )),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (review_session_id, run_id)
      REFERENCES ai_review_sessions(review_session_id, run_id) ON DELETE RESTRICT
);
CREATE INDEX run_outputs_order ON run_outputs(run_id, display_order);
CREATE INDEX run_outputs_status_updated ON run_outputs(status, updated_at);

CREATE TABLE candidate_decisions (
    decision_id TEXT PRIMARY KEY,
    review_session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('selected', 'rejected')),
    rank INTEGER NOT NULL CHECK (rank >= 1),
    candidate_type TEXT NOT NULL,
    source_start_ms INTEGER NOT NULL CHECK (source_start_ms >= 0),
    source_end_ms INTEGER NOT NULL CHECK (source_end_ms > source_start_ms),
    selected_start_ms INTEGER,
    selected_end_ms INTEGER,
    remove_ranges_json TEXT NOT NULL DEFAULT '[]',
    hook TEXT NOT NULL DEFAULT '',
    core_value TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    rejection_reason_code TEXT,
    risks_json TEXT NOT NULL DEFAULT '[]',
    transcript_excerpt TEXT NOT NULL DEFAULT '',
    output_id TEXT,
    internal_sort_value REAL,
    UNIQUE (review_session_id, candidate_id),
    CHECK (
      (decision = 'selected' AND output_id IS NOT NULL
       AND selected_start_ms IS NOT NULL AND selected_end_ms IS NOT NULL
       AND selected_end_ms > selected_start_ms
       AND selected_start_ms >= source_start_ms AND selected_end_ms <= source_end_ms)
      OR
      (decision = 'rejected' AND output_id IS NULL
       AND selected_start_ms IS NULL AND selected_end_ms IS NULL)
    ),
    CHECK (typeof(source_start_ms) = 'integer' AND typeof(source_end_ms) = 'integer'),
    CHECK (selected_start_ms IS NULL OR typeof(selected_start_ms) = 'integer'),
    CHECK (selected_end_ms IS NULL OR typeof(selected_end_ms) = 'integer'),
    FOREIGN KEY (review_session_id, run_id)
      REFERENCES ai_review_sessions(review_session_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (output_id, run_id, candidate_id)
      REFERENCES run_outputs(output_id, run_id, candidate_id) ON DELETE RESTRICT
);

CREATE TABLE run_results (
    run_id TEXT PRIMARY KEY,
    review_session_id TEXT NOT NULL,
    result_type TEXT NOT NULL CHECK (result_type IN ('clips_ready', 'no_clip', 'partial', 'unavailable')),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    available_output_count INTEGER NOT NULL CHECK (available_output_count >= 0),
    failed_output_count INTEGER NOT NULL CHECK (failed_output_count >= 0),
    total_duration_ms INTEGER NOT NULL CHECK (total_duration_ms >= 0),
    overall_summary TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    format_version INTEGER NOT NULL CHECK (format_version >= 1),
    result_revision INTEGER NOT NULL CHECK (result_revision >= 1),
    seen_result_revision INTEGER CHECK (seen_result_revision IS NULL OR seen_result_revision >= 1),
    result_seen_at TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('native_v2', 'indexed_v1')),
    evidence_hash TEXT,
    completed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (candidate_count = selected_count + rejected_count),
    CHECK (seen_result_revision IS NULL OR seen_result_revision <= result_revision),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (review_session_id, run_id)
      REFERENCES ai_review_sessions(review_session_id, run_id) ON DELETE RESTRICT
);
CREATE INDEX run_results_completed ON run_results(completed_at DESC, run_id DESC);
CREATE INDEX run_results_unseen ON run_results(completed_at DESC, run_id DESC)
WHERE seen_result_revision IS NULL OR seen_result_revision < result_revision;

CREATE TABLE output_materials (
    material_id TEXT PRIMARY KEY,
    output_id TEXT NOT NULL UNIQUE,
    title_candidates_json TEXT NOT NULL,
    preferred_title_id TEXT,
    description TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    generation_source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    material_revision INTEGER NOT NULL CHECK (material_revision >= 1),
    created_at TEXT NOT NULL,
    saved_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (material_id, output_id),
    FOREIGN KEY (output_id) REFERENCES run_outputs(output_id) ON DELETE RESTRICT
);

CREATE TABLE issues (
    issue_id TEXT PRIMARY KEY,
    issue_code TEXT NOT NULL,
    category TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'run', 'output', 'material')),
    project_id TEXT NOT NULL,
    run_id TEXT,
    output_id TEXT,
    material_id TEXT,
    issue_group_key TEXT NOT NULL,
    root_cause_ref TEXT,
    status TEXT NOT NULL CHECK (status IN
      ('retrying', 'action_required', 'checking', 'ready_to_recover', 'recovering', 'resolved')),
    impact_level TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    impact TEXT NOT NULL,
    preserved_content TEXT NOT NULL,
    next_step TEXT NOT NULL,
    recovery_capability TEXT NOT NULL,
    safe_checkpoint TEXT,
    reuse_stages_json TEXT NOT NULL DEFAULT '[]',
    redo_stages_json TEXT NOT NULL DEFAULT '[]',
    operational_overrides_json TEXT NOT NULL DEFAULT '{}',
    automatic_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (automatic_attempt_count >= 0),
    total_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (total_attempt_count >= 0),
    next_retry_at TEXT,
    retry_exhausted INTEGER NOT NULL DEFAULT 0 CHECK (retry_exhausted IN (0, 1)),
    diagnostic_id TEXT,
    diagnostic_summary TEXT,
    log_relative_path TEXT,
    issue_revision INTEGER NOT NULL DEFAULT 1 CHECK (issue_revision >= 1),
    occurred_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (
      (scope_type = 'project' AND run_id IS NULL AND output_id IS NULL AND material_id IS NULL)
      OR (scope_type = 'run' AND run_id IS NOT NULL AND output_id IS NULL AND material_id IS NULL)
      OR (scope_type = 'output' AND run_id IS NOT NULL AND output_id IS NOT NULL AND material_id IS NULL)
      OR (scope_type = 'material' AND run_id IS NOT NULL AND output_id IS NOT NULL AND material_id IS NOT NULL)
    ),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, project_id) REFERENCES runs(run_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (output_id, run_id) REFERENCES run_outputs(output_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (material_id, output_id) REFERENCES output_materials(material_id, output_id) ON DELETE RESTRICT
);
CREATE INDEX issues_status_updated ON issues(status, updated_at);
CREATE INDEX issues_group_status ON issues(issue_group_key, status);
CREATE UNIQUE INDEX one_active_issue_per_cause ON issues(
    scope_type, project_id, ifnull(run_id, ''), ifnull(output_id, ''), ifnull(material_id, ''),
    issue_code, issue_group_key
) WHERE status <> 'resolved';

CREATE TABLE issue_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE RESTRICT
);
CREATE TRIGGER issue_events_append_only_update BEFORE UPDATE ON issue_events BEGIN
    SELECT RAISE(ABORT, 'issue events are append only');
END;
CREATE TRIGGER issue_events_append_only_delete BEFORE DELETE ON issue_events BEGIN
    SELECT RAISE(ABORT, 'issue events are append only');
END;

CREATE TABLE recovery_attempts (
    attempt_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    attempt_type TEXT NOT NULL CHECK (attempt_type IN
      ('continue_run', 'retry_output', 'retry_material', 'operational_repair')),
    run_id TEXT,
    output_id TEXT,
    material_id TEXT,
    requested_by TEXT NOT NULL,
    reuse_stages_json TEXT NOT NULL DEFAULT '[]',
    redo_stages_json TEXT NOT NULL DEFAULT '[]',
    operational_overrides_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('requested', 'accepted', 'running', 'completed', 'failed')),
    requested_at TEXT NOT NULL,
    accepted_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (issue_id, request_id),
    CHECK (
      (attempt_type = 'continue_run' AND run_id IS NOT NULL AND output_id IS NULL AND material_id IS NULL)
      OR (attempt_type = 'retry_output' AND run_id IS NOT NULL AND output_id IS NOT NULL AND material_id IS NULL)
      OR (attempt_type = 'retry_material' AND run_id IS NOT NULL AND output_id IS NOT NULL AND material_id IS NOT NULL)
      OR (attempt_type = 'operational_repair')
    ),
    FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (output_id) REFERENCES run_outputs(output_id) ON DELETE RESTRICT,
    FOREIGN KEY (material_id) REFERENCES output_materials(material_id) ON DELETE RESTRICT
);
CREATE INDEX recovery_attempts_issue_requested ON recovery_attempts(issue_id, requested_at);
SELECT migration_fault('before_version_record');
"""


SCHEMA_V3 = """
SELECT migration_fault('before_first_run_table');
CREATE TABLE first_run_sessions (
    session_id TEXT PRIMARY KEY CHECK (session_id = 'primary'),
    state TEXT NOT NULL CHECK (state IN ('in_progress', 'paused', 'activation_pending', 'completed')),
    current_step TEXT NOT NULL CHECK (current_step IN ('welcome', 'asr', 'ai', 'project', 'complete')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    draft_json TEXT NOT NULL,
    project_request_id TEXT,
    project_request_hash TEXT,
    first_project_id TEXT,
    failure_code TEXT,
    failure_summary TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paused_at TEXT,
    completed_at TEXT,
    CHECK ((project_request_id IS NULL) = (project_request_hash IS NULL)),
    CHECK (project_request_hash IS NULL OR (
      length(project_request_hash) = 64 AND project_request_hash NOT GLOB '*[^0-9a-f]*'
    )),
    CHECK (state <> 'paused' OR paused_at IS NOT NULL),
    CHECK (state = 'paused' OR paused_at IS NULL),
    CHECK (state <> 'activation_pending' OR (
      project_request_id IS NOT NULL AND first_project_id IS NOT NULL AND current_step = 'complete'
    )),
    CHECK (state <> 'completed' OR (
      project_request_id IS NOT NULL AND first_project_id IS NOT NULL
      AND current_step = 'complete' AND completed_at IS NOT NULL
    )),
    CHECK (state = 'completed' OR completed_at IS NULL)
);
CREATE INDEX first_run_sessions_state ON first_run_sessions(state, updated_at);
SELECT migration_fault('after_first_run_table');
"""


SCHEMA_V4 = """
SELECT migration_fault('before_migration_sessions_table');
CREATE TABLE migration_sessions (
    migration_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE CHECK (
      length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    plan_hash TEXT NOT NULL CHECK (
      length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_manifest_json TEXT NOT NULL,
    choices_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
      'backing_up', 'migrating', 'validating', 'completed_ready',
      'completed_attention', 'failed_rolled_back', 'diagnostic_required'
    )),
    stage TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    request_id TEXT UNIQUE,
    request_hash TEXT,
    project_id TEXT,
    backup_path TEXT,
    backup_status TEXT NOT NULL CHECK (backup_status IN ('pending', 'completed', 'failed')),
    failure_code TEXT,
    failure_summary TEXT,
    report_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    acknowledged_at TEXT,
    CHECK ((request_id IS NULL) = (request_hash IS NULL)),
    CHECK (request_hash IS NULL OR (
      length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'
    )),
    CHECK (state NOT IN ('completed_ready', 'completed_attention') OR (
      project_id IS NOT NULL AND backup_path IS NOT NULL AND backup_status = 'completed'
      AND report_json IS NOT NULL AND completed_at IS NOT NULL
    )),
    CHECK (state != 'failed_rolled_back' OR (
      failure_code IS NOT NULL AND failure_summary IS NOT NULL
    )),
    CHECK (acknowledged_at IS NULL OR state IN ('completed_ready', 'completed_attention')),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);
CREATE INDEX migration_sessions_state_updated ON migration_sessions(state, updated_at);
SELECT migration_fault('after_migration_sessions_table');
DROP TABLE legacy_imports;
SELECT migration_fault('after_legacy_imports_drop');
"""


def connect_database(service_dir: str | Path) -> sqlite3.Connection:
    path = database_path(service_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        initialize_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def initialize_schema(
    connection: sqlite3.Connection,
    *,
    migration_fault: Callable[[str], None] | None = None,
) -> None:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    versions = (
        sorted(int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations"))
        if table_exists
        else []
    )
    if any(version > SCHEMA_VERSION for version in versions):
        raise RuntimeError("database schema is newer than this application")
    if versions and versions != list(range(1, max(versions) + 1)):
        raise RuntimeError("database schema history is incomplete")
    if SCHEMA_VERSION in versions:
        return
    if 3 in versions and 4 not in versions:
        legacy_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_imports'"
        ).fetchone()
        if legacy_table is not None and connection.execute("SELECT 1 FROM legacy_imports LIMIT 1").fetchone():
            raise MigrationSchemaError("legacy_import_state_requires_diagnostic")

    def inject(phase: str) -> int:
        if migration_fault is not None:
            migration_fault(phase)
        return 0

    connection.create_function("migration_fault", 1, inject)
    connection.execute("PRAGMA foreign_keys = OFF")
    statements = ["BEGIN IMMEDIATE;"]
    if 1 not in versions:
        statements.extend(
            [
                SCHEMA_V1,
                """INSERT INTO schema_migrations(version, name, applied_at)
VALUES (1, 'project foundation v1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));""",
                """INSERT INTO system_state(key, value, updated_at)
VALUES ('data_mode', 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));""",
            ]
        )
    if 2 not in versions:
        statements.extend(
            [
                SCHEMA_V2,
                """INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, 'result and issue foundation v2', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));""",
            ]
        )
    if 3 not in versions:
        statements.extend(
            [
                SCHEMA_V3,
                """INSERT INTO schema_migrations(version, name, applied_at)
VALUES (3, 'first-run state foundation v3', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));""",
                "SELECT migration_fault('after_first_run_version');",
            ]
        )
    if 4 not in versions:
        statements.extend(
            [
                SCHEMA_V4,
                """INSERT INTO schema_migrations(version, name, applied_at)
VALUES (4, 'migration state and plan foundation v4', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));""",
                "SELECT migration_fault('after_migration_version');",
            ]
        )
    statements.append("COMMIT;")
    try:
        connection.executescript("\n".join(statements))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function("migration_fault", 1, None)
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"schema migration left foreign key violations: {violations!r}")


def _dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    rows = _dicts(cursor)
    return rows[0] if rows else None


def _safe_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if re.search(r"(?i)(?:raw[_-]?response|full[_-]?prompt|chain[_-]?of[_-]?thought|hidden[_-]?reasoning)", normalized_key):
                raise ValueError(f"non-persistable model field: {normalized_key}")
            result[normalized_key] = _safe_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_persisted_text(value)
    return value


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _first_run_session(row: Mapping[str, Any]) -> FirstRunSession:
    draft = parse_json(str(row["draft_json"]))
    if not isinstance(draft, Mapping):
        raise RuntimeError("first-run draft is not an object")
    normalized_draft = normalize_first_run_draft(draft)
    return FirstRunSession(
        session_id=str(row["session_id"]),
        state=str(row["state"]),
        current_step=str(row["current_step"]),
        revision=int(row["revision"]),
        draft=normalized_draft,
        project_request_id=str(row["project_request_id"]) if row["project_request_id"] is not None else None,
        project_request_hash=str(row["project_request_hash"]) if row["project_request_hash"] is not None else None,
        first_project_id=str(row["first_project_id"]) if row["first_project_id"] is not None else None,
        failure_code=str(row["failure_code"]) if row["failure_code"] is not None else None,
        failure_summary=str(row["failure_summary"]) if row["failure_summary"] is not None else None,
        started_at=str(row["started_at"]),
        updated_at=str(row["updated_at"]),
        paused_at=str(row["paused_at"]) if row["paused_at"] is not None else None,
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
    )


def _migration_session(row: Mapping[str, Any]) -> MigrationSession:
    manifest = parse_json(str(row["source_manifest_json"]))
    choices = parse_json(str(row["choices_json"]))
    report = parse_json(str(row["report_json"])) if row["report_json"] is not None else None
    if not isinstance(manifest, list) or not all(isinstance(item, Mapping) for item in manifest):
        raise RuntimeError("migration source manifest is invalid")
    if not isinstance(choices, Mapping) or (report is not None and not isinstance(report, Mapping)):
        raise RuntimeError("migration persisted payload is invalid")
    return MigrationSession(
        migration_id=str(row["migration_id"]),
        source_fingerprint=str(row["source_fingerprint"]),
        plan_version=int(row["plan_version"]),
        plan_hash=str(row["plan_hash"]),
        source_manifest=[dict(item) for item in manifest],
        choices=dict(choices),
        state=str(row["state"]),
        stage=str(row["stage"]) if row["stage"] is not None else None,
        revision=int(row["revision"]),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        request_hash=str(row["request_hash"]) if row["request_hash"] is not None else None,
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        backup_path=str(row["backup_path"]) if row["backup_path"] is not None else None,
        backup_status=str(row["backup_status"]),
        failure_code=str(row["failure_code"]) if row["failure_code"] is not None else None,
        failure_summary=str(row["failure_summary"]) if row["failure_summary"] is not None else None,
        report=dict(report) if report is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        updated_at=str(row["updated_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        acknowledged_at=str(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None,
    )


class ProjectRepository:
    def __init__(self, service_dir: str | Path, *, connection: sqlite3.Connection | None = None) -> None:
        self.service_dir = Path(service_dir).expanduser().resolve()
        self.database_path = database_path(self.service_dir)
        self.connection = connection or connect_database(self.service_dir)
        self._owns_connection = connection is None

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> ProjectRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def get_data_mode(self) -> str:
        row = self.connection.execute("SELECT value FROM system_state WHERE key = 'data_mode'").fetchone()
        if row is None:
            raise RuntimeError("system_state.data_mode is missing")
        return str(row[0])

    def set_data_mode(self, mode: str, *, occurred_at: str | None = None) -> None:
        if mode not in {"legacy", "projects"}:
            raise ValueError("data_mode must be legacy or projects")
        with self.transaction():
            self.connection.execute(
                "UPDATE system_state SET value = ?, updated_at = ? WHERE key = 'data_mode'",
                (mode, normalize_utc(occurred_at)),
            )

    def get_first_run_session(self) -> FirstRunSession | None:
        row = _one(
            self.connection.execute(
                "SELECT * FROM first_run_sessions WHERE session_id = ?", (FIRST_RUN_SESSION_ID,)
            )
        )
        return _first_run_session(row) if row else None

    def begin_first_run_session(self, *, started_at: str | None = None) -> FirstRunSession:
        timestamp = normalize_utc(started_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                if self.get_data_mode() != "projects":
                    raise FirstRunStateError("first-run session requires projects data mode")
                if self.connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None:
                    raise FirstRunStateError("first-run session cannot start after a project exists")
                if self.connection.execute("SELECT 1 FROM migration_sessions LIMIT 1").fetchone() is not None:
                    raise FirstRunStateError("first-run session cannot start after a migration session")
                self.connection.execute(
                    """INSERT INTO first_run_sessions(
                         session_id, state, current_step, revision, draft_json, started_at, updated_at
                       ) VALUES (?, 'in_progress', 'welcome', 1, '{}', ?, ?)""",
                    (FIRST_RUN_SESSION_ID, timestamp, timestamp),
                )
            elif not (
                current.state == "in_progress"
                and current.current_step == "welcome"
                and current.revision == 1
                and not current.draft
                and current.project_request_id is None
            ):
                raise FirstRunStateError("first-run session already exists")
        session = self.get_first_run_session()
        assert session is not None
        return session

    @staticmethod
    def _require_first_run_revision(current: FirstRunSession, expected_revision: int) -> None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        if current.revision != expected_revision:
            raise RevisionConflictError("first-run session revision conflict")

    def update_first_run_draft(
        self,
        expected_revision: int,
        patch: Mapping[str, Any],
        *,
        current_step: str | None = None,
        occurred_at: str | None = None,
    ) -> FirstRunSession:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if current.state != "in_progress":
                raise FirstRunStateError("draft can only be changed while first-run is in progress")
            target_step = current.current_step if current_step is None else str(current_step)
            if target_step not in {"welcome", "asr", "ai", "project"}:
                raise FirstRunStateError("invalid in-progress first-run step")
            merged = merge_first_run_draft(current.draft, patch)
            if current.revision != expected_revision:
                if (
                    current.revision == expected_revision + 1
                    and merged == current.draft
                    and target_step == current.current_step
                ):
                    return current
                raise RevisionConflictError("first-run session revision conflict")
            if merged == current.draft and target_step == current.current_step:
                return current
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET draft_json = ?, current_step = ?, revision = revision + 1, updated_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (stable_json(merged), target_step, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def pause_first_run(self, expected_revision: int, *, occurred_at: str | None = None) -> FirstRunSession:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if current.state == "paused" and current.revision == expected_revision + 1:
                return current
            if current.state != "in_progress":
                raise FirstRunStateError("only an in-progress first-run session can be paused")
            self._require_first_run_revision(current, expected_revision)
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET state = 'paused', revision = revision + 1, updated_at = ?, paused_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (timestamp, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def resume_first_run(self, expected_revision: int, *, occurred_at: str | None = None) -> FirstRunSession:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if current.state == "in_progress" and current.revision == expected_revision + 1:
                return current
            if current.state != "paused":
                raise FirstRunStateError("only a paused first-run session can be resumed")
            self._require_first_run_revision(current, expected_revision)
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET state = 'in_progress', revision = revision + 1, updated_at = ?, paused_at = NULL
                   WHERE session_id = ? AND revision = ?""",
                (timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def reserve_first_project_request(
        self,
        expected_revision: int,
        request_id: str,
        request_hash: str,
        *,
        occurred_at: str | None = None,
    ) -> FirstRunSession:
        normalized_id = validate_public_identifier(request_id, field="project_request_id")
        normalized_hash = validate_sha256(request_hash, field="project_request_hash")
        assert normalized_hash is not None
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if current.state != "in_progress":
                raise FirstRunStateError("project request can only be reserved while first-run is in progress")
            if current.project_request_id == normalized_id:
                if current.project_request_hash != normalized_hash:
                    raise RequestConflictError("project request identity was reused with different content")
                if current.revision in {expected_revision, expected_revision + 1}:
                    return current
                raise RevisionConflictError("first-run session revision conflict")
            self._require_first_run_revision(current, expected_revision)
            if current.project_request_id is not None:
                existing = self.get_idempotency_key("project.create", current.project_request_id)
                if existing is not None:
                    raise RequestConflictError("reserved project request already has a durable result")
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET project_request_id = ?, project_request_hash = ?, first_project_id = NULL,
                       failure_code = NULL, failure_summary = NULL,
                       revision = revision + 1, updated_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (normalized_id, normalized_hash, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def bind_first_project(
        self,
        expected_revision: int,
        request_id: str,
        project_id: str,
        *,
        occurred_at: str | None = None,
    ) -> FirstRunSession:
        normalized_request_id = validate_public_identifier(request_id, field="project_request_id")
        normalized_project_id = validate_public_identifier(project_id, field="first_project_id")
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if (
                current.state == "activation_pending"
                and current.project_request_id == normalized_request_id
                and current.first_project_id == normalized_project_id
                and current.revision == expected_revision + 1
            ):
                return current
            if current.state != "in_progress":
                raise FirstRunStateError("first project can only be bound from in-progress state")
            self._require_first_run_revision(current, expected_revision)
            if current.project_request_id != normalized_request_id or current.project_request_hash is None:
                raise RequestConflictError("project binding does not match the reserved request")
            idempotency = self.get_idempotency_key("project.create", normalized_request_id)
            if (
                idempotency is None
                or idempotency["request_hash"] != current.project_request_hash
                or idempotency["object_type"] != "project"
                or idempotency["object_id"] != normalized_project_id
                or self.get_project(normalized_project_id) is None
            ):
                raise RequestConflictError("project binding does not match a durable project.create result")
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET state = 'activation_pending', current_step = 'complete', first_project_id = ?,
                       failure_code = NULL, failure_summary = NULL,
                       revision = revision + 1, updated_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (normalized_project_id, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def record_activation_failure(
        self,
        expected_revision: int,
        code: str,
        summary: str,
        *,
        occurred_at: str | None = None,
    ) -> FirstRunSession:
        normalized_code = validate_public_identifier(code, field="failure_code")
        normalized_summary = sanitize_persisted_text(summary).strip()[:240]
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None or current.state != "activation_pending":
                raise FirstRunStateError("activation failure requires activation_pending state")
            if current.revision != expected_revision:
                if (
                    current.revision == expected_revision + 1
                    and current.failure_code == normalized_code
                    and current.failure_summary == normalized_summary
                ):
                    return current
                raise RevisionConflictError("first-run session revision conflict")
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET failure_code = ?, failure_summary = ?, revision = revision + 1, updated_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (normalized_code, normalized_summary, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def complete_first_run(
        self, expected_revision: int, *, occurred_at: str | None = None
    ) -> FirstRunSession:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_first_run_session()
            if current is None:
                raise FirstRunStateError("first-run session does not exist")
            if current.state == "completed" and current.revision == expected_revision + 1:
                return current
            if current.state != "activation_pending" or current.first_project_id is None:
                raise FirstRunStateError("only a bound activation_pending session can complete")
            self._require_first_run_revision(current, expected_revision)
            if self.get_project(current.first_project_id) is None:
                raise FirstRunStateError("bound first project is missing")
            self.connection.execute(
                """UPDATE first_run_sessions
                   SET state = 'completed', current_step = 'complete', failure_code = NULL,
                       failure_summary = NULL, revision = revision + 1,
                       updated_at = ?, completed_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (timestamp, timestamp, FIRST_RUN_SESSION_ID, expected_revision),
            )
        session = self.get_first_run_session()
        assert session is not None
        return session

    def create_project(
        self,
        name: str,
        config: Mapping[str, Any],
        *,
        description: str = "",
        activation_state: str = "inactive",
        project_id: str | None = None,
        created_at: str | None = None,
    ) -> Project:
        if activation_state not in {"inactive", "active", "paused"}:
            raise ValueError("invalid activation_state")
        validated = validate_project_config(config)
        schema_version = int(validated["schema_version"])
        project_id = project_id or new_id()
        timestamp = normalize_utc(created_at)
        activated_at = timestamp if activation_state == "active" else None
        paused_at = timestamp if activation_state == "paused" else None
        with self.transaction():
            self.connection.execute(
                """INSERT INTO projects(
                     project_id, name, description, activation_state, current_config_revision,
                     created_at, updated_at, activated_at, paused_at
                   ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (project_id, name, description, activation_state, timestamp, timestamp, activated_at, paused_at),
            )
            self.connection.execute(
                """INSERT INTO project_config_revisions(
                     project_id, revision, config_json, schema_version, created_at
                   ) VALUES (?, 1, ?, ?, ?)""",
                (project_id, stable_json(validated), schema_version, timestamp),
            )
            self.connection.execute(
                """INSERT INTO project_runtime(
                     project_id, readiness_state, auto_scan_state, first_scan_state
                   ) VALUES (?, 'ready', 'off', 'pending')""",
                (project_id,),
            )
        project = self.get_project(project_id)
        assert project is not None
        return project

    def create_project_bundle(
        self,
        name: str,
        config: Mapping[str, Any],
        *,
        description: str = "",
        activation_state: str = "inactive",
        project_id: str | None = None,
        created_at: str | None = None,
        runtime: Mapping[str, Any] | None = None,
        event_payload: Mapping[str, Any] | None = None,
        idempotency: Mapping[str, str] | None = None,
    ) -> Project:
        """Create a project and all durable side effects in one transaction.

        M1 uses this path so a failure in runtime/event/idempotency persistence
        cannot leave a project that the first-run session cannot recover.
        """
        if activation_state not in {"inactive", "active", "paused"}:
            raise ValueError("invalid activation_state")
        validated = validate_project_config(config)
        schema_version = int(validated["schema_version"])
        project_id = project_id or new_id()
        timestamp = normalize_utc(created_at)
        activated_at = timestamp if activation_state == "active" else None
        paused_at = timestamp if activation_state == "paused" else None
        runtime_values = {
            "readiness_state": "ready",
            "auto_scan_state": "off",
            "first_scan_state": "pending",
            **dict(runtime or {}),
        }
        allowed_runtime = {
            "readiness_state",
            "auto_scan_state",
            "last_scan_at",
            "next_scan_at",
            "failure_code",
            "failure_summary",
            "discovery_baseline",
            "first_scan_state",
            "schedule_cursor",
        }
        if set(runtime_values) - allowed_runtime:
            raise ValueError("runtime contains unsupported fields")
        if idempotency is not None:
            required_idempotency = {"scope", "request_id", "request_hash"}
            if not required_idempotency <= set(idempotency):
                raise ValueError("idempotency is missing required fields")
            if idempotency.get("object_type", "project") != "project":
                raise ValueError("project idempotency object_type must be project")
        event_payload_json = stable_json(event_payload or {})
        with self.transaction():
            if idempotency is not None:
                existing = _one(
                    self.connection.execute(
                        "SELECT * FROM idempotency_keys WHERE scope = ? AND request_id = ?",
                        (idempotency["scope"], idempotency["request_id"]),
                    )
                )
                if existing is not None:
                    if existing["request_hash"] != idempotency["request_hash"]:
                        raise RequestConflictError("project request identity was reused with different content")
                    existing_project = self.get_project(str(existing["object_id"]))
                    if existing_project is None:
                        raise RuntimeError("project idempotency record points to a missing project")
                    return existing_project
            self.connection.execute(
                """INSERT INTO projects(
                     project_id, name, description, activation_state, current_config_revision,
                     created_at, updated_at, activated_at, paused_at
                   ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (project_id, name, description, activation_state, timestamp, timestamp, activated_at, paused_at),
            )
            self.connection.execute(
                """INSERT INTO project_config_revisions(
                     project_id, revision, config_json, schema_version, created_at
                   ) VALUES (?, 1, ?, ?, ?)""",
                (project_id, stable_json(validated), schema_version, timestamp),
            )
            runtime_columns = ["project_id", *runtime_values.keys()]
            runtime_values_sql = [project_id, *runtime_values.values()]
            placeholders = ", ".join("?" for _ in runtime_columns)
            self.connection.execute(
                f"INSERT INTO project_runtime({', '.join(runtime_columns)}) VALUES ({placeholders})",  # noqa: S608
                runtime_values_sql,
            )
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, occurred_at, payload_json)
                   VALUES ('project_created', ?, ?, ?)""",
                (project_id, timestamp, event_payload_json),
            )
            if idempotency is not None:
                self.connection.execute(
                    """INSERT INTO idempotency_keys(
                         scope, request_id, request_hash, object_type, object_id, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        idempotency["scope"],
                        idempotency["request_id"],
                        idempotency["request_hash"],
                        idempotency.get("object_type", "project"),
                        project_id,
                        timestamp,
                    ),
                )
        project = self.get_project(project_id)
        assert project is not None
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = _one(self.connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)))
        return Project(**row) if row else None

    def list_projects(self) -> list[Project]:
        return [Project(**row) for row in _dicts(self.connection.execute("SELECT * FROM projects ORDER BY created_at, project_id"))]

    def update_project_identity(
        self,
        project_id: str,
        *,
        name: str,
        description: str,
        updated_at: str | None = None,
    ) -> Project:
        timestamp = normalize_utc(updated_at)
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE projects SET name = ?, description = ?, updated_at = ? WHERE project_id = ?",
                (name, description, timestamp, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        project = self.get_project(project_id)
        assert project is not None
        return project

    def update_project_activation(
        self,
        project_id: str,
        activation_state: str,
        *,
        occurred_at: str | None = None,
    ) -> Project:
        if activation_state not in {"inactive", "active", "paused"}:
            raise ValueError("invalid activation_state")
        timestamp = normalize_utc(occurred_at)
        activated_at = timestamp if activation_state == "active" else None
        paused_at = timestamp if activation_state == "paused" else None
        event_type = {"active": "project_enabled", "paused": "project_paused", "inactive": "project_deactivated"}[
            activation_state
        ]
        with self.transaction():
            cursor = self.connection.execute(
                """UPDATE projects SET activation_state = ?, updated_at = ?,
                     activated_at = CASE WHEN ? = 'active' THEN ? ELSE activated_at END,
                     paused_at = CASE WHEN ? = 'paused' THEN ? ELSE NULL END
                   WHERE project_id = ?""",
                (
                    activation_state,
                    timestamp,
                    activation_state,
                    activated_at,
                    activation_state,
                    paused_at,
                    project_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, occurred_at, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (event_type, project_id, timestamp, stable_json({"activation_state": activation_state})),
            )
        project = self.get_project(project_id)
        assert project is not None
        return project

    def get_runtime(self, project_id: str) -> ProjectRuntime | None:
        row = _one(self.connection.execute("SELECT * FROM project_runtime WHERE project_id = ?", (project_id,)))
        return ProjectRuntime(**row) if row else None

    def update_runtime(self, project_id: str, **changes: Any) -> ProjectRuntime:
        allowed = {
            "readiness_state",
            "auto_scan_state",
            "last_scan_at",
            "next_scan_at",
            "failure_code",
            "failure_summary",
            "discovery_baseline",
            "first_scan_state",
            "schedule_cursor",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("runtime update contains no fields or unsupported fields")
        for field in {"last_scan_at", "next_scan_at"} & changes.keys():
            if changes[field] is not None:
                changes[field] = normalize_utc(changes[field])
        assignments = ", ".join(f"{field} = ?" for field in changes)
        with self.transaction():
            cursor = self.connection.execute(
                f"UPDATE project_runtime SET {assignments} WHERE project_id = ?",  # noqa: S608
                (*changes.values(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        runtime = self.get_runtime(project_id)
        assert runtime is not None
        return runtime

    def get_config_revision(self, project_id: str, revision: int | None = None) -> ProjectConfigRevision | None:
        if revision is None:
            project = self.get_project(project_id)
            if project is None:
                return None
            revision = project.current_config_revision
        row = _one(
            self.connection.execute(
                "SELECT * FROM project_config_revisions WHERE project_id = ? AND revision = ?",
                (project_id, revision),
            )
        )
        if row is None:
            return None
        row["config"] = parse_json(row.pop("config_json"))
        return ProjectConfigRevision(**row)

    def add_config_revision(
        self,
        project_id: str,
        config: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        created_at: str | None = None,
    ) -> ProjectConfigRevision:
        validated = validate_project_config(config)
        schema_version = int(validated["schema_version"])
        timestamp = normalize_utc(created_at)
        with self.transaction():
            project = _one(self.connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)))
            if project is None:
                raise KeyError(project_id)
            current = int(project["current_config_revision"])
            if expected_revision is not None and current != expected_revision:
                raise ValueError("revision_conflict")
            revision = current + 1
            self.connection.execute(
                "INSERT INTO project_config_revisions VALUES (?, ?, ?, ?, ?)",
                (project_id, revision, stable_json(validated), schema_version, timestamp),
            )
            self.connection.execute(
                "UPDATE projects SET current_config_revision = ?, updated_at = ? WHERE project_id = ?",
                (revision, timestamp, project_id),
            )
        result = self.get_config_revision(project_id, revision)
        assert result is not None
        return result

    def create_scan_event(
        self,
        project_id: str,
        *,
        trigger_source: str,
        recovery_scan: bool = False,
        scheduled_at: str | None = None,
        started_at: str | None = None,
        scan_id: str | None = None,
    ) -> ScanEvent:
        scan_id = scan_id or new_id()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO scan_events(
                     scan_id, project_id, trigger_source, recovery_scan, scheduled_at, started_at, status
                   ) VALUES (?, ?, ?, ?, ?, ?, 'running')""",
                (
                    scan_id,
                    project_id,
                    trigger_source,
                    int(recovery_scan),
                    normalize_utc(scheduled_at) if scheduled_at else None,
                    normalize_utc(started_at),
                ),
            )
        result = self.get_scan_event(scan_id)
        assert result is not None
        return result

    def get_scan_event(self, scan_id: str) -> ScanEvent | None:
        row = _one(self.connection.execute("SELECT * FROM scan_events WHERE scan_id = ?", (scan_id,)))
        if row is None:
            return None
        row["recovery_scan"] = bool(row["recovery_scan"])
        return ScanEvent(**row)

    def get_running_scan(self, project_id: str) -> ScanEvent | None:
        row = _one(
            self.connection.execute(
                "SELECT * FROM scan_events WHERE project_id = ? AND status = 'running'", (project_id,)
            )
        )
        if row is None:
            return None
        row["recovery_scan"] = bool(row["recovery_scan"])
        return ScanEvent(**row)

    def list_scan_events(self, project_id: str) -> list[ScanEvent]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM scan_events WHERE project_id = ? ORDER BY started_at DESC, scan_id DESC",
                (project_id,),
            )
        )
        events = []
        for row in rows:
            row["recovery_scan"] = bool(row["recovery_scan"])
            events.append(ScanEvent(**row))
        return events

    def complete_scan_event(
        self,
        scan_id: str,
        *,
        status: str,
        counts: Mapping[str, int] | None = None,
        error_summary: str | None = None,
        completed_at: str | None = None,
    ) -> ScanEvent:
        if status not in {"success", "partial", "failed"}:
            raise ValueError("a completed scan must be success, partial, or failed")
        counts = dict(counts or {})
        fields = (
            "matched_count",
            "created_count",
            "duplicate_count",
            "unstable_count",
            "unsupported_count",
            "excluded_count",
            "failed_count",
        )
        with self.transaction():
            current = self.get_scan_event(scan_id)
            if current is None:
                raise KeyError(scan_id)
            values = [int(counts.get(field, getattr(current, field))) for field in fields]
            self.connection.execute(
                f"UPDATE scan_events SET status = ?, completed_at = ?, error_summary = ?, {', '.join(f'{field} = ?' for field in fields)} WHERE scan_id = ?",  # noqa: S608
                (status, normalize_utc(completed_at), error_summary, *values, scan_id),
            )
        result = self.get_scan_event(scan_id)
        assert result is not None
        return result

    def create_normal_run(
        self,
        *,
        project_id: str,
        content_id: str,
        trigger_source: str,
        first_seen_path: str,
        latest_seen_path: str,
        parameter_snapshot: Mapping[str, Any],
        source_scan_id: str | None = None,
        config_revision: int | None = None,
        queued_at: str | None = None,
        run_id: str | None = None,
    ) -> NormalRunCreationResult:
        if not content_id:
            raise ValueError("content_id cannot be empty")
        snapshot_json = stable_json(parameter_snapshot)
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        revision = config_revision or project.current_config_revision
        timestamp = normalize_utc(queued_at)
        run_id = run_id or new_id()
        try:
            with self.transaction():
                # Resolve these inside the write transaction so a stale caller cannot create a partial run.
                if self.get_config_revision(project_id, revision) is None:
                    raise ValueError("config revision does not belong to project")
                if source_scan_id is not None:
                    scan = self.get_scan_event(source_scan_id)
                    if scan is None or scan.project_id != project_id:
                        raise ValueError("source scan does not belong to project")
                self.connection.execute(
                    """INSERT INTO runs(
                         run_id, project_id, content_id, processing_sequence, origin_run_id,
                         source_scan_id, trigger_source, first_seen_path, latest_seen_path,
                         status, current_stage, config_revision, parameter_snapshot_json,
                         queued_at, updated_at
                       ) VALUES (?, ?, ?, 1, NULL, ?, ?, ?, ?, 'queued', NULL, ?, ?, ?, ?)""",
                    (
                        run_id,
                        project_id,
                        content_id,
                        source_scan_id,
                        trigger_source,
                        first_seen_path,
                        latest_seen_path,
                        revision,
                        snapshot_json,
                        timestamp,
                        timestamp,
                    ),
                )
                if source_scan_id is not None:
                    self.connection.execute(
                        "UPDATE scan_events SET created_count = created_count + 1 WHERE scan_id = ?",
                        (source_scan_id,),
                    )
                self.connection.execute(
                    """INSERT INTO workspace_events(
                         event_type, project_id, run_id, scan_id, occurred_at, payload_json
                       ) VALUES ('run_queued', ?, ?, ?, ?, ?)""",
                    (project_id, run_id, source_scan_id, timestamp, stable_json({"content_id": content_id})),
                )
        except sqlite3.IntegrityError as error:
            if "runs.project_id, runs.content_id, runs.processing_sequence" not in str(error):
                raise
            existing = self.find_run(project_id, content_id, 1)
            if existing is None:
                raise
            with self.transaction():
                self.connection.execute(
                    "UPDATE runs SET latest_seen_path = ?, updated_at = ? WHERE run_id = ?",
                    (latest_seen_path, timestamp, existing.run_id),
                )
            refreshed = self.get_run(existing.run_id)
            assert refreshed is not None
            return NormalRunCreationResult(created=False, duplicate=True, run=refreshed)
        created = self.get_run(run_id)
        assert created is not None
        return NormalRunCreationResult(created=True, duplicate=False, run=created)

    def get_run(self, run_id: str) -> Run | None:
        row = _one(self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
        return self._run_from_row(row) if row else None

    def find_run(self, project_id: str, content_id: str, processing_sequence: int = 1) -> Run | None:
        row = _one(
            self.connection.execute(
                "SELECT * FROM runs WHERE project_id = ? AND content_id = ? AND processing_sequence = ?",
                (project_id, content_id, processing_sequence),
            )
        )
        return self._run_from_row(row) if row else None

    def list_runs(self, project_id: str | None = None) -> list[Run]:
        if project_id is None:
            cursor = self.connection.execute("SELECT * FROM runs ORDER BY queued_at, run_id")
        else:
            cursor = self.connection.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY queued_at, run_id", (project_id,)
            )
        return [self._run_from_row(row) for row in _dicts(cursor)]

    def find_active_run(
        self,
        project_id: str,
        content_id: str,
        *,
        exclude_run_id: str | None = None,
    ) -> Run | None:
        row = _one(
            self.connection.execute(
                """SELECT * FROM runs
                   WHERE project_id = ? AND content_id = ?
                     AND status IN ('queued', 'processing') AND run_id <> ifnull(?, '')
                   ORDER BY queued_at, run_id LIMIT 1""",
                (project_id, content_id, exclude_run_id),
            )
        )
        return self._run_from_row(row) if row else None

    def list_content_runs(self, project_id: str, content_id: str) -> list[Run]:
        rows = _dicts(
            self.connection.execute(
                """SELECT * FROM runs WHERE project_id = ? AND content_id = ?
                   ORDER BY processing_sequence DESC, run_id""",
                (project_id, content_id),
            )
        )
        return [self._run_from_row(row) for row in rows]

    def create_reprocess_run(
        self,
        origin_run_id: str,
        *,
        request_id: str,
        request_hash: str,
        config_revision: int,
        parameter_snapshot: Mapping[str, Any],
        source_path: str,
        queued_at: str | None = None,
    ) -> tuple[Run, str]:
        """Create or reuse the single active derived Run in one write transaction."""
        scope = f"run_reprocess:{origin_run_id}"
        snapshot_json = stable_json(parameter_snapshot)
        timestamp = normalize_utc(queued_at)
        with self.transaction():
            existing = _one(
                self.connection.execute(
                    "SELECT * FROM idempotency_keys WHERE scope = ? AND request_id = ?",
                    (scope, request_id),
                )
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise RequestConflictError("reprocess_request_conflict")
                run = self.get_run(str(existing["object_id"]))
                if run is None:
                    raise RuntimeError("reprocess idempotency points to a missing run")
                return run, "idempotent_request"
            origin = self.get_run(origin_run_id)
            if origin is None:
                raise KeyError(origin_run_id)
            if origin.status not in {"completed", "failed"}:
                raise RevisionConflictError("origin_run_state_changed")
            project = self.get_project(origin.project_id)
            runtime = self.get_runtime(origin.project_id)
            if (
                project is None
                or project.activation_state == "inactive"
                or project.current_config_revision != config_revision
                or runtime is None
                or runtime.readiness_state != "ready"
                or runtime.failure_code
            ):
                raise RevisionConflictError("project_runtime_changed")
            if self.get_config_revision(origin.project_id, config_revision) is None:
                raise RevisionConflictError("project_config_revision_conflict")
            active = self.find_active_run(origin.project_id, origin.content_id)
            if active is not None:
                self.connection.execute(
                    """INSERT INTO idempotency_keys(
                         scope, request_id, request_hash, object_type, object_id, created_at
                       ) VALUES (?, ?, ?, 'run', ?, ?)""",
                    (scope, request_id, request_hash, active.run_id, timestamp),
                )
                return active, "active_run"
            row = self.connection.execute(
                "SELECT COALESCE(MAX(processing_sequence), 0) FROM runs WHERE project_id = ? AND content_id = ?",
                (origin.project_id, origin.content_id),
            ).fetchone()
            sequence = int(row[0]) + 1
            run_id = new_id()
            self.connection.execute(
                """INSERT INTO runs(
                     run_id, project_id, content_id, processing_sequence, origin_run_id,
                     source_scan_id, trigger_source, first_seen_path, latest_seen_path,
                     status, current_stage, config_revision, parameter_snapshot_json,
                     queued_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, NULL, 'manual', ?, ?, 'queued', NULL, ?, ?, ?, ?)""",
                (
                    run_id,
                    origin.project_id,
                    origin.content_id,
                    sequence,
                    origin_run_id,
                    source_path,
                    source_path,
                    config_revision,
                    snapshot_json,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, run_id, occurred_at, payload_json)
                   VALUES ('run_queued', ?, ?, ?, ?)""",
                (
                    origin.project_id,
                    run_id,
                    timestamp,
                    stable_json({"content_id": origin.content_id, "origin_run_id": origin_run_id, "processing_sequence": sequence}),
                ),
            )
            self.connection.execute(
                """INSERT INTO idempotency_keys(
                     scope, request_id, request_hash, object_type, object_id, created_at
                   ) VALUES (?, ?, ?, 'run', ?, ?)""",
                (scope, request_id, request_hash, run_id, timestamp),
            )
        run = self.get_run(run_id)
        assert run is not None
        return run, "created"

    @staticmethod
    def _run_from_row(row: dict[str, Any]) -> Run:
        row["parameter_snapshot"] = parse_json(row.pop("parameter_snapshot_json"))
        return Run(**row)

    def append_stage_event(
        self,
        run_id: str,
        *,
        stage: str,
        event_type: str,
        detail: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> RunStageEvent:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO run_stage_events(run_id, stage, event_type, occurred_at, detail_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, stage, event_type, timestamp, stable_json(detail or {})),
            )
            event_id = int(cursor.lastrowid)
        row = _one(self.connection.execute("SELECT * FROM run_stage_events WHERE event_id = ?", (event_id,)))
        assert row is not None
        row["detail"] = parse_json(row.pop("detail_json"))
        return RunStageEvent(**row)

    def list_stage_events(self, run_id: str) -> list[RunStageEvent]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM run_stage_events WHERE run_id = ? ORDER BY event_id", (run_id,)
            )
        )
        events = []
        for row in rows:
            row["detail"] = parse_json(row.pop("detail_json"))
            events.append(RunStageEvent(**row))
        return events

    def transition_run(
        self,
        run_id: str,
        *,
        status: str,
        stage: str,
        event_type: str,
        detail: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> Run:
        """Atomically update the run summary and append its durable stage/workspace events."""
        timestamp = normalize_utc(occurred_at)
        detail_json = stable_json(detail or {})
        with self.transaction():
            cursor = self.connection.execute(
                """UPDATE runs SET status = ?, current_stage = ?, updated_at = ?,
                     started_at = CASE WHEN ? = 'processing' THEN COALESCE(started_at, ?) ELSE started_at END,
                     review_at = CASE WHEN ? = 'awaiting_review' THEN ? ELSE review_at END,
                     completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END,
                     error_code = ?, error_summary = ?
                   WHERE run_id = ?""",
                (
                    status,
                    stage,
                    timestamp,
                    status,
                    timestamp,
                    status,
                    timestamp,
                    status,
                    timestamp,
                    error_code,
                    error_summary,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                "INSERT INTO run_stage_events(run_id, stage, event_type, occurred_at, detail_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, stage, event_type, timestamp, detail_json),
            )
            run_row = _one(self.connection.execute("SELECT project_id FROM runs WHERE run_id = ?", (run_id,)))
            assert run_row is not None
            self.connection.execute(
                """INSERT INTO workspace_events(
                     event_type, project_id, run_id, occurred_at, payload_json
                   ) VALUES ('run_status_changed', ?, ?, ?, ?)""",
                (
                    run_row["project_id"],
                    run_id,
                    timestamp,
                    stable_json({"status": status, "stage": stage, "stage_event": event_type}),
                ),
            )
        result = self.get_run(run_id)
        assert result is not None
        return result

    def append_workspace_event(
        self,
        event_type: str,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        scan_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> WorkspaceEvent:
        with self.transaction():
            cursor = self.connection.execute(
                """INSERT INTO workspace_events(
                     event_type, project_id, run_id, scan_id, occurred_at, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (event_type, project_id, run_id, scan_id, normalize_utc(occurred_at), stable_json(payload or {})),
            )
            event_id = int(cursor.lastrowid)
        return next(event for event in self.list_workspace_events(after_event_id=event_id - 1) if event.event_id == event_id)

    def list_workspace_events(self, *, after_event_id: int = 0) -> list[WorkspaceEvent]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM workspace_events WHERE event_id > ? ORDER BY event_id", (after_event_id,)
            )
        )
        events: list[WorkspaceEvent] = []
        for row in rows:
            row["payload"] = parse_json(row.pop("payload_json"))
            events.append(WorkspaceEvent(**row))
        return events

    def max_workspace_event_id(self) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(event_id), 0) FROM workspace_events").fetchone()
        return int(row[0]) if row else 0

    def set_workspace_view(self, view_id: str, last_seen_event_id: int, *, updated_at: str | None = None) -> None:
        if last_seen_event_id < 0:
            raise ValueError("last_seen_event_id cannot be negative")
        with self.transaction():
            self.connection.execute(
                """INSERT INTO workspace_views(view_id, last_seen_event_id, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(view_id) DO UPDATE SET
                     last_seen_event_id = MAX(workspace_views.last_seen_event_id, excluded.last_seen_event_id),
                     updated_at = excluded.updated_at""",
                (view_id, last_seen_event_id, normalize_utc(updated_at)),
            )

    def get_workspace_view(self, view_id: str) -> dict[str, Any] | None:
        return _one(self.connection.execute("SELECT * FROM workspace_views WHERE view_id = ?", (view_id,)))

    def save_idempotency_key(
        self,
        scope: str,
        request_id: str,
        *,
        request_hash: str,
        object_type: str,
        object_id: str,
        created_at: str | None = None,
    ) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO idempotency_keys(
                     scope, request_id, request_hash, object_type, object_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (scope, request_id, request_hash, object_type, object_id, normalize_utc(created_at)),
            )
        return cursor.rowcount == 1

    def get_idempotency_key(self, scope: str, request_id: str) -> dict[str, Any] | None:
        return _one(
            self.connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND request_id = ?",
                (scope, request_id),
            )
        )

    @staticmethod
    def _review_session_from_row(row: dict[str, Any]) -> AIReviewSession:
        row["parameter_snapshot"] = parse_json(row.pop("parameter_snapshot_json"))
        row["warnings"] = parse_json(row.pop("warnings_json"))
        return AIReviewSession(**row)

    @staticmethod
    def _decision_from_row(row: dict[str, Any]) -> CandidateDecision:
        row["remove_ranges"] = parse_json(row.pop("remove_ranges_json"))
        row["risks"] = parse_json(row.pop("risks_json"))
        return CandidateDecision(**row)

    @staticmethod
    def _result_from_row(row: dict[str, Any]) -> RunResult:
        row["warnings"] = parse_json(row.pop("warnings_json"))
        return RunResult(**row)

    @staticmethod
    def _material_from_row(row: dict[str, Any]) -> OutputMaterial:
        row["title_candidates"] = parse_json(row.pop("title_candidates_json"))
        row["tags"] = parse_json(row.pop("tags_json"))
        return OutputMaterial(**row)

    @staticmethod
    def _issue_from_row(row: dict[str, Any]) -> Issue:
        row["reuse_stages"] = parse_json(row.pop("reuse_stages_json"))
        row["redo_stages"] = parse_json(row.pop("redo_stages_json"))
        row["operational_overrides"] = parse_json(row.pop("operational_overrides_json"))
        row["retry_exhausted"] = bool(row["retry_exhausted"])
        return Issue(**row)

    @staticmethod
    def _recovery_from_row(row: dict[str, Any]) -> RecoveryAttempt:
        row["reuse_stages"] = parse_json(row.pop("reuse_stages_json"))
        row["redo_stages"] = parse_json(row.pop("redo_stages_json"))
        row["operational_overrides"] = parse_json(row.pop("operational_overrides_json"))
        row["result"] = parse_json(row.pop("result_json"))
        return RecoveryAttempt(**row)

    def create_ai_review_session(
        self,
        run_id: str,
        *,
        attempt_number: int,
        resource_ref: str,
        model_name: str,
        strategy_version: str,
        parameter_snapshot: Mapping[str, Any],
        format_version: int = 1,
        evidence_relative_path: str | None = None,
        review_session_id: str | None = None,
        started_at: str | None = None,
    ) -> AIReviewSession:
        attempt_number = _integer(attempt_number, field="attempt_number", minimum=1)
        format_version = _integer(format_version, field="format_version", minimum=1)
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        review_session_id = review_session_id or new_id()
        timestamp = normalize_utc(started_at)
        evidence_relative_path = validate_relative_reference(
            evidence_relative_path, field="evidence_relative_path"
        )
        safe_resource_ref = sanitize_persisted_text(resource_ref)
        safe_model_name = sanitize_persisted_text(model_name)
        safe_strategy_version = sanitize_persisted_text(strategy_version)
        snapshot_json = stable_json(_safe_payload(parameter_snapshot))
        with self.transaction():
            existing = _one(
                self.connection.execute(
                    "SELECT * FROM ai_review_sessions WHERE run_id = ? AND attempt_number = ?",
                    (run_id, attempt_number),
                )
            )
            if existing is not None:
                expected = (
                    safe_resource_ref,
                    safe_model_name,
                    safe_strategy_version,
                    run.config_revision,
                    snapshot_json,
                    format_version,
                    evidence_relative_path,
                )
                actual = tuple(
                    existing[field]
                    for field in (
                        "resource_ref",
                        "model_name",
                        "strategy_version",
                        "config_revision",
                        "parameter_snapshot_json",
                        "format_version",
                        "evidence_relative_path",
                    )
                )
                if actual != expected:
                    raise RequestConflictError("review_attempt_conflict")
                review_session_id = str(existing["review_session_id"])
            else:
                self.connection.execute(
                    """INSERT INTO ai_review_sessions(
                         review_session_id, run_id, attempt_number, status, resource_ref, model_name,
                         strategy_version, config_revision, parameter_snapshot_json, format_version,
                         evidence_relative_path, started_at, updated_at
                       ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review_session_id,
                        run_id,
                        attempt_number,
                        safe_resource_ref,
                        safe_model_name,
                        safe_strategy_version,
                        run.config_revision,
                        snapshot_json,
                        format_version,
                        evidence_relative_path,
                        timestamp,
                        timestamp,
                    ),
                )
        result = self.get_ai_review_session(review_session_id)
        assert result is not None
        return result

    def get_ai_review_session(self, review_session_id: str) -> AIReviewSession | None:
        row = _one(
            self.connection.execute(
                "SELECT * FROM ai_review_sessions WHERE review_session_id = ?", (review_session_id,)
            )
        )
        return self._review_session_from_row(row) if row else None

    def list_ai_review_sessions(self, run_id: str) -> list[AIReviewSession]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM ai_review_sessions WHERE run_id = ? ORDER BY attempt_number", (run_id,)
            )
        )
        return [self._review_session_from_row(row) for row in rows]

    def get_candidate_decisions(self, review_session_id: str) -> list[CandidateDecision]:
        rows = _dicts(
            self.connection.execute(
                """SELECT * FROM candidate_decisions WHERE review_session_id = ?
                   ORDER BY rank, decision_id""",
                (review_session_id,),
            )
        )
        return [self._decision_from_row(row) for row in rows]

    def get_run_output(self, output_id: str) -> RunOutput | None:
        row = _one(self.connection.execute("SELECT * FROM run_outputs WHERE output_id = ?", (output_id,)))
        return RunOutput(**row) if row else None

    def list_run_outputs(self, run_id: str) -> list[RunOutput]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM run_outputs WHERE run_id = ? ORDER BY display_order, output_id", (run_id,)
            )
        )
        return [RunOutput(**row) for row in rows]

    def get_output_material(self, output_id: str) -> OutputMaterial | None:
        row = _one(self.connection.execute("SELECT * FROM output_materials WHERE output_id = ?", (output_id,)))
        return self._material_from_row(row) if row else None

    def get_run_result(self, run_id: str) -> RunResult | None:
        row = _one(self.connection.execute("SELECT * FROM run_results WHERE run_id = ?", (run_id,)))
        return self._result_from_row(row) if row else None

    def list_run_results(self, project_id: str | None = None, *, unseen_only: bool = False) -> list[RunResult]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            conditions.append("runs.project_id = ?")
            parameters.append(project_id)
        if unseen_only:
            conditions.append(
                "(run_results.seen_result_revision IS NULL "
                "OR run_results.seen_result_revision < run_results.result_revision)"
            )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = _dicts(
            self.connection.execute(
                "SELECT run_results.* FROM run_results JOIN runs USING(run_id)"
                f"{where} ORDER BY run_results.completed_at DESC, run_results.run_id DESC",  # noqa: S608
                parameters,
            )
        )
        return [self._result_from_row(row) for row in rows]

    def register_verified_review(
        self,
        review_session_id: str,
        *,
        status: str,
        decisions: Sequence[Mapping[str, Any]],
        outputs: Sequence[Mapping[str, Any]] = (),
        materials: Sequence[Mapping[str, Any]] = (),
        overall_summary: str = "",
        warnings: Sequence[Mapping[str, Any]] = (),
        evidence_relative_path: str | None = None,
        evidence_sha256: str | None = None,
        completed_at: str | None = None,
    ) -> AIReviewSession:
        if status not in {"selected", "no_clip"}:
            raise ValueError("verified review status must be selected or no_clip")
        timestamp = normalize_utc(completed_at)
        evidence_relative_path = validate_relative_reference(
            evidence_relative_path, field="evidence_relative_path"
        )
        evidence_sha256 = validate_sha256(evidence_sha256, field="evidence_sha256")
        session = self.get_ai_review_session(review_session_id)
        if session is None:
            raise KeyError(review_session_id)
        if session.status != "running":
            if session.status == status and session.evidence_sha256 == evidence_sha256:
                return session
            raise ValueError("review_session_not_running")

        decision_items = [dict(item) for item in decisions]
        output_items = [dict(item) for item in outputs]
        material_items = [dict(item) for item in materials]
        candidate_ids = [
            validate_public_identifier(item.get("candidate_id"), field="candidate_id")
            for item in decision_items
        ]
        ranks = [
            _integer(item.get("rank", index), field="rank", minimum=1)
            for index, item in enumerate(decision_items, start=1)
        ]
        if not candidate_ids or any(not value for value in candidate_ids):
            if status != "no_clip" or decision_items:
                raise ValueError("candidate_id is required")
        if len(set(candidate_ids)) != len(candidate_ids) or len(set(ranks)) != len(ranks):
            raise ValueError("candidate decisions and ranks must be unique")
        selected_items = [item for item in decision_items if item.get("decision") == "selected"]
        rejected_items = [item for item in decision_items if item.get("decision") == "rejected"]
        if len(selected_items) + len(rejected_items) != len(decision_items):
            raise ValueError("all candidate decisions must be selected or rejected")
        if status == "selected" and not selected_items:
            raise ValueError("selected review requires at least one selected candidate")
        if status == "no_clip" and selected_items:
            raise ValueError("no_clip review cannot select candidates")
        output_by_id = {str(item.get("output_id") or ""): item for item in output_items}
        output_by_candidate = {
            validate_public_identifier(item.get("candidate_id"), field="candidate_id"): item
            for item in output_items
        }
        if "" in output_by_id or len(output_by_id) != len(output_items) or len(output_by_candidate) != len(output_items):
            raise ValueError("outputs require unique output_id and candidate_id")
        selected_candidate_ids = {str(item["candidate_id"]) for item in selected_items}
        if set(output_by_candidate) != selected_candidate_ids:
            raise ValueError("selected candidates and outputs must match exactly")
        material_by_output = {str(item.get("output_id") or ""): item for item in material_items}
        if "" in material_by_output or len(material_by_output) != len(material_items):
            raise ValueError("materials require unique output_id")
        if set(material_by_output) != set(output_by_id):
            raise ValueError("every selected output requires one initial material")

        with self.transaction():
            current_session = _one(
                self.connection.execute(
                    "SELECT status, evidence_sha256 FROM ai_review_sessions WHERE review_session_id = ?",
                    (review_session_id,),
                )
            )
            if current_session is None:
                raise KeyError(review_session_id)
            if current_session["status"] != "running":
                if current_session["status"] == status and current_session["evidence_sha256"] == evidence_sha256:
                    result = self.get_ai_review_session(review_session_id)
                    assert result is not None
                    return result
                raise ValueError("review_session_not_running")
            for index, item in enumerate(output_items, start=1):
                relative_path = validate_relative_reference(str(item["relative_path"]), field="relative_path")
                file_name = str(item["file_name"])
                if not file_name or "/" in file_name or "\\" in file_name:
                    raise ValueError("file_name must be a base name")
                self.connection.execute(
                    """INSERT INTO run_outputs(
                         output_id, run_id, review_session_id, candidate_id, display_order,
                         status, storage_kind, relative_path, file_name, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 'pending', 'project_output', ?, ?, ?)""",
                    (
                        item["output_id"],
                        session.run_id,
                        review_session_id,
                        item["candidate_id"],
                        _integer(item.get("display_order", index), field="display_order", minimum=1),
                        relative_path,
                        file_name,
                        timestamp,
                    ),
                )
            for index, item in enumerate(decision_items, start=1):
                selected = item["decision"] == "selected"
                candidate_id = str(item["candidate_id"])
                output = output_by_candidate.get(candidate_id)
                source_start_ms = _integer(item.get("source_start_ms", 0), field="source_start_ms")
                source_end_ms = _integer(item.get("source_end_ms", 0), field="source_end_ms", minimum=1)
                if source_end_ms <= source_start_ms:
                    raise ValueError("source_end_ms must be greater than source_start_ms")
                selected_start_ms = (
                    _integer(item.get("selected_start_ms", source_start_ms), field="selected_start_ms")
                    if selected
                    else None
                )
                selected_end_ms = (
                    _integer(item.get("selected_end_ms", source_end_ms), field="selected_end_ms", minimum=1)
                    if selected
                    else None
                )
                if selected and not (
                    source_start_ms <= selected_start_ms < selected_end_ms <= source_end_ms
                ):
                    raise ValueError("selected range must be contained in the source range")
                remove_ranges = validate_remove_ranges(
                    item.get("remove_ranges", []),
                    start_ms=selected_start_ms if selected_start_ms is not None else source_start_ms,
                    end_ms=selected_end_ms if selected_end_ms is not None else source_end_ms,
                )
                self.connection.execute(
                    """INSERT INTO candidate_decisions(
                         decision_id, review_session_id, run_id, candidate_id, decision, rank,
                         candidate_type, source_start_ms, source_end_ms, selected_start_ms,
                         selected_end_ms, remove_ranges_json, hook, core_value, reason,
                         rejection_reason_code, risks_json, transcript_excerpt, output_id,
                         internal_sort_value
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.get("decision_id") or new_id(),
                        review_session_id,
                        session.run_id,
                        candidate_id,
                        item["decision"],
                        int(item.get("rank", index)),
                        sanitize_persisted_text(item.get("candidate_type", "highlight")),
                        source_start_ms,
                        source_end_ms,
                        selected_start_ms,
                        selected_end_ms,
                        stable_json(remove_ranges),
                        sanitize_persisted_text(item.get("hook", "")),
                        sanitize_persisted_text(item.get("core_value", "")),
                        sanitize_persisted_text(item.get("reason", "")),
                        sanitize_persisted_text(item.get("rejection_reason_code")) or None,
                        stable_json(_safe_payload(item.get("risks", []))),
                        sanitize_persisted_text(item.get("transcript_excerpt", "")),
                        output["output_id"] if output else None,
                        item.get("internal_sort_value"),
                    ),
                )
            for item in material_items:
                titles = validate_titles(item.get("title_candidates", []), item.get("preferred_title_id"))
                self.connection.execute(
                    """INSERT INTO output_materials(
                         material_id, output_id, title_candidates_json, preferred_title_id,
                         description, tags_json, generation_source, status, material_revision,
                         created_at, saved_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        item.get("material_id") or new_id(),
                        item["output_id"],
                        stable_json(titles),
                        item.get("preferred_title_id"),
                        sanitize_persisted_text(item.get("description", "")),
                        stable_json([sanitize_persisted_text(tag) for tag in item.get("tags", [])]),
                        sanitize_persisted_text(item.get("generation_source", "ai_review")),
                        item.get("status", "ready"),
                        timestamp,
                        timestamp if item.get("status", "ready") == "ready" else None,
                        timestamp,
                    ),
                )
            self.connection.execute(
                """UPDATE ai_review_sessions SET status = ?, overall_summary = ?, warnings_json = ?,
                     candidate_count = ?, selected_count = ?, rejected_count = ?,
                     evidence_relative_path = COALESCE(?, evidence_relative_path), evidence_sha256 = ?,
                     completed_at = ?, validated_at = ?, updated_at = ?
                   WHERE review_session_id = ? AND status = 'running'""",
                (
                    status,
                    sanitize_persisted_text(overall_summary),
                    stable_json(_safe_payload(warnings)),
                    len(decision_items),
                    len(selected_items),
                    len(rejected_items),
                    evidence_relative_path,
                    evidence_sha256,
                    timestamp,
                    timestamp,
                    timestamp,
                    review_session_id,
                ),
            )
            if status == "no_clip":
                self.connection.execute(
                    """INSERT INTO run_results(
                         run_id, review_session_id, result_type, candidate_count, selected_count,
                         rejected_count, available_output_count, failed_output_count,
                         total_duration_ms, overall_summary, warnings_json, format_version,
                         result_revision, source_kind, completed_at, updated_at
                       ) VALUES (?, ?, 'no_clip', ?, 0, ?, 0, 0, 0, ?, ?, ?, 1,
                         'native_v2', ?, ?)""",
                    (
                        session.run_id,
                        review_session_id,
                        len(decision_items),
                        len(rejected_items),
                        sanitize_persisted_text(overall_summary),
                        stable_json(_safe_payload(warnings)),
                        session.format_version,
                        timestamp,
                        timestamp,
                    ),
                )
                self.connection.execute(
                    """UPDATE runs SET status = 'completed', current_stage = 'review',
                         completed_at = ?, updated_at = ?, error_code = NULL, error_summary = NULL
                       WHERE run_id = ?""",
                    (timestamp, timestamp, session.run_id),
                )
                self.connection.execute(
                    """INSERT INTO run_stage_events(run_id, stage, event_type, occurred_at, detail_json)
                       VALUES (?, 'review', 'no_clip_completed', ?, '{}')""",
                    (session.run_id, timestamp),
                )
            run_row = _one(
                self.connection.execute("SELECT project_id FROM runs WHERE run_id = ?", (session.run_id,))
            )
            assert run_row is not None
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, run_id, occurred_at, payload_json)
                   VALUES ('review_verified', ?, ?, ?, ?)""",
                (
                    run_row["project_id"],
                    session.run_id,
                    timestamp,
                    stable_json({"review_session_id": review_session_id, "status": status}),
                ),
            )
        result = self.get_ai_review_session(review_session_id)
        assert result is not None
        return result

    def update_output_and_reproject_result(
        self,
        output_id: str,
        *,
        status: str,
        media_metadata: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        warnings: Sequence[Mapping[str, Any]] | None = None,
        occurred_at: str | None = None,
    ) -> RunResult | None:
        if status not in {"pending", "rendering", "ready", "failed", "missing", "unreadable"}:
            raise ValueError("invalid output status")
        timestamp = normalize_utc(occurred_at)
        output = self.get_run_output(output_id)
        if output is None:
            raise KeyError(output_id)
        metadata = dict(media_metadata or {})
        required_metadata = {"duration_ms", "width", "height", "container", "video_codec", "byte_size"}
        if status == "ready" and set(metadata) != required_metadata:
            raise ValueError("ready output requires complete media metadata")
        if status == "ready":
            for field in ("duration_ms", "width", "height", "byte_size"):
                metadata[field] = _integer(metadata[field], field=field)
            if not all(isinstance(metadata[field], str) and metadata[field] for field in ("container", "video_codec")):
                raise ValueError("container and video_codec are required")
        with self.transaction():
            current = _one(self.connection.execute("SELECT * FROM run_outputs WHERE output_id = ?", (output_id,)))
            assert current is not None
            allowed_transitions = {
                "pending": {"pending", "rendering", "failed"},
                "rendering": {"rendering", "ready", "failed"},
                "ready": {"ready", "missing", "unreadable"},
                "failed": {"failed", "rendering"},
                "missing": {"missing", "rendering", "ready"},
                "unreadable": {"unreadable", "rendering", "ready"},
            }
            if status not in allowed_transitions[str(current["status"])]:
                raise ValueError("invalid output status transition")
            values = {
                "duration_ms": metadata.get("duration_ms") if status == "ready" else current["duration_ms"],
                "width": metadata.get("width") if status == "ready" else current["width"],
                "height": metadata.get("height") if status == "ready" else current["height"],
                "container": metadata.get("container") if status == "ready" else current["container"],
                "video_codec": metadata.get("video_codec") if status == "ready" else current["video_codec"],
                "byte_size": metadata.get("byte_size") if status == "ready" else current["byte_size"],
            }
            generated_at = timestamp if status == "ready" else current["generated_at"]
            verified_at = timestamp if status == "ready" else current["verified_at"]
            changed = any(
                (
                    current["status"] != status,
                    current["error_code"] != error_code,
                    current["error_summary"] != sanitize_persisted_text(error_summary) if error_summary else current["error_summary"] is not None,
                    *(current[field] != value for field, value in values.items()),
                )
            )
            self.connection.execute(
                """UPDATE run_outputs SET status = ?, duration_ms = ?, width = ?, height = ?,
                     container = ?, video_codec = ?, byte_size = ?, generated_at = ?, verified_at = ?,
                     updated_at = ?, error_code = ?, error_summary = ? WHERE output_id = ?""",
                (
                    status,
                    values["duration_ms"],
                    values["width"],
                    values["height"],
                    values["container"],
                    values["video_codec"],
                    values["byte_size"],
                    generated_at,
                    verified_at,
                    timestamp,
                    error_code,
                    sanitize_persisted_text(error_summary) if error_summary else None,
                    output_id,
                ),
            )
            output_rows = _dicts(
                self.connection.execute(
                    "SELECT * FROM run_outputs WHERE run_id = ? ORDER BY display_order", (output.run_id,)
                )
            )
            terminal = all(
                item["status"] in {"ready", "failed", "missing", "unreadable"} for item in output_rows
            )
            if not terminal:
                return self.get_run_result(output.run_id)
            session_row = _one(
                self.connection.execute(
                    "SELECT * FROM ai_review_sessions WHERE review_session_id = ?",
                    (output.review_session_id,),
                )
            )
            assert session_row is not None
            decision_rows = _dicts(
                self.connection.execute(
                    "SELECT * FROM candidate_decisions WHERE review_session_id = ? ORDER BY rank",
                    (output.review_session_id,),
                )
            )
            projected = project_result_projection(
                review_status=session_row["status"],
                decisions=decision_rows,
                outputs=output_rows,
                material_problem_count=int(
                    self.connection.execute(
                        """SELECT COUNT(*) FROM output_materials
                           JOIN run_outputs USING(output_id)
                           WHERE run_outputs.run_id = ? AND output_materials.status = 'failed'""",
                        (output.run_id,),
                    ).fetchone()[0]
                ),
            )
            current_result = _one(
                self.connection.execute("SELECT * FROM run_results WHERE run_id = ?", (output.run_id,))
            )
            warning_value = list(warnings) if warnings is not None else (
                parse_json(current_result["warnings_json"]) if current_result else parse_json(session_row["warnings_json"])
            )
            visible = (
                projected.result_type,
                projected.candidate_count,
                projected.selected_count,
                projected.rejected_count,
                projected.available_output_count,
                projected.failed_output_count,
                projected.total_duration_ms,
                stable_json(_safe_payload(warning_value)),
            )
            previous_visible = None
            if current_result is not None:
                previous_visible = (
                    current_result["result_type"],
                    current_result["candidate_count"],
                    current_result["selected_count"],
                    current_result["rejected_count"],
                    current_result["available_output_count"],
                    current_result["failed_output_count"],
                    current_result["total_duration_ms"],
                    current_result["warnings_json"],
                )
            revision = 1 if current_result is None else int(current_result["result_revision"]) + int(visible != previous_visible)
            completed_at = current_result["completed_at"] if current_result else timestamp
            self.connection.execute(
                """INSERT INTO run_results(
                     run_id, review_session_id, result_type, candidate_count, selected_count,
                     rejected_count, available_output_count, failed_output_count, total_duration_ms,
                     overall_summary, warnings_json, format_version, result_revision,
                     seen_result_revision, result_seen_at, source_kind, evidence_hash,
                     completed_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                     'native_v2', NULL, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     result_type = excluded.result_type,
                     candidate_count = excluded.candidate_count,
                     selected_count = excluded.selected_count,
                     rejected_count = excluded.rejected_count,
                     available_output_count = excluded.available_output_count,
                     failed_output_count = excluded.failed_output_count,
                     total_duration_ms = excluded.total_duration_ms,
                     warnings_json = excluded.warnings_json,
                     result_revision = excluded.result_revision,
                     updated_at = excluded.updated_at""",
                (
                    output.run_id,
                    output.review_session_id,
                    projected.result_type,
                    projected.candidate_count,
                    projected.selected_count,
                    projected.rejected_count,
                    projected.available_output_count,
                    projected.failed_output_count,
                    projected.total_duration_ms,
                    session_row["overall_summary"],
                    stable_json(_safe_payload(warning_value)),
                    session_row["format_version"],
                    revision,
                    completed_at,
                    timestamp,
                ),
            )
            run_status = "failed" if projected.result_type == "unavailable" else "completed"
            self.connection.execute(
                """UPDATE runs SET status = ?, current_stage = 'render', completed_at = ?,
                     updated_at = ?, error_code = ?, error_summary = ? WHERE run_id = ?""",
                (
                    run_status,
                    timestamp if run_status == "completed" else None,
                    timestamp,
                    "result_unavailable" if run_status == "failed" else None,
                    "结果不可用" if run_status == "failed" else None,
                    output.run_id,
                ),
            )
            run_row = _one(self.connection.execute("SELECT project_id FROM runs WHERE run_id = ?", (output.run_id,)))
            assert run_row is not None
            if status in {"failed", "missing", "unreadable"}:
                self._discover_issue_in_transaction(
                    issue_code=f"output_{status}",
                    category="output",
                    scope_type="output",
                    project_id=run_row["project_id"],
                    run_id=output.run_id,
                    output_id=output_id,
                    material_id=None,
                    issue_group_key=f"output:{status}",
                    status="action_required",
                    impact_level="partial" if projected.available_output_count else "blocking",
                    title="成片不可用",
                    summary=error_summary or "成片处理未成功",
                    impact="该成片当前不可用",
                    preserved_content="其他已完成内容保持不变",
                    next_step="检查后重试该成片",
                    recovery_capability="retry_output",
                    occurred_at=timestamp,
                )
            elif status == "ready":
                issue_rows = _dicts(
                    self.connection.execute(
                        """SELECT issue_id FROM issues WHERE output_id = ? AND status <> 'resolved'
                           AND issue_code IN ('output_failed', 'output_missing', 'output_unreadable')""",
                        (output_id,),
                    )
                )
                for issue_row in issue_rows:
                    self.connection.execute(
                        """UPDATE issues SET status = 'resolved', issue_revision = issue_revision + 1,
                             updated_at = ?, resolved_at = ? WHERE issue_id = ?""",
                        (timestamp, timestamp, issue_row["issue_id"]),
                    )
                    self.connection.execute(
                        """INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json)
                           VALUES (?, 'output_verified', ?, '{}')""",
                        (issue_row["issue_id"], timestamp),
                    )
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, run_id, occurred_at, payload_json)
                   VALUES ('run_result_updated', ?, ?, ?, ?)""",
                (
                    run_row["project_id"],
                    output.run_id,
                    timestamp,
                    stable_json(
                        {
                            "output_id": output_id,
                            "result_type": projected.result_type,
                            "result_revision": revision,
                            "changed": changed,
                        }
                    ),
                ),
            )
        return self.get_run_result(output.run_id)

    def mark_result_seen(
        self,
        run_id: str,
        *,
        expected_result_revision: int,
        seen_at: str | None = None,
    ) -> RunResult:
        timestamp = normalize_utc(seen_at)
        with self.transaction():
            current = _one(self.connection.execute("SELECT * FROM run_results WHERE run_id = ?", (run_id,)))
            if current is None:
                raise KeyError(run_id)
            if int(current["result_revision"]) != expected_result_revision:
                raise RevisionConflictError("result_revision_conflict")
            if current["seen_result_revision"] is None or int(current["seen_result_revision"]) < expected_result_revision:
                self.connection.execute(
                    """UPDATE run_results SET seen_result_revision = ?,
                         result_seen_at = COALESCE(result_seen_at, ?) WHERE run_id = ?""",
                    (expected_result_revision, timestamp, run_id),
                )
        result = self.get_run_result(run_id)
        assert result is not None
        return result

    def update_output_material(
        self,
        output_id: str,
        *,
        expected_material_revision: int,
        title_candidates: Sequence[Mapping[str, Any]],
        preferred_title_id: str | None,
        description: str,
        tags: Sequence[str],
        status: str = "ready",
        saved_at: str | None = None,
    ) -> OutputMaterial:
        if status not in {"pending", "ready", "failed"}:
            raise ValueError("invalid material status")
        titles = validate_titles([dict(item) for item in title_candidates], preferred_title_id)
        timestamp = normalize_utc(saved_at)
        with self.transaction():
            current = _one(
                self.connection.execute("SELECT * FROM output_materials WHERE output_id = ?", (output_id,))
            )
            if current is None:
                raise KeyError(output_id)
            if int(current["material_revision"]) != expected_material_revision:
                raise RevisionConflictError("material_revision_conflict")
            self.connection.execute(
                """UPDATE output_materials SET title_candidates_json = ?, preferred_title_id = ?,
                     description = ?, tags_json = ?, status = ?,
                     material_revision = material_revision + 1, saved_at = ?, updated_at = ?
                   WHERE output_id = ?""",
                (
                    stable_json(titles),
                    preferred_title_id,
                    sanitize_persisted_text(description),
                    stable_json([sanitize_persisted_text(tag) for tag in tags]),
                    status,
                    timestamp,
                    timestamp,
                    output_id,
                ),
            )
        result = self.get_output_material(output_id)
        assert result is not None
        return result

    def _validate_issue_scope(
        self,
        *,
        scope_type: str,
        project_id: str,
        run_id: str | None,
        output_id: str | None,
        material_id: str | None,
    ) -> None:
        if scope_type not in {"project", "run", "output", "material"}:
            raise ValueError("invalid issue scope")
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        expected_presence = {
            "project": (False, False, False),
            "run": (True, False, False),
            "output": (True, True, False),
            "material": (True, True, True),
        }[scope_type]
        if tuple(value is not None for value in (run_id, output_id, material_id)) != expected_presence:
            raise ValueError("issue scope references do not match scope_type")
        if run_id is not None:
            run = self.get_run(run_id)
            if run is None or run.project_id != project_id:
                raise ValueError("issue run does not belong to project")
        if output_id is not None:
            output = self.get_run_output(output_id)
            if output is None or output.run_id != run_id:
                raise ValueError("issue output does not belong to run")
        if material_id is not None:
            material = _one(
                self.connection.execute(
                    "SELECT material_id, output_id FROM output_materials WHERE material_id = ?", (material_id,)
                )
            )
            if material is None or material["output_id"] != output_id:
                raise ValueError("issue material does not belong to output")

    def _discover_issue_in_transaction(
        self,
        *,
        issue_code: str,
        category: str,
        scope_type: str,
        project_id: str,
        run_id: str | None,
        output_id: str | None,
        material_id: str | None,
        issue_group_key: str,
        status: str,
        impact_level: str,
        title: str,
        summary: str,
        impact: str,
        preserved_content: str,
        next_step: str,
        recovery_capability: str,
        occurred_at: str,
        root_cause_ref: str | None = None,
        safe_checkpoint: str | None = None,
        reuse_stages: Sequence[str] = (),
        redo_stages: Sequence[str] = (),
        operational_overrides: Mapping[str, Any] | None = None,
        automatic_attempt_count: int = 0,
        total_attempt_count: int = 0,
        next_retry_at: str | None = None,
        retry_exhausted: bool = False,
        diagnostic_id: str | None = None,
        diagnostic_summary: str | None = None,
        log_relative_path: str | None = None,
        issue_id: str | None = None,
    ) -> str:
        existing = _one(
            self.connection.execute(
                """SELECT * FROM issues WHERE scope_type = ? AND project_id = ?
                   AND ifnull(run_id, '') = ifnull(?, '')
                   AND ifnull(output_id, '') = ifnull(?, '')
                   AND ifnull(material_id, '') = ifnull(?, '')
                   AND issue_code = ? AND issue_group_key = ? AND status <> 'resolved'""",
                (scope_type, project_id, run_id, output_id, material_id, issue_code, issue_group_key),
            )
        )
        safe_summary = sanitize_persisted_text(summary)
        safe_diagnostic = sanitize_persisted_text(diagnostic_summary) if diagnostic_summary else None
        if existing is not None:
            issue_id = str(existing["issue_id"])
            self.connection.execute(
                """UPDATE issues SET summary = ?, diagnostic_summary = ?, updated_at = ?,
                     issue_revision = issue_revision + 1 WHERE issue_id = ?""",
                (safe_summary, safe_diagnostic, occurred_at, issue_id),
            )
            event_type = "rediscovered"
        else:
            issue_id = issue_id or new_id()
            self.connection.execute(
                """INSERT INTO issues(
                     issue_id, issue_code, category, scope_type, project_id, run_id, output_id,
                     material_id, issue_group_key, root_cause_ref, status, impact_level,
                     title, summary, impact, preserved_content, next_step, recovery_capability,
                     safe_checkpoint, reuse_stages_json, redo_stages_json,
                     operational_overrides_json, automatic_attempt_count, total_attempt_count,
                     next_retry_at, retry_exhausted, diagnostic_id, diagnostic_summary,
                     log_relative_path, issue_revision, occurred_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    issue_id,
                    sanitize_persisted_text(issue_code),
                    sanitize_persisted_text(category),
                    scope_type,
                    project_id,
                    run_id,
                    output_id,
                    material_id,
                    sanitize_persisted_text(issue_group_key),
                    sanitize_persisted_text(root_cause_ref) if root_cause_ref else None,
                    status,
                    sanitize_persisted_text(impact_level),
                    sanitize_persisted_text(title),
                    safe_summary,
                    sanitize_persisted_text(impact),
                    sanitize_persisted_text(preserved_content),
                    sanitize_persisted_text(next_step),
                    sanitize_persisted_text(recovery_capability),
                    sanitize_persisted_text(safe_checkpoint) if safe_checkpoint else None,
                    stable_json(list(reuse_stages)),
                    stable_json(list(redo_stages)),
                    stable_json(_safe_payload(operational_overrides or {})),
                    automatic_attempt_count,
                    total_attempt_count,
                    normalize_utc(next_retry_at) if next_retry_at else None,
                    int(retry_exhausted),
                    sanitize_persisted_text(diagnostic_id) if diagnostic_id else None,
                    safe_diagnostic,
                    validate_relative_reference(log_relative_path, field="log_relative_path"),
                    occurred_at,
                    occurred_at,
                ),
            )
            event_type = "discovered"
        self.connection.execute(
            """INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json)
               VALUES (?, ?, ?, ?)""",
            (issue_id, event_type, occurred_at, stable_json({"summary": safe_summary})),
        )
        return issue_id

    def discover_issue(
        self,
        *,
        issue_code: str,
        category: str,
        scope_type: str,
        project_id: str,
        issue_group_key: str,
        status: str = "action_required",
        run_id: str | None = None,
        output_id: str | None = None,
        material_id: str | None = None,
        impact_level: str = "blocking",
        title: str = "处理问题",
        summary: str = "处理未完成",
        impact: str = "当前对象不可继续处理",
        preserved_content: str = "已完成内容保持不变",
        next_step: str = "检查问题后继续",
        recovery_capability: str = "none",
        root_cause_ref: str | None = None,
        safe_checkpoint: str | None = None,
        reuse_stages: Sequence[str] = (),
        redo_stages: Sequence[str] = (),
        operational_overrides: Mapping[str, Any] | None = None,
        automatic_attempt_count: int = 0,
        total_attempt_count: int = 0,
        next_retry_at: str | None = None,
        retry_exhausted: bool = False,
        diagnostic_id: str | None = None,
        diagnostic_summary: str | None = None,
        log_relative_path: str | None = None,
        issue_id: str | None = None,
        occurred_at: str | None = None,
    ) -> Issue:
        if status not in {"retrying", "action_required", "checking", "ready_to_recover", "recovering", "resolved"}:
            raise ValueError("invalid issue status")
        self._validate_issue_scope(
            scope_type=scope_type,
            project_id=project_id,
            run_id=run_id,
            output_id=output_id,
            material_id=material_id,
        )
        timestamp = normalize_utc(occurred_at)
        normalized_group = sanitize_persisted_text(issue_group_key)
        with self.transaction():
            result_id = self._discover_issue_in_transaction(
                issue_code=issue_code,
                category=category,
                scope_type=scope_type,
                project_id=project_id,
                run_id=run_id,
                output_id=output_id,
                material_id=material_id,
                issue_group_key=normalized_group,
                status=status,
                impact_level=impact_level,
                title=title,
                summary=summary,
                impact=impact,
                preserved_content=preserved_content,
                next_step=next_step,
                recovery_capability=recovery_capability,
                occurred_at=timestamp,
                root_cause_ref=root_cause_ref,
                safe_checkpoint=safe_checkpoint,
                reuse_stages=reuse_stages,
                redo_stages=redo_stages,
                operational_overrides=operational_overrides,
                automatic_attempt_count=automatic_attempt_count,
                total_attempt_count=total_attempt_count,
                next_retry_at=next_retry_at,
                retry_exhausted=retry_exhausted,
                diagnostic_id=diagnostic_id,
                diagnostic_summary=diagnostic_summary,
                log_relative_path=log_relative_path,
                issue_id=issue_id,
            )
        result = self.get_issue(result_id)
        assert result is not None
        return result

    def get_issue(self, issue_id: str) -> Issue | None:
        row = _one(self.connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)))
        return self._issue_from_row(row) if row else None

    def list_issues(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        active_only: bool = False,
    ) -> list[Issue]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            conditions.append("project_id = ?")
            parameters.append(project_id)
        if run_id is not None:
            conditions.append("run_id = ?")
            parameters.append(run_id)
        if active_only:
            conditions.append("status <> 'resolved'")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = _dicts(
            self.connection.execute(
                f"SELECT * FROM issues{where} ORDER BY updated_at DESC, issue_id",  # noqa: S608
                parameters,
            )
        )
        return [self._issue_from_row(row) for row in rows]

    def list_issue_events(self, issue_id: str) -> list[IssueEvent]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM issue_events WHERE issue_id = ? ORDER BY event_id", (issue_id,)
            )
        )
        events: list[IssueEvent] = []
        for row in rows:
            row["detail"] = parse_json(row.pop("detail_json"))
            events.append(IssueEvent(**row))
        return events

    def transition_issue(
        self,
        issue_id: str,
        *,
        expected_issue_revision: int,
        status: str,
        event_type: str,
        detail: Mapping[str, Any] | None = None,
        recovery_capability: str | None = None,
        safe_checkpoint: str | None = None,
        occurred_at: str | None = None,
    ) -> Issue:
        if status not in {"retrying", "action_required", "checking", "ready_to_recover", "recovering", "resolved"}:
            raise ValueError("invalid issue status")
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = _one(self.connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)))
            if current is None:
                raise KeyError(issue_id)
            if int(current["issue_revision"]) != expected_issue_revision:
                raise RevisionConflictError("issue_revision_conflict")
            allowed_transitions = {
                "retrying": {"action_required", "resolved"},
                "action_required": {"checking"},
                "checking": {"action_required", "ready_to_recover", "resolved"},
                "ready_to_recover": {"recovering"},
                "recovering": {"action_required", "resolved"},
                "resolved": set(),
            }
            if status not in allowed_transitions[str(current["status"])]:
                raise ValueError("invalid issue status transition")
            self.connection.execute(
                """UPDATE issues SET status = ?, recovery_capability = ?, safe_checkpoint = ?,
                     issue_revision = issue_revision + 1, updated_at = ?,
                     resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE NULL END
                   WHERE issue_id = ?""",
                (
                    status,
                    sanitize_persisted_text(recovery_capability)
                    if recovery_capability is not None
                    else current["recovery_capability"],
                    sanitize_persisted_text(safe_checkpoint)
                    if safe_checkpoint is not None
                    else current["safe_checkpoint"],
                    timestamp,
                    status,
                    timestamp,
                    issue_id,
                ),
            )
            self.connection.execute(
                "INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json) VALUES (?, ?, ?, ?)",
                (issue_id, event_type, timestamp, stable_json(_safe_payload(detail or {}))),
            )
        result = self.get_issue(issue_id)
        assert result is not None
        return result

    def resolve_run_source_issue(
        self,
        issue_id: str,
        *,
        expected_issue_revision: int,
        source_path: str,
        occurred_at: str | None = None,
    ) -> Issue:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = _one(self.connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)))
            if current is None:
                raise KeyError(issue_id)
            if current["status"] != "checking" or int(current["issue_revision"]) != expected_issue_revision:
                raise RevisionConflictError("issue_revision_conflict")
            run_id = str(current["run_id"] or "")
            cursor = self.connection.execute(
                "UPDATE runs SET latest_seen_path = ?, updated_at = ? WHERE run_id = ?",
                (source_path, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                """UPDATE issues SET status = 'resolved', issue_revision = issue_revision + 1,
                     updated_at = ?, resolved_at = ? WHERE issue_id = ?""",
                (timestamp, timestamp, issue_id),
            )
            self.connection.execute(
                "INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json) VALUES (?, 'source_repaired', ?, '{}')",
                (issue_id, timestamp),
            )
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, run_id, occurred_at, payload_json)
                   VALUES ('source_repaired', ?, ?, ?, ?)""",
                (current["project_id"], run_id, timestamp, stable_json({"issue_id": issue_id})),
            )
        issue = self.get_issue(issue_id)
        assert issue is not None
        return issue

    def register_recovery_attempt(
        self,
        issue_id: str,
        *,
        request_id: str,
        attempt_type: str,
        requested_by: str,
        run_id: str | None = None,
        output_id: str | None = None,
        material_id: str | None = None,
        reuse_stages: Sequence[str] = (),
        redo_stages: Sequence[str] = (),
        operational_overrides: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
        requested_at: str | None = None,
    ) -> RecoveryAttempt:
        if attempt_type not in {"continue_run", "retry_output", "retry_material", "operational_repair"}:
            raise ValueError("invalid recovery attempt type")
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(issue_id)
        timestamp = normalize_utc(requested_at)
        normalized = {
            "attempt_type": attempt_type,
            "run_id": run_id,
            "output_id": output_id,
            "material_id": material_id,
            "requested_by": sanitize_persisted_text(requested_by),
            "reuse_stages_json": stable_json(list(reuse_stages)),
            "redo_stages_json": stable_json(list(redo_stages)),
            "operational_overrides_json": stable_json(_safe_payload(operational_overrides or {})),
        }
        with self.transaction():
            existing = _one(
                self.connection.execute(
                    "SELECT * FROM recovery_attempts WHERE issue_id = ? AND request_id = ?",
                    (issue_id, request_id),
                )
            )
            if existing is not None:
                for field, value in normalized.items():
                    if existing[field] != value:
                        raise RequestConflictError("recovery_request_conflict")
                attempt_id = str(existing["attempt_id"])
            else:
                attempt_id = attempt_id or new_id()
                self.connection.execute(
                    """INSERT INTO recovery_attempts(
                         attempt_id, issue_id, request_id, attempt_type, run_id, output_id,
                         material_id, requested_by, reuse_stages_json, redo_stages_json,
                         operational_overrides_json, status, requested_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)""",
                    (
                        attempt_id,
                        issue_id,
                        request_id,
                        normalized["attempt_type"],
                        normalized["run_id"],
                        normalized["output_id"],
                        normalized["material_id"],
                        normalized["requested_by"],
                        normalized["reuse_stages_json"],
                        normalized["redo_stages_json"],
                        normalized["operational_overrides_json"],
                        timestamp,
                    ),
                )
                self.connection.execute(
                    "UPDATE issues SET total_attempt_count = total_attempt_count + 1, updated_at = ? WHERE issue_id = ?",
                    (timestamp, issue_id),
                )
                self.connection.execute(
                    """INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json)
                       VALUES (?, 'recovery_requested', ?, ?)""",
                    (
                        issue_id,
                        timestamp,
                        stable_json({"attempt_id": attempt_id, "attempt_type": attempt_type}),
                    ),
                )
        result = self.get_recovery_attempt(str(attempt_id))
        assert result is not None
        return result

    def get_recovery_attempt(self, attempt_id: str) -> RecoveryAttempt | None:
        row = _one(
            self.connection.execute("SELECT * FROM recovery_attempts WHERE attempt_id = ?", (attempt_id,))
        )
        return self._recovery_from_row(row) if row else None

    def update_recovery_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> RecoveryAttempt:
        if status not in {"accepted", "running", "completed", "failed"}:
            raise ValueError("invalid recovery status transition")
        timestamp = normalize_utc(occurred_at)
        time_field = {
            "accepted": "accepted_at",
            "running": "started_at",
            "completed": "completed_at",
            "failed": "failed_at",
        }[status]
        with self.transaction():
            current = _one(
                self.connection.execute("SELECT status FROM recovery_attempts WHERE attempt_id = ?", (attempt_id,))
            )
            if current is None:
                raise KeyError(attempt_id)
            allowed_transitions = {
                "requested": {"accepted", "failed"},
                "accepted": {"running", "completed", "failed"},
                "running": {"completed", "failed"},
                "completed": set(),
                "failed": set(),
            }
            if status not in allowed_transitions[str(current["status"])]:
                raise ValueError("invalid recovery status transition")
            cursor = self.connection.execute(
                f"""UPDATE recovery_attempts SET status = ?, {time_field} = ?, result_json = ?
                    WHERE attempt_id = ?""",  # noqa: S608
                (status, timestamp, stable_json(_safe_payload(result or {})), attempt_id),
            )
            assert cursor.rowcount == 1
            attempt_row = _one(
                self.connection.execute("SELECT issue_id FROM recovery_attempts WHERE attempt_id = ?", (attempt_id,))
            )
            assert attempt_row is not None
            self.connection.execute(
                """INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    attempt_row["issue_id"],
                    f"recovery_{status}",
                    timestamp,
                    stable_json({"attempt_id": attempt_id}),
                ),
            )
        updated = self.get_recovery_attempt(attempt_id)
        assert updated is not None
        return updated

    def list_recovery_attempts(self, issue_id: str) -> list[RecoveryAttempt]:
        rows = _dicts(
            self.connection.execute(
                "SELECT * FROM recovery_attempts WHERE issue_id = ? ORDER BY requested_at, attempt_id",
                (issue_id,),
            )
        )
        return [self._recovery_from_row(row) for row in rows]

    def get_migration_session(self, migration_id: str) -> MigrationSession | None:
        row = _one(
            self.connection.execute("SELECT * FROM migration_sessions WHERE migration_id = ?", (migration_id,))
        )
        return _migration_session(row) if row else None

    def get_migration_session_by_fingerprint(self, source_fingerprint: str) -> MigrationSession | None:
        fingerprint = validate_sha256(source_fingerprint, field="source_fingerprint")
        assert fingerprint is not None
        row = _one(
            self.connection.execute(
                "SELECT * FROM migration_sessions WHERE source_fingerprint = ?", (fingerprint,)
            )
        )
        return _migration_session(row) if row else None

    def get_migration_session_by_request(self, request_id: str) -> MigrationSession | None:
        normalized = validate_public_identifier(request_id, field="request_id")
        row = _one(
            self.connection.execute("SELECT * FROM migration_sessions WHERE request_id = ?", (normalized,))
        )
        return _migration_session(row) if row else None

    def list_migration_sessions(self) -> list[MigrationSession]:
        rows = _dicts(self.connection.execute("SELECT * FROM migration_sessions ORDER BY created_at, migration_id"))
        return [_migration_session(row) for row in rows]

    @staticmethod
    def _require_migration_revision(current: MigrationSession, expected_revision: int) -> None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        if current.revision != expected_revision:
            raise RevisionConflictError("migration session revision conflict")

    def create_migration_session(
        self,
        *,
        migration_id: str,
        source_fingerprint: str,
        plan_version: int,
        plan_hash: str,
        source_manifest: Sequence[Mapping[str, Any]],
        choices: Mapping[str, Any],
        request_id: str,
        request_hash: str,
        backup_path: str | None = None,
        occurred_at: str | None = None,
    ) -> MigrationSession:
        migration_id = validate_public_identifier(migration_id, field="migration_id")
        request_id = validate_public_identifier(request_id, field="request_id")
        fingerprint = validate_sha256(source_fingerprint, field="source_fingerprint")
        normalized_plan_hash = validate_sha256(plan_hash, field="plan_hash")
        normalized_request_hash = validate_sha256(request_hash, field="request_hash")
        assert fingerprint and normalized_plan_hash and normalized_request_hash
        plan_version = _integer(plan_version, field="plan_version", minimum=1)
        safe_manifest = _safe_payload(list(source_manifest))
        safe_choices = _safe_payload(dict(choices))
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            existing_request = self.get_migration_session_by_request(request_id)
            if existing_request is not None:
                if (
                    existing_request.request_hash == normalized_request_hash
                    and existing_request.source_fingerprint == fingerprint
                    and existing_request.plan_hash == normalized_plan_hash
                ):
                    return existing_request
                raise RequestConflictError("migration request identity was reused with different content")
            existing = self.list_migration_sessions()
            if existing:
                same = next((item for item in existing if item.source_fingerprint == fingerprint), None)
                if same is not None and same.plan_hash == normalized_plan_hash:
                    return same
                raise MigrationStateError("a different migration session already exists")
            if self.get_data_mode() != "legacy":
                raise MigrationStateError("migration session requires legacy data mode")
            if self.connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None:
                raise MigrationStateError("migration session cannot start after projects exist")
            self.connection.execute(
                """INSERT INTO migration_sessions(
                     migration_id, source_fingerprint, plan_version, plan_hash,
                     source_manifest_json, choices_json, state, stage, revision,
                     request_id, request_hash, backup_path, backup_status,
                     created_at, started_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'backing_up', 'copy', 1, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    migration_id,
                    fingerprint,
                    plan_version,
                    normalized_plan_hash,
                    stable_json(safe_manifest),
                    stable_json(safe_choices),
                    request_id,
                    normalized_request_hash,
                    backup_path,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        created = self.get_migration_session(migration_id)
        assert created is not None
        return created

    def update_migration_stage(
        self,
        migration_id: str,
        expected_revision: int,
        *,
        state: str,
        stage: str | None,
        backup_status: str | None = None,
        backup_path: str | None = None,
        occurred_at: str | None = None,
    ) -> MigrationSession:
        timestamp = normalize_utc(occurred_at)
        target_state = str(state)
        if target_state not in MIGRATION_STATES:
            raise MigrationStateError("unknown migration state")
        if target_state in {"completed_ready", "completed_attention", "failed_rolled_back"}:
            raise MigrationStateError("use the dedicated terminal transition")
        if backup_status is not None and backup_status not in {"pending", "completed", "failed"}:
            raise MigrationStateError("invalid backup status")
        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session does not exist")
            self._require_migration_revision(current, expected_revision)
            if target_state not in _MIGRATION_TRANSITIONS[current.state]:
                raise MigrationStateError("invalid migration state transition")
            if target_state == "migrating" and (backup_status or current.backup_status) != "completed":
                raise MigrationStateError("migration cannot start before backup completes")
            reset_failure = target_state == "backing_up"
            self.connection.execute(
                """UPDATE migration_sessions
                   SET state = ?, stage = ?, backup_status = COALESCE(?, backup_status),
                       backup_path = COALESCE(?, backup_path),
                       failure_code = CASE WHEN ? THEN NULL ELSE failure_code END,
                       failure_summary = CASE WHEN ? THEN NULL ELSE failure_summary END,
                       revision = revision + 1, updated_at = ?
                   WHERE migration_id = ? AND revision = ?""",
                (
                    target_state,
                    stage,
                    backup_status,
                    backup_path,
                    reset_failure,
                    reset_failure,
                    timestamp,
                    migration_id,
                    expected_revision,
                ),
            )
        updated = self.get_migration_session(migration_id)
        assert updated is not None
        return updated

    def record_migration_failure(
        self,
        migration_id: str,
        expected_revision: int,
        *,
        failure_code: str,
        failure_summary: str,
        backup_status: str = "failed",
        occurred_at: str | None = None,
    ) -> MigrationSession:
        code = validate_public_identifier(failure_code, field="failure_code")
        summary = sanitize_persisted_text(failure_summary).strip()[:240]
        if backup_status not in {"completed", "failed"}:
            raise MigrationStateError("failed migration backup status must be completed or failed")
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session does not exist")
            self._require_migration_revision(current, expected_revision)
            if "failed_rolled_back" not in _MIGRATION_TRANSITIONS[current.state]:
                raise MigrationStateError("migration cannot fail from the current state")
            self.connection.execute(
                """UPDATE migration_sessions
                   SET state = 'failed_rolled_back', stage = 'rolled_back', backup_status = ?,
                       failure_code = ?, failure_summary = ?, revision = revision + 1, updated_at = ?
                   WHERE migration_id = ? AND revision = ?""",
                (backup_status, code, summary, timestamp, migration_id, expected_revision),
            )
        failed = self.get_migration_session(migration_id)
        assert failed is not None
        return failed

    def complete_migration_session(
        self,
        migration_id: str,
        expected_revision: int,
        *,
        project_id: str,
        report: Mapping[str, Any],
        attention_required: bool,
        occurred_at: str | None = None,
    ) -> MigrationSession:
        project_id = validate_public_identifier(project_id, field="project_id")
        safe_report = _safe_payload(dict(report))
        timestamp = normalize_utc(occurred_at)
        target = "completed_attention" if attention_required else "completed_ready"
        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session does not exist")
            self._require_migration_revision(current, expected_revision)
            if current.state != "validating":
                raise MigrationStateError("migration can only complete from validating")
            if current.backup_status != "completed" or current.backup_path is None:
                raise MigrationStateError("migration completion requires a completed backup")
            if self.get_data_mode() != "projects" or self.get_project(project_id) is None:
                raise MigrationStateError("migration completion requires the durable projects result")
            self.connection.execute(
                """UPDATE migration_sessions
                   SET state = ?, stage = 'complete', project_id = ?, report_json = ?,
                       failure_code = NULL, failure_summary = NULL, revision = revision + 1,
                       updated_at = ?, completed_at = ?
                   WHERE migration_id = ? AND revision = ?""",
                (target, project_id, stable_json(safe_report), timestamp, timestamp, migration_id, expected_revision),
            )
        completed = self.get_migration_session(migration_id)
        assert completed is not None
        return completed

    def apply_migration_transaction(
        self,
        migration_id: str,
        expected_revision: int,
        *,
        source_fingerprint: str,
        plan_hash: str,
        project_id: str,
        project_name: str,
        config: Mapping[str, Any],
        history_entries: Sequence[Mapping[str, Any]],
        safe_results: Sequence[Mapping[str, Any]],
        blocker_codes: Sequence[str],
        report: Mapping[str, Any],
        fault_injection: Callable[[str], None] | None = None,
        occurred_at: str | None = None,
    ) -> MigrationSession:
        """Atomically publish the complete legacy-to-projects business result.

        Backup and control-stage changes happen before this method. Every new
        project-mode fact, the terminal report and the data-mode switch share
        this one transaction; data_mode is deliberately written last.
        """
        migration_id = validate_public_identifier(migration_id, field="migration_id")
        project_id = validate_public_identifier(project_id, field="project_id")
        fingerprint = validate_sha256(source_fingerprint, field="source_fingerprint")
        normalized_plan_hash = validate_sha256(plan_hash, field="plan_hash")
        validated_config = validate_project_config(config)
        safe_report = _safe_payload(dict(report))
        timestamp = normalize_utc(occurred_at)
        blockers = tuple(sorted({validate_public_identifier(code, field="blocker_code") for code in blocker_codes}))
        activation_state = "inactive" if blockers else "active"
        readiness_state = "blocked" if blockers else "ready"
        auto_scan_state = (
            "scheduled"
            if not blockers and bool(validated_config["schedule"]["enabled"])
            else ("blocked" if blockers else "off")
        )

        def inject(phase: str) -> None:
            if fault_injection is not None:
                fault_injection(phase)

        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session does not exist")
            self._require_migration_revision(current, expected_revision)
            if current.state != "validating" or current.stage not in {"database", "runtime"}:
                raise MigrationStateError("migration apply requires the validating database stage")
            if current.source_fingerprint != fingerprint or current.plan_hash != normalized_plan_hash:
                raise MigrationStateError("migration apply no longer matches its durable plan")
            if current.backup_status != "completed" or current.backup_path is None:
                raise MigrationStateError("migration apply requires a completed backup")
            if self.get_data_mode() != "legacy":
                raise MigrationStateError("migration apply requires legacy data mode")
            if self.connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None:
                raise MigrationStateError("migration apply requires an empty project store")

            schema_version = int(validated_config["schema_version"])
            activated_at = timestamp if activation_state == "active" else None
            self.connection.execute(
                """INSERT INTO projects(
                     project_id, name, description, activation_state, current_config_revision,
                     created_at, updated_at, activated_at, paused_at
                   ) VALUES (?, ?, '', ?, 1, ?, ?, ?, NULL)""",
                (project_id, project_name, activation_state, timestamp, timestamp, activated_at),
            )
            self.connection.execute(
                """INSERT INTO project_config_revisions(
                     project_id, revision, config_json, schema_version, created_at
                   ) VALUES (?, 1, ?, ?, ?)""",
                (project_id, stable_json(validated_config), schema_version, timestamp),
            )
            self.connection.execute(
                """INSERT INTO project_runtime(
                     project_id, readiness_state, auto_scan_state, failure_code,
                     failure_summary, discovery_baseline, first_scan_state
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    project_id,
                    readiness_state,
                    auto_scan_state,
                    blockers[0] if blockers else None,
                    "迁移完成后需要修复项目条件" if blockers else None,
                    timestamp,
                ),
            )
            self.connection.execute(
                """INSERT INTO workspace_events(event_type, project_id, occurred_at, payload_json)
                   VALUES ('project_created', ?, ?, ?)""",
                (project_id, timestamp, stable_json({"source": "legacy_migration"})),
            )
            inject("after_project")

            run_ids: dict[str, str] = {}
            for entry in history_entries:
                if entry.get("category") == "quarantined":
                    continue
                legacy_run_id = str(entry["legacy_run_id"])
                run_id = legacy_id(fingerprint, f"run:{legacy_run_id}")
                run_ids[legacy_run_id] = run_id
                target_state = str(entry["target_state"])
                if target_state not in {"completed", "failed"}:
                    raise MigrationStateError("imported history cannot become queued work")
                created_at = normalize_utc(str(entry["created_at"]))
                updated_at = normalize_utc(str(entry["updated_at"]))
                error_code = None
                error_summary = None
                if target_state == "failed":
                    error_code = str(
                        entry.get("failure_code")
                        or ("legacy_compatibility_history" if entry.get("category") == "compatibility" else "legacy_import_failed")
                    )
                    error_summary = "历史处理记录已保留，且不会进入处理队列"
                self.connection.execute(
                    """INSERT INTO runs(
                         run_id, project_id, content_id, processing_sequence, origin_run_id,
                         source_scan_id, trigger_source, first_seen_path, latest_seen_path,
                         status, current_stage, config_revision, parameter_snapshot_json,
                         queued_at, started_at, completed_at, updated_at, error_code, error_summary
                       ) VALUES (?, ?, ?, 1, NULL, NULL, 'legacy_import', ?, ?, ?, NULL, 1,
                         ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        project_id,
                        str(entry["content_id"]),
                        str(entry["source_identity"]),
                        str(entry["source_identity"]),
                        target_state,
                        stable_json(
                            {
                                "migration": {"source_fingerprint": fingerprint, "legacy_run_id": legacy_run_id},
                                "output": {"directory": validated_config["output"]["directory"]},
                            }
                        ),
                        created_at,
                        created_at,
                        updated_at if target_state == "completed" else None,
                        updated_at,
                        error_code,
                        error_summary,
                    ),
                )
            inject("after_runs")

            for fact in safe_results:
                legacy_run_id = str(fact["legacy_run_id"])
                run_id = run_ids.get(legacy_run_id)
                if run_id is None:
                    raise MigrationStateError("safe result does not belong to imported history")
                review_id = legacy_id(fingerprint, f"review:{legacy_run_id}")
                candidate_id = legacy_id(fingerprint, f"candidate:{legacy_run_id}")
                output_id = legacy_id(fingerprint, f"output:{legacy_run_id}")
                decision_id = legacy_id(fingerprint, f"decision:{legacy_run_id}")
                material_id = legacy_id(fingerprint, f"material:{legacy_run_id}")
                duration_ms = int(fact["duration_ms"])
                self.connection.execute(
                    """INSERT INTO ai_review_sessions(
                         review_session_id, run_id, attempt_number, status, resource_ref, model_name,
                         strategy_version, config_revision, parameter_snapshot_json, format_version,
                         overall_summary, warnings_json, candidate_count, selected_count, rejected_count,
                         evidence_relative_path, evidence_sha256, started_at, completed_at, validated_at, updated_at
                       ) VALUES (?, ?, 1, 'selected', 'legacy.analysis.default', 'legacy', 'migration_v1',
                         1, '{}', 1, '历史安全结果', '[]', 1, 1, 0, NULL, ?, ?, ?, ?, ?)""",
                    (review_id, run_id, str(fact["sha256"]), timestamp, timestamp, timestamp, timestamp),
                )
                self.connection.execute(
                    """INSERT INTO run_outputs(
                         output_id, run_id, review_session_id, candidate_id, display_order, status,
                         storage_kind, relative_path, file_name, duration_ms, width, height, container,
                         video_codec, byte_size, generated_at, verified_at, updated_at
                       ) VALUES (?, ?, ?, ?, 1, 'ready', 'project_output', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        output_id,
                        run_id,
                        review_id,
                        candidate_id,
                        str(fact["relative_path"]),
                        str(fact["file_name"]),
                        duration_ms,
                        int(fact["width"]),
                        int(fact["height"]),
                        str(fact["container"]),
                        str(fact["video_codec"]),
                        int(fact["byte_size"]),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                self.connection.execute(
                    """INSERT INTO candidate_decisions(
                         decision_id, review_session_id, run_id, candidate_id, decision, rank,
                         candidate_type, source_start_ms, source_end_ms, selected_start_ms,
                         selected_end_ms, remove_ranges_json, hook, core_value, reason,
                         risks_json, transcript_excerpt, output_id
                       ) VALUES (?, ?, ?, ?, 'selected', 1, 'legacy_safe_result', 0, ?, 0, ?,
                         '[]', '', '', '历史安全结果', '[]', '', ?)""",
                    (decision_id, review_id, run_id, candidate_id, duration_ms, duration_ms, output_id),
                )
                self.connection.execute(
                    """INSERT INTO output_materials(
                         material_id, output_id, title_candidates_json, preferred_title_id,
                         description, tags_json, generation_source, status, material_revision,
                         created_at, updated_at
                       ) VALUES (?, ?, '[]', NULL, '', '[]', 'indexed_v1', 'pending', 1, ?, ?)""",
                    (material_id, output_id, timestamp, timestamp),
                )
                self.connection.execute(
                    """INSERT INTO run_results(
                         run_id, review_session_id, result_type, candidate_count, selected_count,
                         rejected_count, available_output_count, failed_output_count, total_duration_ms,
                         overall_summary, warnings_json, format_version, result_revision, source_kind,
                         evidence_hash, completed_at, updated_at
                       ) VALUES (?, ?, 'clips_ready', 1, 1, 0, 1, 0, ?, '历史安全结果', '[]',
                         1, 1, 'indexed_v1', ?, ?, ?)""",
                    (run_id, review_id, duration_ms, str(fact["sha256"]), timestamp, timestamp),
                )
            inject("after_results")

            for code in blockers:
                self._discover_issue_in_transaction(
                    issue_code=code,
                    category="project",
                    scope_type="project",
                    project_id=project_id,
                    run_id=None,
                    output_id=None,
                    material_id=None,
                    issue_group_key="migration-readiness",
                    status="action_required",
                    impact_level="blocking",
                    title="迁移后项目条件需要修复",
                    summary=f"迁移计划记录了待修复条件：{code}",
                    impact="项目不会自动扫描或创建新 Run",
                    preserved_content="历史记录与备份已安全保留",
                    next_step="进入项目问题页完成修复",
                    recovery_capability="operational_repair",
                    occurred_at=timestamp,
                    issue_id=legacy_id(fingerprint, f"issue:readiness:{code}"),
                )
            inject("after_issues")

            target = "completed_attention" if blockers else "completed_ready"
            self.connection.execute(
                """UPDATE migration_sessions
                   SET state = ?, stage = NULL, project_id = ?, report_json = ?,
                       failure_code = NULL, failure_summary = NULL, revision = revision + 1,
                       updated_at = ?, completed_at = ?
                   WHERE migration_id = ? AND revision = ?""",
                (
                    target,
                    project_id,
                    stable_json(safe_report),
                    timestamp,
                    timestamp,
                    migration_id,
                    expected_revision,
                ),
            )
            inject("before_mode")
            self.connection.execute(
                """INSERT INTO system_state(key, value, updated_at) VALUES ('data_mode', 'projects', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (timestamp,),
            )
            inject("after_mode")
        completed = self.get_migration_session(migration_id)
        assert completed is not None
        return completed

    def acknowledge_migration_session(
        self, migration_id: str, expected_revision: int, *, occurred_at: str | None = None
    ) -> MigrationSession:
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session does not exist")
            self._require_migration_revision(current, expected_revision)
            if current.state not in {"completed_ready", "completed_attention"}:
                raise MigrationStateError("only a completed migration can be acknowledged")
            if current.acknowledged_at is not None:
                return current
            self.connection.execute(
                """UPDATE migration_sessions
                   SET acknowledged_at = ?, revision = revision + 1, updated_at = ?
                   WHERE migration_id = ? AND revision = ?""",
                (timestamp, timestamp, migration_id, expected_revision),
            )
        acknowledged = self.get_migration_session(migration_id)
        assert acknowledged is not None
        return acknowledged

    def mark_completed_migration_attention(
        self,
        migration_id: str,
        expected_revision: int,
        *,
        failure_code: str,
        occurred_at: str | None = None,
    ) -> MigrationSession:
        code = validate_public_identifier(failure_code, field="failure_code")
        timestamp = normalize_utc(occurred_at)
        with self.transaction():
            current = self.get_migration_session(migration_id)
            if current is None or current.project_id is None or current.report is None:
                raise MigrationStateError("completed migration result is unavailable")
            self._require_migration_revision(current, expected_revision)
            if current.state != "completed_ready":
                raise MigrationStateError("only a ready migration can become attention")
            blocker_codes = sorted(
                {str(item) for item in current.report.get("blocker_codes", [])} | {code}
            )
            report = {
                **current.report,
                "readiness": "attention",
                "blocker_count": len(blocker_codes),
                "blocker_codes": blocker_codes,
            }
            self.connection.execute(
                """UPDATE projects SET activation_state = 'inactive', activated_at = NULL,
                     updated_at = ? WHERE project_id = ?""",
                (timestamp, current.project_id),
            )
            self.connection.execute(
                """UPDATE project_runtime SET readiness_state = 'blocked', auto_scan_state = 'blocked',
                     failure_code = ?, failure_summary = ? WHERE project_id = ?""",
                (code, "迁移完成，但项目运行服务尚未就绪", current.project_id),
            )
            self._discover_issue_in_transaction(
                issue_code=code,
                category="project",
                scope_type="project",
                project_id=current.project_id,
                run_id=None,
                output_id=None,
                material_id=None,
                issue_group_key="migration-runtime-readiness",
                status="action_required",
                impact_level="blocking",
                title="迁移完成后服务尚未就绪",
                summary="项目数据已安全迁移，但运行服务需要修复",
                impact="项目不会自动扫描或创建新 Run",
                preserved_content="迁移项目、历史和备份均已保留",
                next_step="进入项目问题页完成运行环境修复",
                recovery_capability="operational_repair",
                occurred_at=timestamp,
                issue_id=legacy_id(current.source_fingerprint, "issue:migration-runtime-readiness"),
            )
            self.connection.execute(
                """UPDATE migration_sessions SET state = 'completed_attention', report_json = ?,
                     revision = revision + 1, updated_at = ? WHERE migration_id = ? AND revision = ?""",
                (stable_json(report), timestamp, migration_id, expected_revision),
            )
        attention = self.get_migration_session(migration_id)
        assert attention is not None
        return attention

    def execute_many_in_transaction(self, statement: str, parameters: Sequence[Sequence[Any]]) -> None:
        with self.transaction():
            self.connection.executemany(statement, parameters)


# Explicit compatibility names for callers that describe the repository by its layer.
ProjectStorage = ProjectRepository
open_database = connect_database
