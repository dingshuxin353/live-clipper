from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from live_clipper.first_run_state import (
    FirstRunSession,
    FirstRunStateError,
    MigrationStartupSession,
    StartupDetection,
    decide_startup,
)
from live_clipper.project_domain import default_project_config
from live_clipper.project_result_domain import RequestConflictError, RevisionConflictError
from live_clipper.project_service import open_project_repository
from live_clipper.project_storage import ProjectRepository


def test_draft_is_whitelisted_merged_and_secret_free_on_disk(tmp_path):
    repository = open_project_repository(tmp_path)
    repository.begin_first_run_session()
    saved = repository.update_first_run_draft(
        1,
        {
            "asr": {"mode": "local", "local_model_id": "balanced", "model_source": "modelscope"},
            "project": {"name": "第一个项目", "interval_minutes": 60},
        },
        current_step="asr",
    )
    assert saved.revision == 2
    assert saved.current_step == "asr"
    assert saved.draft["project"] == {"interval_minutes": 60, "name": "第一个项目"}
    assert repository.update_first_run_draft(saved.revision, {"asr": {}}) == saved

    for forbidden in ["api_key", "ApiKey", "authorization", "bearer", "credential", "raw_response", "prompt"]:
        with pytest.raises(ValueError, match="not persistable") as error:
            repository.update_first_run_draft(saved.revision, {"ai": {forbidden: "SENTINEL-SECRET"}})
        assert "SENTINEL-SECRET" not in str(error.value)
    for database_file in tmp_path.glob("venus.sqlite3*"):
        assert b"SENTINEL-SECRET" not in database_file.read_bytes()


def test_session_can_only_begin_for_confirmed_empty_projects_mode(tmp_path):
    legacy = ProjectRepository(tmp_path / "legacy")
    with pytest.raises(FirstRunStateError):
        legacy.begin_first_run_session()

    repository = open_project_repository(tmp_path / "with-project")
    source = tmp_path / "existing-source"
    output = tmp_path / "existing-output"
    source.mkdir()
    output.mkdir()
    repository.create_project("已有项目", default_project_config(source, output))
    with pytest.raises(FirstRunStateError):
        repository.begin_first_run_session()


def test_state_transitions_revision_idempotency_and_restart(tmp_path):
    repository = open_project_repository(tmp_path)
    started = repository.begin_first_run_session(started_at="2026-08-29T00:00:00Z")
    paused = repository.pause_first_run(started.revision, occurred_at="2026-08-29T00:00:01Z")
    assert paused.state == "paused" and paused.revision == 2 and paused.paused_at
    assert repository.pause_first_run(started.revision) == paused

    resumed = repository.resume_first_run(paused.revision, occurred_at="2026-08-29T00:00:02Z")
    assert resumed.state == "in_progress" and resumed.revision == 3 and resumed.paused_at is None
    with pytest.raises(RevisionConflictError):
        repository.update_first_run_draft(paused.revision, {"project": {"name": "stale"}})
    with pytest.raises(FirstRunStateError):
        repository.complete_first_run(resumed.revision)

    repository.close()
    reopened = ProjectRepository(tmp_path)
    assert reopened.get_first_run_session() == resumed


def test_reservation_binding_activation_failure_and_completion(tmp_path):
    repository = open_project_repository(tmp_path / "service")
    session = repository.begin_first_run_session()
    reserved = repository.reserve_first_project_request(session.revision, "request-1", "a" * 64)
    assert reserved.project_request_id == "request-1" and reserved.revision == 2
    assert repository.reserve_first_project_request(session.revision, "request-1", "a" * 64) == reserved
    with pytest.raises(RequestConflictError):
        repository.reserve_first_project_request(reserved.revision, "request-1", "b" * 64)

    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    project = repository.create_project("首项目", default_project_config(source, output))
    repository.save_idempotency_key(
        "project.create",
        "request-1",
        request_hash="a" * 64,
        object_type="project",
        object_id=project.project_id,
    )
    bound = repository.bind_first_project(reserved.revision, "request-1", project.project_id)
    assert bound.state == "activation_pending" and bound.first_project_id == project.project_id
    assert repository.bind_first_project(reserved.revision, "request-1", project.project_id) == bound

    failed = repository.record_activation_failure(
        bound.revision,
        "service_not_ready",
        "Bearer top-secret /Users/example/private",
    )
    assert failed.failure_code == "service_not_ready"
    assert "top-secret" not in failed.failure_summary and "/Users/example" not in failed.failure_summary
    retried = repository.record_activation_failure(
        failed.revision,
        "service_not_ready",
        "Bearer top-secret /Users/example/private",
        occurred_at="2026-08-29T00:00:02Z",
    )
    assert retried.revision == failed.revision + 1
    assert repository.record_activation_failure(
        failed.revision, "service_not_ready", "Bearer top-secret /Users/example/private"
    ) == retried
    completed = repository.complete_first_run(retried.revision, occurred_at="2026-08-29T00:00:03Z")
    assert completed.state == "completed" and completed.current_step == "complete" and completed.completed_at
    with pytest.raises(FirstRunStateError):
        repository.resume_first_run(completed.revision)


def test_reserved_request_can_change_only_before_project_idempotency_exists(tmp_path):
    repository = open_project_repository(tmp_path)
    started = repository.begin_first_run_session()
    first = repository.reserve_first_project_request(started.revision, "old", "a" * 64)
    replacement = repository.reserve_first_project_request(first.revision, "new", "b" * 64)
    assert replacement.project_request_id == "new"
    repository.save_idempotency_key(
        "project.create", "new", request_hash="b" * 64, object_type="project", object_id="missing-project"
    )
    with pytest.raises(RequestConflictError):
        repository.reserve_first_project_request(replacement.revision, "third", "c" * 64)


def test_concurrent_compare_and_set_has_one_winner(tmp_path):
    setup = open_project_repository(tmp_path)
    setup.begin_first_run_session()
    setup.close()

    def update(name: str):
        repository = ProjectRepository(tmp_path)
        try:
            return repository.update_first_run_draft(1, {"project": {"name": name}})
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, name) for name in ("A", "B")]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except RevisionConflictError:
            outcomes.append("conflict")
    assert sum(item == "conflict" for item in outcomes) == 1
    assert sum(item != "conflict" for item in outcomes) == 1


@pytest.mark.parametrize(
    ("detection", "session_state", "first_project_id", "existing_ids", "entry", "onboarding"),
    [
        (StartupDetection(), None, None, (), "onboarding", "new"),
        (StartupDetection(project_count=1, data_mode="projects", has_project_database=True), None, None, ("p1",), "workbench", None),
        (StartupDetection(False, (), True, "projects", 0, True), "in_progress", None, (), "onboarding", "resume"),
        (StartupDetection(False, (), True, "projects", 0, True), "paused", None, (), "onboarding", "paused"),
        (StartupDetection(False, (), True, "projects", 1, True), "activation_pending", "p1", ("p1",), "onboarding", "activation_pending"),
        (StartupDetection(False, (), True, "projects", 1, True), "completed", "p1", ("p1",), "workbench", None),
        (StartupDetection(False, (), True, "projects", 0, True), "completed", "missing", (), "diagnostic_required", None),
        (StartupDetection(False, (), True, "projects", 0, True), "activation_pending", "missing", (), "diagnostic_required", None),
        (StartupDetection(True, ("legacy_metadata",)), None, None, (), "migration_required", None),
        (StartupDetection(True, ("legacy_data_mode",), True, "legacy"), None, None, (), "diagnostic_required", None),
        (StartupDetection(True, ("legacy_metadata",), True, "projects", 1, True), "in_progress", None, ("p1",), "diagnostic_required", None),
    ],
)
def test_startup_decision_table(detection, session_state, first_project_id, existing_ids, entry, onboarding):
    session = None
    if session_state:
        session = FirstRunSession(
            session_id="primary",
            state=session_state,
            current_step="complete" if session_state in {"activation_pending", "completed"} else "welcome",
            revision=1,
            draft={},
            project_request_id="request-1" if first_project_id else None,
            project_request_hash="a" * 64 if first_project_id else None,
            first_project_id=first_project_id,
            failure_code=None,
            failure_summary=None,
            started_at="2026-08-29T00:00:00Z",
            updated_at="2026-08-29T00:00:00Z",
            paused_at="2026-08-29T00:00:00Z" if session_state == "paused" else None,
            completed_at="2026-08-29T00:00:00Z" if session_state == "completed" else None,
        )
    decision = decide_startup(detection, session=session, existing_project_ids=existing_ids)
    assert decision.entry == entry
    assert decision.onboarding == onboarding


def _migration_startup(
    state: str,
    *,
    project_id: str | None = None,
    backup_status: str = "pending",
    has_report: bool = False,
    completed_at: str | None = None,
    acknowledged_at: str | None = None,
) -> MigrationStartupSession:
    return MigrationStartupSession(
        migration_id="migration-1",
        state=state,
        revision=2,
        project_id=project_id,
        backup_status=backup_status,
        has_report=has_report,
        completed_at=completed_at,
        acknowledged_at=acknowledged_at,
    )


@pytest.mark.parametrize("state", ["backing_up", "migrating", "validating"])
def test_active_migration_routes_to_resume(state):
    detection = StartupDetection(True, ("legacy_metadata",), True, "legacy", 0, False, 1)
    decision = decide_startup(
        detection,
        session=None,
        existing_project_ids=(),
        migration_sessions=(_migration_startup(state, backup_status="completed"),),
    )
    assert decision == decision.__class__("migration_required", reason_code="migration_resume")


def test_failed_migration_routes_to_retry():
    detection = StartupDetection(True, ("legacy_metadata",), True, "legacy", 0, False, 1)
    decision = decide_startup(
        detection,
        session=None,
        existing_project_ids=(),
        migration_sessions=(_migration_startup("failed_rolled_back", backup_status="failed"),),
    )
    assert decision == decision.__class__("migration_required", reason_code="migration_retry")


def test_completed_migration_routes_to_completion_gate_until_acknowledged():
    detection = StartupDetection(False, (), True, "projects", 1, False, 1)
    migration = _migration_startup(
        "completed_attention",
        project_id="project-1",
        backup_status="completed",
        has_report=True,
        completed_at="2026-09-01T00:00:00Z",
    )
    decision = decide_startup(
        detection, session=None, existing_project_ids=("project-1",), migration_sessions=(migration,)
    )
    assert decision.entry == "workbench" and decision.reason_code == "migration_completed_unacknowledged"
    acknowledged = MigrationStartupSession(
        **{**migration.__dict__, "acknowledged_at": "2026-09-01T00:01:00Z"}
    )
    assert decide_startup(
        detection, session=None, existing_project_ids=("project-1",), migration_sessions=(acknowledged,)
    ).reason_code is None


@pytest.mark.parametrize(
    ("detection", "migrations", "existing_ids", "reason"),
    [
        (
            StartupDetection(True, ("legacy_metadata",), True, "projects", 1, False, 1),
            (_migration_startup("migrating", backup_status="completed"),),
            ("project-1",),
            "migration_state_conflict",
        ),
        (
            StartupDetection(True, ("legacy_metadata",), True, "legacy", 0, False, 1),
            (_migration_startup("validating", backup_status="pending"),),
            (),
            "migration_state_conflict",
        ),
        (
            StartupDetection(False, (), True, "projects", 1, False, 1),
            (_migration_startup("completed_ready", project_id="project-1", backup_status="completed"),),
            ("project-1",),
            "migration_completion_conflict",
        ),
        (
            StartupDetection(True, ("legacy_metadata",), True, "legacy", 0, False, 2),
            (_migration_startup("backing_up"), _migration_startup("backing_up")),
            (),
            "multiple_migration_sessions",
        ),
        (
            StartupDetection(True, ("legacy_metadata",), True, "legacy", 0, False, 1),
            (_migration_startup("unknown"),),
            (),
            "unknown_migration_state",
        ),
    ],
)
def test_migration_startup_conflicts_fail_closed(detection, migrations, existing_ids, reason):
    decision = decide_startup(
        detection, session=None, existing_project_ids=existing_ids, migration_sessions=migrations
    )
    assert decision.entry == "diagnostic_required" and decision.reason_code == reason
