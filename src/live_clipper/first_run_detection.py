from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .first_run_state import (
    FirstRunSession,
    MigrationStartupSession,
    StartupDecision,
    StartupDetection,
    decide_startup,
    normalize_first_run_draft,
)
from .project_domain import normalize_utc
from .project_migration import (
    LegacySourceError,
    MigrationError,
    build_migration_plan,
    inspect_legacy_state,
    migration_completion_consistent,
    migration_plan_payload,
    migration_session_payload,
)
from .project_storage import MigrationSession, _migration_session, database_path

LEGACY_METADATA_FILES = ("runs.json", "service.json", "scheduler.json", "scheduler_runs.json", "events.jsonl")
LEGACY_ONBOARDING_MARKER = "onboarding.json"


@dataclass(frozen=True)
class _DatabaseFacts:
    data_mode: str = "absent"
    project_ids: tuple[str, ...] = ()
    session: FirstRunSession | None = None
    migration_sessions: tuple[MigrationStartupSession, ...] = ()
    has_blocked_legacy_import: bool = False
    unreadable: bool = False
    migration_record: MigrationSession | None = None
    first_project: dict[str, Any] | None = None
    resource_migration_state: str | None = None


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


def _migration_from_row(row: sqlite3.Row) -> MigrationStartupSession:
    return MigrationStartupSession(
        migration_id=str(row["migration_id"]),
        state=str(row["state"]),
        revision=int(row["revision"]),
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        backup_status=str(row["backup_status"]),
        has_report=row["report_json"] is not None,
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        acknowledged_at=str(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None,
    )


@contextmanager
def readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    deadline = time.monotonic() + 1.0

    def check_budget() -> None:
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("readonly database time budget exceeded")

    # SQLite owns its WAL/SHM; never repair or remove them during a read.
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("unsafe database path")
    for suffix in ("-wal", "-shm", "-journal"):
        auxiliary = path.with_name(path.name + suffix)
        try:
            info = auxiliary.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError("unsafe database auxiliary path")
    check_budget()
    connection = sqlite3.connect(
        path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=0,
    )
    try:
        check_budget()
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms < 1:
                raise sqlite3.OperationalError("readonly database time budget exceeded")
            connection.execute(f"PRAGMA busy_timeout = {min(50, remaining_ms)}")
            try:
                # Establish one snapshot before any caller reads its related facts.
                connection.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchall()
                break
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", 0) & 0xff != sqlite3.SQLITE_BUSY:
                    raise
        check_budget()
        connection.execute("PRAGMA busy_timeout = 0")
        yield connection
        check_budget()
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError("database identity changed")
    finally:
        try:
            connection.set_progress_handler(None, 0)
        finally:
            connection.close()


def _read_database_facts(path: Path) -> _DatabaseFacts:
    try:
        path.lstat()
    except FileNotFoundError:
        return _DatabaseFacts()
    except OSError:
        return _DatabaseFacts(unreadable=True)
    try:
        with readonly_database(path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            resource_state = None
            data_mode = "absent"
            if "system_state" in tables:
                row = connection.execute("SELECT value FROM system_state WHERE key = 'data_mode'").fetchone()
                if row is not None:
                    data_mode = str(row[0])
                resource_row = connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
                resource_state = str(resource_row[0]) if resource_row else None
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
            migration_sessions: tuple[MigrationStartupSession, ...] = ()
            if "migration_sessions" in tables:
                rows = connection.execute(
                    "SELECT * FROM migration_sessions ORDER BY created_at, migration_id"
                ).fetchall()
                migration_sessions = tuple(_migration_from_row(row) for row in rows)
            has_blocked_legacy_import = False
            if "migration_sessions" not in tables and "legacy_imports" in tables:
                has_blocked_legacy_import = (
                    connection.execute("SELECT 1 FROM legacy_imports LIMIT 1").fetchone() is not None
                )
            first_project = None
            if session and session.first_project_id and "project_runtime" in tables:
                row = connection.execute(
                    "SELECT p.project_id,p.name,p.activation_state,r.readiness_state FROM projects p "
                    "LEFT JOIN project_runtime r ON r.project_id=p.project_id WHERE p.project_id=?",
                    (session.first_project_id,),
                ).fetchone()
                if row:
                    first_project = dict(row)
                    first_project["readiness_state"] = row["readiness_state"] or "blocked"
            return _DatabaseFacts(
                data_mode, project_ids, session, migration_sessions, has_blocked_legacy_import,
                migration_record=_migration_session(dict(rows[0])) if migration_sessions else None,
                first_project=first_project, resource_migration_state=resource_state,
            )
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, RuntimeError, KeyError):
        return _DatabaseFacts(unreadable=True)


def _is_current_project_runtime(service: Path, facts: _DatabaseFacts) -> bool:
    """Recognize metadata written by the current project-mode service.

    The service and scheduler intentionally retain their historical filenames,
    but those files are not legacy evidence once the authoritative database is
    in projects mode and ``service.json`` carries the provenance marker. A
    legacy ``runs.json`` remains independent evidence unless verified as a
    strictly empty index; mixed installations still require diagnosis.
    """
    if facts.data_mode != "projects":
        return False
    try:
        payload = json.loads((service / "service.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("runtime_mode") == "projects"


def _runs_are_legacy(path: Path, *, current_project_runtime: bool) -> bool:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not current_project_runtime or not stat.S_ISREG(before.st_mode):
        return True

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if identity(opened) != identity(before):
                return True
            payload = json.loads(stream.read().decode("utf-8"), object_pairs_hook=unique_object)
            if identity(os.fstat(stream.fileno())) != identity(before):
                return True
        if identity(path.lstat()) != identity(before):
            return True
    except (OSError, UnicodeError, ValueError, RecursionError):
        return True
    return not (isinstance(payload, dict) and set(payload) == {"runs"} and payload["runs"] == [])


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
    if _runs_are_legacy(service / "runs.json", current_project_runtime=current_project_runtime) or (
        not current_project_runtime and any((service / name).exists() for name in LEGACY_METADATA_FILES)
    ):
        evidence.add("legacy_metadata")
    if facts.has_blocked_legacy_import:
        evidence.add("legacy_import_state_requires_diagnostic")
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
        migration_session_count=len(facts.migration_sessions),
        resource_migration_incomplete=bool(facts.migration_sessions) and facts.resource_migration_state != "completed",
    )
    return detection, facts


@dataclass(frozen=True)
class StartupInspection:
    decision: StartupDecision
    detection: StartupDetection
    facts: _DatabaseFacts
    config_path: Path
    service_dir: Path

    @property
    def entry(self) -> dict[str, Any]:
        return {
            "mode": self.decision.entry,
            "onboarding": self.decision.onboarding,
            "reason_code": self.decision.reason_code,
            "evidence_codes": list(self.detection.evidence_codes),
        }

    @property
    def restriction(self) -> str | None:
        if self.decision.entry in {"migration_required", "diagnostic_required"}:
            return self.decision.entry
        return None

    def migration_snapshot(self) -> dict[str, Any]:
        facts = self.facts
        if facts.unreadable:
            raise MigrationError("diagnostic_required", "无法读取升级状态，请记录问题编号以便排查。")
        session = facts.migration_record
        report = ({**session.report, "acknowledged_at": session.acknowledged_at} if session and session.report else None)
        if session is not None:
            incomplete = facts.data_mode == "projects" and facts.resource_migration_state != "completed"
            if incomplete:
                entry = "executing" if session.state == "validating" and session.stage == "resources" else "incomplete"
                report = None
            elif session.state.startswith("completed_"):
                entry = "completed"
            elif session.state == "failed_rolled_back":
                entry = "failed"
            elif session.state == "diagnostic_required":
                entry = "diagnostic"
            else:
                entry = "executing"
            source = {
                "detected": True,
                "checked_at": session.updated_at,
                "display_summary": {"metadata_file_count": len(session.source_manifest)},
            }
            plan = None
            if session.state.startswith("completed_"):
                consistent = migration_completion_consistent(facts.data_mode, facts.project_ids, session)
                if not consistent:
                    entry = "diagnostic"
        else:
            try:
                inspection = inspect_legacy_state(self.service_dir, config_path=self.config_path)
            except LegacySourceError as exc:
                raise MigrationError(exc.code, "无法读取旧版数据，请记录问题编号以便排查。", status=409) from exc
            plan_object = build_migration_plan(inspection, backup_root=self.service_dir.parent / "migration-backups")
            entry = "review" if not plan_object.requires_user_choices else "inspect"
            source = {
                "detected": True,
                "checked_at": normalize_utc(),
                "display_summary": {
                    "metadata_file_count": len(inspection.source_manifest),
                    "history_count": len(inspection.runs),
                },
            }
            plan = migration_plan_payload(plan_object)
        payload = {
            "ok": True,
            "entry": entry,
            "source": source,
            "plan": plan,
            "session": migration_session_payload(session) if session else None,
            "report": report,
        }
        assert "source_manifest" not in json.dumps(payload, ensure_ascii=False)
        return payload

    def migration_summary(self) -> dict[str, Any] | None:
        if self.decision.entry not in {"migration_required", "diagnostic_required", "workbench"}:
            return None
        if self.facts.unreadable:
            return {"entry": "diagnostic", "session": None, "report": None}
        if not self.restriction and not self.detection.resource_migration_incomplete:
            session = self.facts.migration_record
            if session is None or not session.state.startswith("completed_") or session.acknowledged_at is not None:
                return None
        try:
            snapshot = self.migration_snapshot()
        except MigrationError:
            return {"entry": "diagnostic", "session": None, "report": None}
        return {"entry": snapshot["entry"], "session": snapshot["session"], "report": snapshot["report"]}


def inspect_startup_state(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> StartupInspection:
    detection, facts = _inspect(config_path=config_path, env_path=env_path, service_dir=service_dir)
    decision = decide_startup(
        detection,
        session=facts.session,
        existing_project_ids=facts.project_ids,
        migration_sessions=facts.migration_sessions,
    )
    return StartupInspection(
        decision, detection, facts, Path(config_path).expanduser().resolve(), Path(service_dir).expanduser().resolve(),
    )


def detect_first_run_environment(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> StartupDetection:
    return inspect_startup_state(config_path=config_path, env_path=env_path, service_dir=service_dir).detection


def inspect_startup(
    *, config_path: str | Path, env_path: str | Path, service_dir: str | Path
) -> StartupDecision:
    return inspect_startup_state(config_path=config_path, env_path=env_path, service_dir=service_dir).decision
