from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from live_clipper.project_storage import SCHEMA_V1, SCHEMA_V2, ProjectRepository, initialize_schema


def _v2_database(service_dir: Path) -> sqlite3.Connection:
    service_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(service_dir / "venus.sqlite3", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.create_function("migration_fault", 1, lambda _phase: 0)
    connection.executescript(
        f"""BEGIN IMMEDIATE;
{SCHEMA_V1}
INSERT INTO schema_migrations VALUES (1, 'project foundation v1', '2026-08-29T00:00:00Z');
INSERT INTO system_state VALUES ('data_mode', 'projects', '2026-08-29T00:00:00Z');
{SCHEMA_V2}
INSERT INTO schema_migrations VALUES (2, 'result and issue foundation v2', '2026-08-29T00:00:01Z');
COMMIT;
"""
    )
    connection.create_function("migration_fault", 1, None)
    connection.executescript(
        """BEGIN IMMEDIATE;
INSERT INTO projects VALUES (
  'project-v2', 'Existing v2', '', 'inactive', 1,
  '2026-08-29T00:00:02Z', '2026-08-29T00:00:02Z', NULL, NULL
);
INSERT INTO project_config_revisions VALUES (
  'project-v2', 1, '{}', 1, '2026-08-29T00:00:02Z'
);
INSERT INTO project_runtime(project_id, readiness_state, auto_scan_state, first_scan_state)
VALUES ('project-v2', 'ready', 'off', 'not_required');
COMMIT;
"""
    )
    return connection


def _logical_dump(connection: sqlite3.Connection) -> str:
    return "\n".join(connection.iterdump())


def test_v2_upgrades_to_current_schema_without_rebuilding_result_tables(tmp_path):
    connection = _v2_database(tmp_path)
    result_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run_results'"
    ).fetchone()[0]
    initialize_schema(connection)

    assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
        (1,),
        (2,),
        (3,),
        (4,),
    ]
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run_results'"
    ).fetchone()[0] == result_sql
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'first_run_sessions'"
    ).fetchone()[0]
    assert "session_id = 'primary'" in table_sql
    assert "activation_pending" in table_sql
    assert connection.execute("PRAGMA foreign_key_list(first_run_sessions)").fetchall() == []
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "phase",
    ["before_first_run_table", "after_first_run_table", "after_first_run_version"],
)
def test_v3_fault_injection_rolls_back_to_identical_v2(phase, tmp_path):
    connection = _v2_database(tmp_path)
    before = _logical_dump(connection)

    def fail(current: str) -> None:
        if current == phase:
            raise RuntimeError("fault injection")

    with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
        initialize_schema(connection, migration_fault=fail)

    assert _logical_dump(connection) == before
    assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'first_run_sessions'"
    ).fetchone() is None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_first_run_session_is_singleton_and_survives_reopen(tmp_path):
    repository = ProjectRepository(tmp_path)
    repository.set_data_mode("projects")
    session = repository.begin_first_run_session(started_at="2026-08-29T00:00:00Z")
    assert session.session_id == "primary"
    assert session.state == "in_progress"
    assert session.current_step == "welcome"
    assert session.revision == 1
    repository.close()

    reopened = ProjectRepository(tmp_path)
    assert reopened.get_first_run_session() == session
    with pytest.raises(sqlite3.IntegrityError):
        reopened.connection.execute(
            """INSERT INTO first_run_sessions(
                 session_id, state, current_step, revision, draft_json, started_at, updated_at
               ) VALUES ('secondary', 'in_progress', 'welcome', 1, '{}', ?, ?)""",
            ("2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
        )
