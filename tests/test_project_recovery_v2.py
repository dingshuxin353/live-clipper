from __future__ import annotations

from test_project_auto_review_v2 import _candidate, _project_run, _selected

from live_clipper.media_probe import MediaMetadata
from live_clipper.project_recovery import continue_run, recheck_issue, retry_material, retry_output
from live_clipper.project_result_runtime import render_project_outputs, run_project_review


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

    retry_output(
        repository,
        checked.issue_id,
        expected_issue_revision=checked.issue_revision,
        request_id="retry-output-1",
        requested_by="test",
    )

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
