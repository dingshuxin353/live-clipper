from __future__ import annotations

import json
from pathlib import Path

import pytest

from live_clipper.project_domain import default_project_config
from live_clipper.project_result_index import (
    apply_project_result_index_plan,
    build_project_result_index_plan,
    inspect_project_result_artifacts,
)
from live_clipper.project_storage import ProjectRepository


def _legacy_run(repo: ProjectRepository, tmp_path: Path, content_id: str = "legacy"):
    project = repo.create_project("P", default_project_config(tmp_path / "source", tmp_path / "output"))
    work_root = tmp_path / "work" / "projects" / project.project_id
    run = repo.create_normal_run(
        project_id=project.project_id,
        content_id=content_id,
        trigger_source="legacy_import",
        first_seen_path="/recordings/source.mp4",
        latest_seen_path="/recordings/source.mp4",
        parameter_snapshot={"work_dir": str(work_root)},
    ).run
    workspace = work_root / "runs" / run.run_id
    workspace.mkdir(parents=True)
    return project, run, workspace


def _write_trusted_result(workspace: Path):
    (workspace / "clips").mkdir()
    (workspace / "clips" / "clip-1.mp4").write_bytes(b"")
    (workspace / "selected_clips.json").write_text(
        json.dumps(
            [
                {
                    "clip_id": "clip-1",
                    "source_start": 1.0,
                    "source_end": 4.0,
                    "title": "历史片段",
                }
            ]
        ),
        encoding="utf-8",
    )
    (workspace / "edit_decision_list.json").write_text(
        json.dumps(
            [
                {
                    "clip_id": "clip-1",
                    "file_name": "clip-1.mp4",
                    "duration_ms": 3000,
                    "width": 1920,
                    "height": 1080,
                    "container": "mp4",
                    "video_codec": "h264",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_explicit_index_plan_is_deterministic_atomic_and_idempotent(tmp_path):
    repo = ProjectRepository(tmp_path / "service")
    _project, run, workspace = _legacy_run(repo, tmp_path)
    _write_trusted_result(workspace)
    inspection = inspect_project_result_artifacts(repo, run.run_id)
    first_plan = build_project_result_index_plan(inspection)
    second_plan = build_project_result_index_plan(inspect_project_result_artifacts(repo, run.run_id))
    assert first_plan == second_plan
    assert str(tmp_path) not in repr(inspection) and str(tmp_path) not in repr(first_plan)

    first = apply_project_result_index_plan(
        repo, first_plan, occurred_at="2026-08-26T00:00:00Z"
    )
    second = apply_project_result_index_plan(
        repo, second_plan, occurred_at="2026-08-26T00:00:01Z"
    )
    assert first.applied and second.already_applied
    assert first.result_type == second.result_type == "clips_ready"
    result = repo.get_run_result(run.run_id)
    assert result is not None and result.source_kind == "indexed_v1" and result.result_revision == 1
    assert len(repo.list_run_outputs(run.run_id)) == 1
    assert len(repo.list_issues(run_id=run.run_id)) == 1


def test_empty_selection_never_becomes_no_clip(tmp_path):
    repo = ProjectRepository(tmp_path / "service")
    _project, run, workspace = _legacy_run(repo, tmp_path)
    (workspace / "selected_clips.json").write_text("[]", encoding="utf-8")
    (workspace / "edit_decision_list.json").write_text("[]", encoding="utf-8")
    plan = build_project_result_index_plan(inspect_project_result_artifacts(repo, run.run_id))
    assert plan.review_session_id is None
    assert any(item["code"] == "empty_selection_unproven" for item in plan.report)
    applied = apply_project_result_index_plan(repo, plan)
    assert applied.result_type is None
    assert repo.get_run_result(run.run_id) is None
    assert repo.list_issues(run_id=run.run_id)[0].issue_code == "legacy_judgement_unavailable"


def test_missing_clip_preserves_output_identity_and_reports_unavailable(tmp_path):
    repo = ProjectRepository(tmp_path / "service")
    _project, run, workspace = _legacy_run(repo, tmp_path)
    _write_trusted_result(workspace)
    (workspace / "clips" / "clip-1.mp4").unlink()
    plan = build_project_result_index_plan(inspect_project_result_artifacts(repo, run.run_id))
    output_id = plan.outputs[0]["output_id"]
    applied = apply_project_result_index_plan(repo, plan)
    assert applied.result_type == "unavailable"
    assert repo.get_run_output(output_id).status == "missing"
    assert {issue.issue_code for issue in repo.list_issues(run_id=run.run_id)} == {
        "output_missing",
        "legacy_judgement_unavailable",
    }


def test_index_apply_fault_rolls_back_all_objects(tmp_path):
    repo = ProjectRepository(tmp_path / "service")
    _project, run, workspace = _legacy_run(repo, tmp_path)
    _write_trusted_result(workspace)
    plan = build_project_result_index_plan(inspect_project_result_artifacts(repo, run.run_id))

    def fail(phase: str):
        if phase == "before_commit":
            raise RuntimeError("fault")

    with pytest.raises(RuntimeError, match="fault"):
        apply_project_result_index_plan(repo, plan, fault_injection=fail)
    assert repo.get_run_result(run.run_id) is None
    assert repo.list_run_outputs(run.run_id) == []
    assert repo.list_issues(run_id=run.run_id) == []
    assert repo.get_idempotency_key(f"project_result_index:{run.run_id}", plan.evidence_hash) is None


def test_indexer_rejects_workspace_not_bound_by_snapshot(tmp_path):
    repo = ProjectRepository(tmp_path / "service")
    _project, run, workspace = _legacy_run(repo, tmp_path)
    _write_trusted_result(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="controlled reference"):
        inspect_project_result_artifacts(repo, run.run_id, run_workspace=outside)
