from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from test_project_auto_review_v2 import _candidate, _project_run, _selected

from live_clipper.config import Settings
from live_clipper.media_probe import MediaMetadata
from live_clipper.project_recovery import continue_run, recheck_issue, retry_material, retry_output
from live_clipper.project_result_api import ProjectResultAPI
from live_clipper.project_result_runtime import render_project_outputs, run_project_review
from live_clipper.project_storage import ProjectRepository


def _failed_output_issue(tmp_path):
    repository, project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
    )
    render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda *_args: (_ for _ in ()).throw(RuntimeError("failed")),
        probe=lambda _path: MediaMetadata(1, 1, 1, "mp4", "h264", 1),
    )
    issue = next(item for item in repository.list_issues(run_id=run.run_id, active_only=True) if item.issue_code == "render_failed")
    return repository, project, repository.get_run(run.run_id), issue


def test_recheck_only_changes_issue_state_and_does_not_start_processing(tmp_path):
    repository, _project, run, issue = _failed_output_issue(tmp_path)

    checked = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        checker=lambda _issue: {"ok": True, "safe_checkpoint": "validated_review"},
    )

    assert checked.status == "ready_to_recover"
    assert repository.get_run(run.run_id).status == run.status
    assert not repository.list_recovery_attempts(issue.issue_id)


def test_recheck_final_save_failure_preserves_checking_without_new_overrides(tmp_path):
    repository, _project, run, issue = _failed_output_issue(tmp_path)
    output = tmp_path / "replacement-output"
    output.mkdir()
    repository.connection.execute("""CREATE TEMP TRIGGER fail_recheck BEFORE INSERT ON issue_events
        WHEN NEW.event_type = 'recheck_succeeded'
        BEGIN SELECT RAISE(ABORT, 'test final save failure'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="test final save failure"):
        recheck_issue(repository, issue.issue_id, expected_issue_revision=issue.issue_revision,
                      operational_overrides={"output_directory": str(output)})

    current = repository.get_issue(issue.issue_id)
    assert current.status == "checking" and current.issue_revision == issue.issue_revision + 1
    assert current.operational_overrides == issue.operational_overrides
    assert repository.list_issue_events(issue.issue_id)[-1].event_type == "recheck_started"
    assert not repository.list_recovery_attempts(issue.issue_id)
    assert repository.get_run(run.run_id) == run


def test_continue_run_requeues_same_run_id_and_is_idempotent(tmp_path):
    repository, project, run, _output_issue = _failed_output_issue(tmp_path)
    issue = repository.discover_issue(
        issue_code="processing_interrupted",
        category="process",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key=f"processing:{run.run_id}",
        recovery_capability="continue_run",
        safe_checkpoint="validated_review",
        reuse_stages=("read_source", "transcribe", "analyze", "arbitrate", "review"),
        redo_stages=("render",),
    )
    ready = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        checker=lambda _issue: {"ok": True, "safe_checkpoint": "validated_review", "redo_stages": ["render"]},
    )

    attempt = continue_run(
        repository,
        ready.issue_id,
        expected_issue_revision=ready.issue_revision,
        request_id="continue-1",
        requested_by="test",
    )
    repeated = continue_run(
        repository,
        ready.issue_id,
        expected_issue_revision=ready.issue_revision,
        request_id="continue-1",
        requested_by="test",
    )

    updated = repository.get_run(run.run_id)
    assert attempt.attempt_id == repeated.attempt_id
    assert updated.run_id == run.run_id
    assert updated.status == "queued" and updated.current_stage == "render"
    assert len(repository.list_runs(project_id=run.project_id)) == 1


def test_continue_run_reuses_active_same_content_without_recovery_attempt(tmp_path):
    repository, project, run, _output_issue = _failed_output_issue(tmp_path)
    active, reason = repository.create_reprocess_run(
        run.run_id,
        request_id="m9-winner",
        request_hash="winner-hash",
        config_revision=run.config_revision,
        parameter_snapshot=run.parameter_snapshot,
        source_path=run.latest_seen_path,
    )
    issue = repository.discover_issue(
        issue_code="processing_interrupted",
        category="process",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key=f"processing-race:{run.run_id}",
        status="ready_to_recover",
        recovery_capability="continue_run",
        redo_stages=("render",),
    )

    winner = continue_run(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        request_id="m8-loser",
        requested_by="test",
    )

    assert reason == "created" and winner.run_id == active.run_id
    assert not repository.list_recovery_attempts(issue.issue_id)
    assert repository.get_issue(issue.issue_id).status == "ready_to_recover"
    assert len([
        item
        for item in repository.list_runs(project.project_id)
        if item.status in {"queued", "processing"}
    ]) == 1


def test_retry_output_only_resets_target_and_preserves_other_ready_output(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(
        tmp_path,
        candidates=[_candidate("one"), _candidate("two", start=30, end=40)],
    )
    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "two",
            "warnings": [],
            "decisions": [_selected("one"), {**_selected("two", start=30, end=40), "rank": 2}],
        },
    )
    render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda _source, _transcript, clip, _work, partial: (
            partial.write_bytes(b"ready") if clip.clip_id == "one" else (_ for _ in ()).throw(RuntimeError("failed"))
        ),
        probe=lambda path: MediaMetadata(1, 1, 1, "mp4", "h264", path.stat().st_size),
    )
    outputs = repository.list_run_outputs(run.run_id)
    ready_output = next(item for item in outputs if item.status == "ready")
    failed_output = next(item for item in outputs if item.status == "failed")
    issue = next(
        item
        for item in repository.list_issues(run_id=run.run_id, active_only=True)
        if item.issue_code == "render_failed" and item.output_id == failed_output.output_id
    )
    checked = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        checker=lambda _issue: {"ok": True, "safe_checkpoint": "ready_outputs", "redo_stages": ["render"]},
    )

    api = ProjectResultAPI(repository, Settings(), service_dir=repository.service_dir)
    status, _ = api.handle("POST", f"/api/issues/{checked.issue_id}/retry-output", body={
        "request_id": "retry-output-1", "expected_issue_revision": checked.issue_revision,
    })
    assert status == 200

    assert repository.get_run_output(failed_output.output_id).status == "pending"
    assert repository.get_run_output(ready_output.output_id).status == "ready"
    assert repository.get_run(run.run_id).status == "completed"


def test_replacement_source_requires_same_content_and_stays_operational_only(tmp_path):
    repository, project, run, _run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    original_snapshot = run.parameter_snapshot
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"source")
    issue = repository.discover_issue(
        issue_code="source_missing",
        category="recording",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key=f"source:{run.run_id}",
        recovery_capability="continue_run",
        redo_stages=("read_source",),
    )

    checked = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        operational_overrides={"source_path": str(replacement)},
    )
    attempt = continue_run(
        repository,
        checked.issue_id,
        expected_issue_revision=checked.issue_revision,
        request_id="replace-source-1",
        requested_by="test",
    )

    assert attempt.operational_overrides == {"source_path": str(replacement.resolve())}
    assert repository.get_run(run.run_id).latest_seen_path == run.latest_seen_path
    assert repository.get_run(run.run_id).parameter_snapshot == original_snapshot


def test_replacement_source_rejects_same_name_with_different_content(tmp_path):
    repository, project, run, _run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    replacement = tmp_path / "recording.mp4"
    replacement.write_bytes(b"different")
    issue = repository.discover_issue(
        issue_code="source_missing",
        category="recording",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key=f"source:{run.run_id}",
        recovery_capability="continue_run",
        redo_stages=("read_source",),
    )

    checked = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        operational_overrides={"source_path": str(replacement)},
    )

    assert checked.status == "action_required"
    assert checked.operational_overrides == {}


def test_retry_material_preserves_user_edited_fields(tmp_path):
    repository, project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
    )
    output = repository.list_run_outputs(run.run_id)[0]
    material = repository.get_output_material(output.output_id)
    edited = repository.update_output_material(
        output.output_id,
        expected_material_revision=material.material_revision,
        title_candidates=[{"title_id": "user-title", "text": "用户标题"}],
        preferred_title_id="user-title",
        description="用户描述",
        tags=["用户标签"],
    )
    issue = repository.discover_issue(
        issue_code="material_generation_failed",
        category="material",
        scope_type="material",
        project_id=project.project_id,
        run_id=run.run_id,
        output_id=output.output_id,
        material_id=edited.material_id,
        issue_group_key=f"material:{edited.material_id}",
        recovery_capability="retry_material",
    )
    checked = recheck_issue(
        repository,
        issue.issue_id,
        expected_issue_revision=issue.issue_revision,
        checker=lambda _issue: {"ok": True, "safe_checkpoint": "validated_review"},
    )

    retry_material(
        repository,
        checked.issue_id,
        expected_issue_revision=checked.issue_revision,
        request_id="retry-material-1",
        requested_by="test",
    )

    retried = repository.get_output_material(output.output_id)
    assert retried.status == "pending"
    assert retried.material_revision == edited.material_revision
    assert retried.title_candidates == edited.title_candidates
    assert retried.description == "用户描述" and retried.tags == ["用户标签"]


def test_renderer_consumes_accepted_recovery_paths_without_repeating_review(tmp_path):
    repository, project, run, issue = _failed_output_issue(tmp_path)
    replacement = tmp_path / "replacement-source/recording.mp4"
    replacement.parent.mkdir()
    replacement.write_bytes(Path(run.latest_seen_path).read_bytes())
    output_dir = tmp_path / "recovered-output"
    output_dir.mkdir()
    checked = recheck_issue(
        repository, issue.issue_id, expected_issue_revision=issue.issue_revision,
        operational_overrides={"source_path": str(replacement), "output_directory": str(output_dir)},
    )
    assert checked.status == "ready_to_recover"
    retry_output(repository, issue.issue_id, expected_issue_revision=checked.issue_revision,
                 request_id="render", requested_by="test")
    sessions = repository.list_ai_review_sessions(run.run_id)

    def renderer(source, _transcript, _clip, _work, partial):
        assert source == replacement and partial.parent == output_dir
        partial.write_bytes(b"recovered")

    render_project_outputs(
        repository, run.run_id, run_dir=tmp_path / "work/projects" / project.project_id / "runs" / run.run_id,
        renderer=renderer, probe=lambda path: MediaMetadata(1, 1, 1, "mp4", "h264", path.stat().st_size),
    )
    output = repository.list_run_outputs(run.run_id)[0]
    assert output.status == "ready" and (output_dir / output.file_name).read_bytes() == b"recovered"
    assert repository.list_ai_review_sessions(run.run_id) == sessions
    assert repository.get_run(run.run_id).parameter_snapshot == run.parameter_snapshot


def test_direct_recovery_same_request_is_atomic_across_connections(tmp_path):
    repository, _project, _run, issue = _failed_output_issue(tmp_path)
    checked = recheck_issue(repository, issue.issue_id, expected_issue_revision=issue.issue_revision)
    barrier = Barrier(2)

    def recover():
        with ProjectRepository(repository.service_dir) as connection:
            barrier.wait(timeout=5)
            return retry_output(connection, issue.issue_id, expected_issue_revision=checked.issue_revision,
                                request_id="direct", requested_by="test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: recover(), range(2)))
    assert attempts[0] == attempts[1]
    assert repository.list_recovery_attempts(issue.issue_id) == [attempts[0]]
    assert repository.get_issue(issue.issue_id).total_attempt_count == 1
