from __future__ import annotations

import sqlite3

import pytest

from live_clipper.project_domain import default_project_config
from live_clipper.project_result_domain import RevisionConflictError
from live_clipper.project_storage import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    MigrationSchemaError,
    MigrationStateError,
    ProjectRepository,
    initialize_schema,
)


def _v3_database(path):
    connection = sqlite3.connect(path / "venus.sqlite3", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.create_function("migration_fault", 1, lambda _phase: 0)
    connection.executescript(
        "BEGIN;"
        + SCHEMA_V1
        + "INSERT INTO schema_migrations VALUES (1, 'v1', '2026-09-01T00:00:00Z');"
        + "INSERT INTO system_state VALUES ('data_mode', 'legacy', '2026-09-01T00:00:00Z');"
        + SCHEMA_V2
        + "INSERT INTO schema_migrations VALUES (2, 'v2', '2026-09-01T00:00:01Z');"
        + SCHEMA_V3
        + "INSERT INTO schema_migrations VALUES (3, 'v3', '2026-09-01T00:00:02Z');"
        + "COMMIT;"
    )
    connection.create_function("migration_fault", 1, None)
    return connection


def test_v3_empty_legacy_import_upgrades_to_single_v4_truth(tmp_path):
    connection = _v3_database(tmp_path)
    initialize_schema(connection)
    assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
        (1,),
        (2,),
        (3,),
        (4,),
    ]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "migration_sessions" in tables
    assert "legacy_imports" not in tables


def test_v3_nonempty_legacy_import_stops_with_stable_diagnostic(tmp_path):
    connection = _v3_database(tmp_path)
    connection.execute(
        "INSERT INTO legacy_imports VALUES (?, ?, '{}', ?, 'completed', '{}', NULL, ?, ?)",
        ("old", "f" * 64, "/backup", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )
    with pytest.raises(MigrationSchemaError) as error:
        initialize_schema(connection)
    assert error.value.code == "legacy_import_state_requires_diagnostic"
    assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
        (1,),
        (2,),
        (3,),
    ]
    assert connection.execute("SELECT count(*) FROM legacy_imports").fetchone()[0] == 1


def test_v4_fault_rolls_back_table_drop_and_version(tmp_path):
    connection = _v3_database(tmp_path)

    def fail(phase: str):
        if phase == "after_migration_sessions_table":
            raise RuntimeError("fault")

    with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
        initialize_schema(connection, migration_fault=fail)
    assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
        (1,),
        (2,),
        (3,),
    ]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "legacy_imports" in tables
    assert "migration_sessions" not in tables


def test_fresh_repository_exposes_migration_session_contract(tmp_path):
    repository = ProjectRepository(tmp_path)
    assert repository.connection.execute("SELECT count(*) FROM migration_sessions").fetchone()[0] == 0


def _session(repository: ProjectRepository):
    return repository.create_migration_session(
        migration_id="migration-1",
        source_fingerprint="a" * 64,
        plan_version=3,
        plan_hash="b" * 64,
        source_manifest=[{"logical_type": "runs", "sha256": "c" * 64}],
        choices={"trigger_mode": "manual"},
        request_id="request-1",
        request_hash="d" * 64,
        backup_path="/safe/backup",
        occurred_at="2026-09-01T00:00:00Z",
    )


def test_migration_session_cas_failure_retry_completion_and_acknowledgement(tmp_path):
    repository = ProjectRepository(tmp_path)
    created = _session(repository)
    assert repository.get_migration_session_by_fingerprint("a" * 64) == created
    assert repository.get_migration_session_by_request("request-1") == created
    with pytest.raises(RevisionConflictError):
        repository.update_migration_stage(created.migration_id, 9, state="migrating", stage="apply")
    with pytest.raises(MigrationStateError, match="backup"):
        repository.update_migration_stage(created.migration_id, 1, state="migrating", stage="apply")
    copying = repository.update_migration_stage(
        created.migration_id,
        created.revision,
        state="backing_up",
        stage="copy_metadata",
    )
    assert copying.revision == created.revision + 1 and copying.stage == "copy_metadata"

    failed = repository.record_migration_failure(
        copying.migration_id,
        copying.revision,
        failure_code="backup_failed",
        failure_summary="Bearer SENTINEL-MIGRATION-KEY /Users/private/source",
    )
    assert failed.state == "failed_rolled_back" and failed.backup_status == "failed"
    assert "SENTINEL-MIGRATION-KEY" not in failed.failure_summary and "/Users/private" not in failed.failure_summary
    assert "SENTINEL-MIGRATION-KEY" not in repr(failed)
    for database_file in tmp_path.glob("venus.sqlite3*"):
        assert b"SENTINEL-MIGRATION-KEY" not in database_file.read_bytes()
    retry = repository.update_migration_stage(
        failed.migration_id,
        failed.revision,
        state="backing_up",
        stage="backup",
        backup_status="pending",
    )
    migrating = repository.update_migration_stage(
        retry.migration_id,
        retry.revision,
        state="migrating",
        stage="apply",
        backup_status="completed",
    )
    validating = repository.update_migration_stage(
        migrating.migration_id, migrating.revision, state="validating", stage="validate"
    )
    project = repository.create_project(
        "迁移项目", default_project_config(tmp_path / "source", tmp_path / "output")
    )
    repository.set_data_mode("projects")
    completed = repository.complete_migration_session(
        validating.migration_id,
        validating.revision,
        project_id=project.project_id,
        report={"imported": 1},
        attention_required=False,
    )
    assert completed.state == "completed_ready" and completed.completed_at and completed.report == {"imported": 1}
    acknowledged = repository.acknowledge_migration_session(completed.migration_id, completed.revision)
    assert acknowledged.acknowledged_at and acknowledged.revision == completed.revision + 1


def test_migration_session_identity_and_terminal_state_invariants(tmp_path):
    repository = ProjectRepository(tmp_path)
    created = _session(repository)
    assert repository.create_migration_session(
        migration_id="other-id",
        source_fingerprint="a" * 64,
        plan_version=3,
        plan_hash="b" * 64,
        source_manifest=[],
        choices={},
        request_id="request-1",
        request_hash="d" * 64,
    ) == created
    with pytest.raises(MigrationStateError):
        repository.create_migration_session(
            migration_id="migration-2",
            source_fingerprint="e" * 64,
            plan_version=3,
            plan_hash="f" * 64,
            source_manifest=[],
            choices={},
            request_id="request-2",
            request_hash="1" * 64,
        )
    diagnostic = repository.update_migration_stage(
        created.migration_id, created.revision, state="diagnostic_required", stage="diagnostic"
    )
    with pytest.raises(MigrationStateError):
        repository.update_migration_stage(
            diagnostic.migration_id, diagnostic.revision, state="backing_up", stage="backup"
        )
    with pytest.raises(sqlite3.IntegrityError):
        repository.connection.execute(
            "UPDATE migration_sessions SET state='completed_ready' WHERE migration_id=?",
            (created.migration_id,),
        )
