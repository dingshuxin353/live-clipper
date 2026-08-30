from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .first_run_state import (
    FirstRunSession,
    StartupDecision,
    StartupDetection,
    decide_startup,
    normalize_first_run_draft,
)
from .project_storage import database_path

LEGACY_METADATA_FILES = ("runs.json", "service.json", "scheduler.json", "scheduler_runs.json", "events.jsonl")
LEGACY_ONBOARDING_MARKER = "onboarding.json"


@dataclass(frozen=True)
class _DatabaseFacts:
    data_mode: str = "absent"
    project_ids: tuple[str, ...] = ()
    session: FirstRunSession | None = None
    has_legacy_import: bool = False
    unreadable: bool = False


def _configured_global_source(config_path: Path) -> tuple[bool, bool]:
    if not config_path.is_file():
        return False, False
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False, True
    recording = payload.get("recording_source")
    if not isinstance(recording, dict):
        return False, False
    values: list[Any] = [recording.get("source_dir")]
    default = recording.get("default")
    if isinstance(default, dict):
        values.append(default.get("source_dir"))
    return any(isinstance(value, str) and bool(value.strip()) for value in values), False


def _session_from_row(row: sqlite3.Row) -> FirstRunSession:
    draft = json.loads(str(row["draft_json"]))
    if not isinstance(draft, dict):
        raise ValueError("invalid first-run draft")
    return FirstRunSession(
        session_id=str(row["session_id"]),
        state=str(row["state"]),
        current_step=str(row["current_step"]),
        revision=int(row["revision"]),
        draft=normalize_first_run_draft(draft),
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


def _read_database_facts(path: Path) -> _DatabaseFacts:
    if not path.is_file():
        return _DatabaseFacts()
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        data_mode = "absent"
        if "system_state" in tables:
            row = connection.execute("SELECT value FROM system_state WHERE key = 'data_mode'").fetchone()
            if row is not None:
                data_mode = str(row[0])
        project_ids: tuple[str, ...] = ()
        if "projects" in tables:
            project_ids = tuple(
                str(row[0]) for row in connection.execute("SELECT project_id FROM projects ORDER BY project_id")
            )
        session = None
        if "first_run_sessions" in tables:
            row = connection.execute(
                "SELECT * FROM first_run_sessions WHERE session_id = 'primary'"
            ).fetchone()
            if row is not None:
                session = _session_from_row(row)
        has_legacy_import = False
        if "legacy_imports" in tables:
            has_legacy_import = connection.execute("SELECT 1 FROM legacy_imports LIMIT 1").fetchone() is not None
        return _DatabaseFacts(data_mode, project_ids, session, has_legacy_import)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
        return _DatabaseFacts(unreadable=True)
    finally:
        if "connection" in locals():
            connection.close()


def _is_current_project_runtime(service: Path, facts: _DatabaseFacts) -> bool:
    """Recognize metadata written by the current project-mode service.

    The service and scheduler intentionally retain their historical filenames,
    but those files are not legacy evidence once the authoritative database is
    in projects mode and ``service.json`` carries the provenance marker. A
    legacy ``runs.json`` remains an independent signal so mixed installations
    still route to migration/diagnostics.
    """
    if facts.data_mode != "projects":
        return False
    try:
        payload = json.loads((service / "service.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("runtime_mode") == "projects"


def _inspect(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> tuple[StartupDetection, _DatabaseFacts]:
    config = Path(config_path).expanduser().resolve()
    Path(env_path).expanduser().resolve()  # Explicit boundary; secret contents are intentionally never read.
    service = Path(service_dir).expanduser().resolve()
    evidence: set[str] = set()
    if (service / LEGACY_ONBOARDING_MARKER).exists():
        evidence.add("legacy_onboarding_marker")
    configured_source, unreadable_config = _configured_global_source(config)
    if configured_source:
        evidence.add("legacy_global_source_configured")
    if unreadable_config:
        evidence.add("legacy_config_unreadable")

    db_path = database_path(service)
    facts = _read_database_facts(db_path)
    current_project_runtime = _is_current_project_runtime(service, facts)
    if any((service / name).exists() for name in LEGACY_METADATA_FILES) and (
        not current_project_runtime or (service / "runs.json").exists()
    ):
        evidence.add("legacy_metadata")
    if facts.has_legacy_import:
        evidence.add("legacy_project_import")
    if facts.data_mode == "legacy":
        evidence.add("legacy_data_mode")
    if facts.unreadable:
        evidence.add("project_database_unreadable")
    detection = StartupDetection(
        has_legacy_evidence=bool(evidence),
        evidence_codes=tuple(sorted(evidence)),
        has_project_database=db_path.is_file(),
        data_mode=facts.data_mode,
        project_count=len(facts.project_ids),
        has_first_run_session=facts.session is not None,
    )
    return detection, facts


def detect_first_run_environment(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> StartupDetection:
    detection, _facts = _inspect(config_path=config_path, env_path=env_path, service_dir=service_dir)
    return detection


def inspect_startup(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> StartupDecision:
    detection, facts = _inspect(config_path=config_path, env_path=env_path, service_dir=service_dir)
    return decide_startup(detection, session=facts.session, existing_project_ids=facts.project_ids)
