from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from live_clipper.project_domain import default_project_config
from live_clipper.project_storage import ProjectRepository, connect_database


def _config(tmp_path: Path) -> dict:
    return default_project_config(tmp_path / "source", tmp_path / "output")


def test_schema_connection_contract_and_reopen(tmp_path):
    repo = ProjectRepository(tmp_path)
    assert repo.get_data_mode() == "legacy"
    assert repo.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert repo.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repo.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert repo.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert repo.connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,)]
    repo.close()

    reopened = connect_database(tmp_path)
    assert reopened.execute("SELECT value FROM system_state WHERE key = 'data_mode'").fetchone()[0] == "legacy"
    reopened.close()


def test_project_config_is_secret_free_and_immutable(tmp_path):
    repo = ProjectRepository(tmp_path)
    config = _config(tmp_path)
    project = repo.create_project("示例", config)
    with pytest.raises(sqlite3.IntegrityError):
        repo.connection.execute(
            "UPDATE project_config_revisions SET config_json = '{}' WHERE project_id = ?",
            (project.project_id,),
        )
    config["resources"]["api_key"] = "sk-do-not-store"
    with pytest.raises(ValueError, match="credential"):
        repo.add_config_revision(project.project_id, config)
    assert "sk-do-not-store" not in repo.database_path.read_bytes().decode("utf-8", errors="ignore")


def test_normal_run_creation_is_atomic_and_project_scoped(tmp_path):
    repo = ProjectRepository(tmp_path)
    first = repo.create_project("A", _config(tmp_path))
    second = repo.create_project("B", _config(tmp_path))
    scan = repo.create_scan_event(first.project_id, trigger_source="manual")

    created = repo.create_normal_run(
        project_id=first.project_id,
        content_id="content-1",
        source_scan_id=scan.scan_id,
        trigger_source="manual",
        first_seen_path="/recordings/a.mp4",
        latest_seen_path="/recordings/a.mp4",
        parameter_snapshot={"model": "safe"},
        queued_at="2026-08-19T00:00:00Z",
    )
    duplicate = repo.create_normal_run(
        project_id=first.project_id,
        content_id="content-1",
        source_scan_id=scan.scan_id,
        trigger_source="manual",
        first_seen_path="/recordings/renamed.mp4",
        latest_seen_path="/recordings/renamed.mp4",
        parameter_snapshot={"model": "safe"},
        queued_at="2026-08-19T00:00:01Z",
    )
    other_project = repo.create_normal_run(
        project_id=second.project_id,
        content_id="content-1",
        trigger_source="manual",
        first_seen_path="/recordings/a.mp4",
        latest_seen_path="/recordings/a.mp4",
        parameter_snapshot={"model": "safe"},
        queued_at="2026-08-19T00:00:02Z",
    )

    assert created.created and not duplicate.created and duplicate.duplicate
    assert duplicate.run.run_id == created.run.run_id
    assert other_project.created
    assert repo.get_scan_event(scan.scan_id).created_count == 1
    assert len(repo.list_workspace_events()) == 2


def test_running_scan_unique_and_transaction_rolls_back(tmp_path):
    repo = ProjectRepository(tmp_path)
    project = repo.create_project("A", _config(tmp_path))
    repo.create_scan_event(project.project_id, trigger_source="manual")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_scan_event(project.project_id, trigger_source="scheduled")

    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.connection.execute(
                "INSERT INTO workspace_events(event_type, occurred_at, payload_json) VALUES (?, ?, ?)",
                ("will_rollback", "2026-08-19T00:00:00Z", "{}"),
            )
            raise RuntimeError("fault injection")
    assert all(event.event_type != "will_rollback" for event in repo.list_workspace_events())


def test_run_creation_rolls_back_run_count_and_event_together(tmp_path):
    repo = ProjectRepository(tmp_path)
    project = repo.create_project("A", _config(tmp_path))
    scan = repo.create_scan_event(project.project_id, trigger_source="manual")
    repo.connection.execute(
        """CREATE TRIGGER reject_workspace_event BEFORE INSERT ON workspace_events
           BEGIN SELECT RAISE(ABORT, 'fault injection'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="fault injection"):
        repo.create_normal_run(
            project_id=project.project_id,
            content_id="content-rollback",
            source_scan_id=scan.scan_id,
            trigger_source="manual",
            first_seen_path="/a.mp4",
            latest_seen_path="/a.mp4",
            parameter_snapshot={},
        )
    assert repo.find_run(project.project_id, "content-rollback") is None
    assert repo.get_scan_event(scan.scan_id).created_count == 0


def test_two_connections_race_to_create_only_one_normal_run(tmp_path):
    setup = ProjectRepository(tmp_path)
    project = setup.create_project("A", _config(tmp_path))

    def create():
        with ProjectRepository(tmp_path) as repo:
            return repo.create_normal_run(
                project_id=project.project_id,
                content_id="same-content",
                trigger_source="manual",
                first_seen_path="/same.mp4",
                latest_seen_path="/same.mp4",
                parameter_snapshot={},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))
    assert sorted(result.created for result in results) == [False, True]
    assert len(setup.list_runs(project.project_id)) == 1


def test_revision_and_run_snapshot_remain_historical(tmp_path):
    repo = ProjectRepository(tmp_path)
    config_v1 = _config(tmp_path)
    project = repo.create_project("A", config_v1)
    run = repo.create_normal_run(
        project_id=project.project_id,
        content_id="content-v1",
        trigger_source="manual",
        first_seen_path="/a.mp4",
        latest_seen_path="/a.mp4",
        parameter_snapshot={"config_marker": "v1"},
    ).run
    config_v2 = _config(tmp_path)
    config_v2["processing"]["output_profile"] = "new-renderer"
    revision = repo.add_config_revision(project.project_id, config_v2, expected_revision=1)

    assert revision.revision == 2
    assert repo.get_config_revision(project.project_id, 1).config == config_v1
    assert repo.get_run(run.run_id).config_revision == 1
    assert repo.get_run(run.run_id).parameter_snapshot == {"config_marker": "v1"}


def test_stage_workspace_view_and_idempotency_state_survive_reopen(tmp_path):
    repo = ProjectRepository(tmp_path)
    project = repo.create_project("A", _config(tmp_path))
    run = repo.create_normal_run(
        project_id=project.project_id,
        content_id="content-events",
        trigger_source="manual",
        first_seen_path="/a.mp4",
        latest_seen_path="/a.mp4",
        parameter_snapshot={},
    ).run
    transitioned = repo.transition_run(
        run.run_id,
        status="processing",
        stage="transcribe",
        event_type="started",
        detail={"attempt": 1},
    )
    latest_event_id = repo.list_workspace_events()[-1].event_id
    repo.set_workspace_view("studio", latest_event_id)
    assert repo.save_idempotency_key(
        "scan", "request-1", request_hash="hash", object_type="run", object_id=run.run_id
    )
    assert not repo.save_idempotency_key(
        "scan", "request-1", request_hash="other", object_type="run", object_id="other"
    )
    repo.close()

    reopened = ProjectRepository(tmp_path)
    assert reopened.get_run(run.run_id).status == transitioned.status == "processing"
    assert reopened.get_workspace_view("studio")["last_seen_event_id"] == latest_event_id
    assert reopened.get_idempotency_key("scan", "request-1")["object_id"] == run.run_id
