from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from live_clipper.project_domain import default_project_config, project_config_v2
from live_clipper.project_result_domain import RequestConflictError, RevisionConflictError
from live_clipper.project_storage import SCHEMA_V1, ProjectRepository, connect_database, initialize_schema


def _v1_database(path: Path) -> sqlite3.Connection:
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "venus.sqlite3", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        f"""BEGIN IMMEDIATE;
{SCHEMA_V1}
INSERT INTO schema_migrations VALUES (1, 'project foundation v1', '2026-08-26T00:00:00Z');
INSERT INTO system_state VALUES ('data_mode', 'projects', '2026-08-26T00:00:00Z');
COMMIT;
"""
    )
    return connection


def _run(repo: ProjectRepository, tmp_path: Path, *, status: str = "processing"):
    project = repo.create_project("P", default_project_config(tmp_path / "source", tmp_path / "output"))
    run = repo.create_normal_run(
        project_id=project.project_id,
        content_id="content",
        trigger_source="manual",
        first_seen_path="/recordings/source.mp4",
        latest_seen_path="/recordings/source.mp4",
        parameter_snapshot={"safe": True},
        queued_at="2026-08-26T00:00:00Z",
    ).run
    if status == "processing":
        run = repo.transition_run(
            run.run_id,
            status="processing",
            stage="review",
            event_type="started",
            occurred_at="2026-08-26T00:00:01Z",
        )
    return project, run


def _selected_review(repo: ProjectRepository, tmp_path: Path):
    project, run = _run(repo, tmp_path)
    session = repo.create_ai_review_session(
        run.run_id,
        attempt_number=1,
        resource_ref="resource.review",
        model_name="model",
        strategy_version="auto_review_v1",
        parameter_snapshot={"temperature": 0},
        evidence_relative_path="review/review_result.json",
        started_at="2026-08-26T00:00:02Z",
    )
    repo.register_verified_review(
        session.review_session_id,
        status="selected",
        decisions=[
            {
                "candidate_id": "candidate-1",
                "decision": "selected",
                "rank": 1,
                "source_start_ms": 1000,
                "source_end_ms": 9000,
                "selected_start_ms": 1500,
                "selected_end_ms": 8500,
                "reason": "有完整叙事",
            },
            {
                "candidate_id": "candidate-2",
                "decision": "rejected",
                "rank": 2,
                "source_start_ms": 10000,
                "source_end_ms": 15000,
                "rejection_reason_code": "weak_context",
            },
        ],
        outputs=[
            {
                "output_id": "output-1",
                "candidate_id": "candidate-1",
                "display_order": 1,
                "relative_path": "clips/output-1.mp4",
                "file_name": "output-1.mp4",
            }
        ],
        materials=[
            {
                "material_id": "material-1",
                "output_id": "output-1",
                "title_candidates": [{"title_id": "title-1", "text": "标题"}],
                "preferred_title_id": "title-1",
                "description": "描述",
                "tags": ["标签"],
            }
        ],
        overall_summary="值得剪辑",
        evidence_sha256="a" * 64,
        completed_at="2026-08-26T00:00:03Z",
    )
    return project, run, session


def test_fresh_database_is_schema_v2_with_required_tables_and_indexes(tmp_path):
    repo = ProjectRepository(tmp_path)
    assert repo.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
        (1,),
        (2,),
    ]
    tables = {
        row[0]
        for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {
        "ai_review_sessions",
        "candidate_decisions",
        "run_results",
        "run_outputs",
        "output_materials",
        "issues",
        "issue_events",
        "recovery_attempts",
    } <= tables
    indexes = {
        row[0]
        for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    assert {
        "one_running_review_per_run",
        "run_results_unseen",
        "run_outputs_status_updated",
        "one_active_issue_per_cause",
        "recovery_attempts_issue_requested",
    } <= indexes
    assert repo.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v1_upgrade_preserves_bytes_and_allows_v1_and_v2_config(tmp_path):
    connection = _v1_database(tmp_path)
    config_v1 = default_project_config(tmp_path / "source", tmp_path / "output")
    config_bytes = __import__("json").dumps(config_v1, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute("BEGIN")
    connection.execute(
        """INSERT INTO projects VALUES (
             'project-1', 'P', '', 'inactive', 1,
             '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z', NULL, NULL)"""
    )
    connection.execute(
        "INSERT INTO project_config_revisions VALUES ('project-1', 1, ?, 1, '2026-08-26T00:00:00Z')",
        (config_bytes,),
    )
    connection.execute(
        "INSERT INTO project_runtime(project_id, readiness_state, auto_scan_state, first_scan_state) "
        "VALUES ('project-1', 'ready', 'off', 'not_required')"
    )
    connection.commit()
    connection.close()

    repo = ProjectRepository(tmp_path)
    historical = repo.get_config_revision("project-1", 1)
    assert historical is not None and historical.config == config_v1 and historical.schema_version == 1
    revision = repo.add_config_revision("project-1", project_config_v2(config_v1), expected_revision=1)
    assert revision.schema_version == 2
    assert revision.config["processing"]["review_strategy"] == "ai_auto"
    assert repo.get_config_revision("project-1", 1).config == config_v1


def test_v1_upgrade_fault_rolls_back_every_schema_change(tmp_path):
    connection = _v1_database(tmp_path)

    def fail(phase: str):
        if phase == "before_version_record":
            raise RuntimeError("fault injection")

    with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
        initialize_schema(connection, migration_fault=fail)
    assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_results'"
    ).fetchone() is None
    config_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_config_revisions'"
    ).fetchone()[0]
    assert "schema_version = 1" in config_sql
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_newer_database_is_rejected_without_creating_business_tables(tmp_path):
    connection = sqlite3.connect(tmp_path / "venus.sqlite3")
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    connection.execute("INSERT INTO schema_migrations VALUES (3, 'future', '2026-08-26T00:00:00Z')")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        connect_database(tmp_path)
    check = sqlite3.connect(tmp_path / "venus.sqlite3")
    assert check.execute("SELECT version FROM schema_migrations").fetchall() == [(3,)]
    assert check.execute("SELECT 1 FROM sqlite_master WHERE name = 'projects'").fetchone() is None


def test_verified_no_clip_is_one_transaction_and_completes_run(tmp_path):
    repo = ProjectRepository(tmp_path)
    _project, run = _run(repo, tmp_path)
    session = repo.create_ai_review_session(
        run.run_id,
        attempt_number=1,
        resource_ref="review",
        model_name="model",
        strategy_version="auto_review_v1",
        parameter_snapshot={},
    )
    verified = repo.register_verified_review(
        session.review_session_id,
        status="no_clip",
        decisions=[
            {
                "candidate_id": "candidate-1",
                "decision": "rejected",
                "source_start_ms": 0,
                "source_end_ms": 1000,
                "rejection_reason_code": "no_value",
            }
        ],
        overall_summary="没有适合成片的候选",
        evidence_sha256="b" * 64,
    )
    assert verified.status == "no_clip"
    assert repo.get_run_result(run.run_id).result_type == "no_clip"
    assert repo.get_run(run.run_id).status == "completed"
    assert repo.list_run_outputs(run.run_id) == []

    repo.connection.execute(
        """CREATE TRIGGER reject_review_event BEFORE INSERT ON workspace_events
           WHEN NEW.event_type = 'review_verified'
           BEGIN SELECT RAISE(ABORT, 'fault'); END"""
    )
    _other_project, other_run = _run(repo, tmp_path / "other")
    other_session = repo.create_ai_review_session(
        other_run.run_id,
        attempt_number=1,
        resource_ref="review",
        model_name="model",
        strategy_version="auto_review_v1",
        parameter_snapshot={},
    )
    with pytest.raises(sqlite3.IntegrityError, match="fault"):
        repo.register_verified_review(other_session.review_session_id, status="no_clip", decisions=[])
    assert repo.get_ai_review_session(other_session.review_session_id).status == "running"
    assert repo.get_run_result(other_run.run_id) is None


def test_review_snapshot_rejects_raw_model_payload_and_redacts_secret_values(tmp_path):
    repo = ProjectRepository(tmp_path)
    _project, run = _run(repo, tmp_path)
    with pytest.raises(ValueError, match="non-persistable model field"):
        repo.create_ai_review_session(
            run.run_id,
            attempt_number=1,
            resource_ref="review",
            model_name="model",
            strategy_version="auto_review_v1",
            parameter_snapshot={"raw_response": "do not store"},
        )
    session = repo.create_ai_review_session(
        run.run_id,
        attempt_number=1,
        resource_ref="review",
        model_name="model",
        strategy_version="auto_review_v1",
        parameter_snapshot={"note": "Bearer super-secret-value"},
    )
    assert session.parameter_snapshot == {"note": "[redacted]"}
    assert "super-secret-value" not in repo.database_path.read_bytes().decode("utf-8", errors="ignore")


def test_selected_review_output_projection_seen_cas_and_material_revision(tmp_path):
    repo = ProjectRepository(tmp_path)
    _project, run, _session = _selected_review(repo, tmp_path)
    assert repo.update_output_and_reproject_result("output-1", status="rendering") is None
    result = repo.update_output_and_reproject_result(
        "output-1",
        status="ready",
        media_metadata={
            "duration_ms": 7000,
            "width": 1920,
            "height": 1080,
            "container": "mp4",
            "video_codec": "h264",
            "byte_size": 1234,
        },
        occurred_at="2026-08-26T00:00:04Z",
    )
    assert result is not None and result.result_type == "clips_ready" and result.result_revision == 1
    assert result.candidate_count == result.selected_count + result.rejected_count == 2
    assert repo.get_run(run.run_id).status == "completed"

    seen = repo.mark_result_seen(
        run.run_id, expected_result_revision=1, seen_at="2026-08-26T00:00:05Z"
    )
    repeated = repo.mark_result_seen(
        run.run_id, expected_result_revision=1, seen_at="2026-08-26T00:00:06Z"
    )
    assert seen.result_seen_at == repeated.result_seen_at == "2026-08-26T00:00:05Z"
    material = repo.update_output_material(
        "output-1",
        expected_material_revision=1,
        title_candidates=[{"title_id": "title-2", "text": "新标题"}],
        preferred_title_id="title-2",
        description="新描述",
        tags=["新标签"],
    )
    assert material.material_revision == 2
    assert repo.get_run_result(run.run_id).result_revision == 1

    failed = repo.update_output_and_reproject_result(
        "output-1", status="missing", error_code="missing", error_summary="文件缺失"
    )
    assert failed.result_type == "unavailable" and failed.result_revision == 2
    with pytest.raises(RevisionConflictError, match="result_revision_conflict"):
        repo.mark_result_seen(run.run_id, expected_result_revision=1)
    assert repo.get_run_result(run.run_id).seen_result_revision == 1


def test_issue_dedupe_revision_events_and_recovery_request_id(tmp_path):
    repo = ProjectRepository(tmp_path)
    project, run = _run(repo, tmp_path)
    first = repo.discover_issue(
        issue_code="resource_unavailable",
        category="resource",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="resource:analysis",
        diagnostic_summary="sk-secret-value at /Users/alice/private/log.txt",
    )
    second = repo.discover_issue(
        issue_code="resource_unavailable",
        category="resource",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="resource:analysis",
        summary="仍不可用",
    )
    assert first.issue_id == second.issue_id and second.issue_revision == 2
    assert [event.event_type for event in repo.list_issue_events(first.issue_id)] == ["discovered", "rediscovered"]
    database_bytes = repo.database_path.read_bytes().decode("utf-8", errors="ignore")
    assert "sk-secret-value" not in database_bytes and "/Users/alice/private" not in database_bytes
    checking = repo.transition_issue(
        first.issue_id,
        expected_issue_revision=2,
        status="checking",
        event_type="recheck_started",
    )
    with pytest.raises(RevisionConflictError):
        repo.transition_issue(
            first.issue_id,
            expected_issue_revision=2,
            status="ready_to_recover",
            event_type="stale",
        )
    attempt = repo.register_recovery_attempt(
        first.issue_id,
        request_id="request-1",
        attempt_type="continue_run",
        requested_by="user",
        run_id=run.run_id,
        reuse_stages=["transcribe"],
        redo_stages=["review"],
    )
    repeated = repo.register_recovery_attempt(
        first.issue_id,
        request_id="request-1",
        attempt_type="continue_run",
        requested_by="user",
        run_id=run.run_id,
        reuse_stages=["transcribe"],
        redo_stages=["review"],
    )
    assert checking.issue_revision == 3 and attempt.attempt_id == repeated.attempt_id
    with pytest.raises(RequestConflictError):
        repo.register_recovery_attempt(
            first.issue_id,
            request_id="request-1",
            attempt_type="operational_repair",
            requested_by="user",
        )


def test_concurrent_issue_and_recovery_identity_are_singletons(tmp_path):
    setup = ProjectRepository(tmp_path)
    project, run = _run(setup, tmp_path)

    def discover():
        with ProjectRepository(tmp_path) as repo:
            return repo.discover_issue(
                issue_code="shared",
                category="resource",
                scope_type="run",
                project_id=project.project_id,
                run_id=run.run_id,
                issue_group_key="same-root",
            ).issue_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        issue_ids = list(pool.map(lambda _index: discover(), range(2)))
    assert len(set(issue_ids)) == 1
    issue_id = issue_ids[0]

    def recover():
        with ProjectRepository(tmp_path) as repo:
            return repo.register_recovery_attempt(
                issue_id,
                request_id="same-request",
                attempt_type="continue_run",
                requested_by="system",
                run_id=run.run_id,
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempt_ids = list(pool.map(lambda _index: recover(), range(2)))
    assert len(set(attempt_ids)) == 1


def test_concurrent_review_attempt_and_verified_registration_reuse_identity(tmp_path):
    setup = ProjectRepository(tmp_path)
    _project, run = _run(setup, tmp_path)

    def create_session():
        with ProjectRepository(tmp_path) as repo:
            return repo.create_ai_review_session(
                run.run_id,
                attempt_number=1,
                resource_ref="review",
                model_name="model",
                strategy_version="auto_review_v1",
                parameter_snapshot={},
            ).review_session_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        session_ids = list(pool.map(lambda _index: create_session(), range(2)))
    assert len(set(session_ids)) == 1
    session_id = session_ids[0]

    def register():
        with ProjectRepository(tmp_path) as repo:
            return repo.register_verified_review(
                session_id,
                status="no_clip",
                decisions=[
                    {
                        "candidate_id": "candidate",
                        "decision": "rejected",
                        "source_start_ms": 0,
                        "source_end_ms": 1000,
                    }
                ],
                evidence_sha256="c" * 64,
            ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _index: register(), range(2)))
    assert statuses == ["no_clip", "no_clip"]
    assert len(setup.get_candidate_decisions(session_id)) == 1
    assert setup.get_run_result(run.run_id).result_revision == 1
