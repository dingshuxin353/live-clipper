from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
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
    utc_now,
    validate_project_config,
)

SCHEMA_VERSION = 1


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


def connect_database(service_dir: str | Path) -> sqlite3.Connection:
    path = database_path(service_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_V1}")
        versions = [int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")]
        if any(version > SCHEMA_VERSION for version in versions):
            raise RuntimeError("database schema is newer than this application")
        now = utc_now()
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (1, 'project foundation v1', ?)",
            (now,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_state(key, value, updated_at) VALUES ('data_mode', 'legacy', ?)",
            (now,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    rows = _dicts(cursor)
    return rows[0] if rows else None


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
                   ) VALUES (?, 1, ?, 1, ?)""",
                (project_id, stable_json(validated), timestamp),
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
                "INSERT INTO project_config_revisions VALUES (?, ?, ?, 1, ?)",
                (project_id, revision, stable_json(validated), timestamp),
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
