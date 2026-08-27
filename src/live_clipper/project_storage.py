from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .project_domain import (
    NormalRunCreationResult,
    Project,
    ProjectConfigRevision,
    ProjectRuntime,
    Run,
    RunStageEvent,
    ScanEvent,
    WorkspaceEvent,
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

SCHEMA_VERSION = 2


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
        [int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")]
        if table_exists
        else []
    )
    if any(version > SCHEMA_VERSION for version in versions):
        raise RuntimeError("database schema is newer than this application")
    if SCHEMA_VERSION in versions:
        return

    def inject(phase: str) -> int:
        if migration_fault is not None:
            migration_fault(phase)
        return 0

    connection.create_function("migration_fault", 1, inject)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            f"""BEGIN IMMEDIATE;
{SCHEMA_V1}
INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (1, 'project foundation v1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
INSERT OR IGNORE INTO system_state(key, value, updated_at)
VALUES ('data_mode', 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
{SCHEMA_V2}
INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, 'result and issue foundation v2', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
COMMIT;
"""
        )
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
                "checking": {"action_required", "ready_to_recover"},
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

    def get_legacy_import(self, source_fingerprint: str) -> dict[str, Any] | None:
        row = _one(
            self.connection.execute(
                "SELECT * FROM legacy_imports WHERE source_fingerprint = ?", (source_fingerprint,)
            )
        )
        if row is not None:
            row["plan"] = parse_json(row.pop("plan_json"))
            row["summary"] = parse_json(row.pop("summary_json"))
        return row

    def apply_legacy_import(
        self,
        *,
        source_fingerprint: str,
        import_id: str,
        project_id: str,
        project_name: str,
        project_config: Mapping[str, Any],
        runs: Sequence[Mapping[str, Any]],
        plan_summary: Mapping[str, Any],
        backup_path: str,
        occurred_at: str,
    ) -> tuple[bool, str]:
        """Atomically persist one already-backed-up legacy plan without switching data mode."""
        validated = validate_project_config(project_config)
        occurred_at = normalize_utc(occurred_at)
        with self.transaction():
            existing = _one(
                self.connection.execute(
                    "SELECT summary_json FROM legacy_imports WHERE source_fingerprint = ?",
                    (source_fingerprint,),
                )
            )
            if existing is not None:
                summary = parse_json(existing["summary_json"])
                return False, str(summary["project_id"])
            self.connection.execute(
                """INSERT INTO projects(
                     project_id, name, description, activation_state, current_config_revision,
                     created_at, updated_at
                   ) VALUES (?, ?, '', 'inactive', 1, ?, ?)""",
                (project_id, project_name, occurred_at, occurred_at),
            )
            self.connection.execute(
                "INSERT INTO project_config_revisions VALUES (?, 1, ?, 1, ?)",
                (project_id, stable_json(validated), occurred_at),
            )
            self.connection.execute(
                """INSERT INTO project_runtime(
                     project_id, readiness_state, auto_scan_state, first_scan_state
                   ) VALUES (?, 'ready', 'off', 'not_required')""",
                (project_id,),
            )
            for item in runs:
                self.connection.execute(
                    """INSERT INTO runs(
                         run_id, project_id, content_id, processing_sequence, origin_run_id,
                         source_scan_id, trigger_source, first_seen_path, latest_seen_path,
                         status, current_stage, config_revision, parameter_snapshot_json,
                         queued_at, started_at, review_at, completed_at, updated_at,
                         error_code, error_summary
                       ) VALUES (?, ?, ?, 1, NULL, NULL, 'legacy_import', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["run_id"],
                        project_id,
                        item["content_id"],
                        item["first_seen_path"],
                        item["latest_seen_path"],
                        item["status"],
                        item.get("current_stage"),
                        stable_json(item["parameter_snapshot"]),
                        item["queued_at"],
                        item.get("started_at"),
                        item.get("review_at"),
                        item.get("completed_at"),
                        item["updated_at"],
                        item.get("error_code"),
                        item.get("error_summary"),
                    ),
                )
            summary = {**plan_summary, "project_id": project_id, "imported_run_count": len(runs)}
            self.connection.execute(
                """INSERT INTO legacy_imports(
                     import_id, source_fingerprint, plan_json, backup_path, status,
                     summary_json, failure_summary, created_at, completed_at
                   ) VALUES (?, ?, ?, ?, 'completed', ?, NULL, ?, ?)""",
                (
                    import_id,
                    source_fingerprint,
                    stable_json(plan_summary),
                    backup_path,
                    stable_json(summary),
                    occurred_at,
                    occurred_at,
                ),
            )
        return True, project_id

    def execute_many_in_transaction(self, statement: str, parameters: Sequence[Sequence[Any]]) -> None:
        with self.transaction():
            self.connection.executemany(statement, parameters)


# Explicit compatibility names for callers that describe the repository by its layer.
ProjectStorage = ProjectRepository
open_database = connect_database
