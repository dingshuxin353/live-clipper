from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from live_clipper.config import Settings
from live_clipper.media_probe import MediaMetadata
from live_clipper.project_domain import default_project_config
from live_clipper.project_resources import resolve_parameter_snapshot
from live_clipper.project_result_runtime import (
    ProjectReviewError,
    ProjectWorkerPool,
    reconcile_review_evidence,
    run_project_review,
)
from live_clipper.project_service import ProjectManager, open_project_repository
from live_clipper.utils import read_json, write_json


def _candidate(candidate_id: str, *, score: float = 9.0, start: float = 10, end: float = 20) -> dict:
    return {
        "id": candidate_id,
        "start": start,
        "end": end,
        "score": score,
        "clip_type": "highlight",
        "hook": f"hook-{candidate_id}",
        "core_value": "value",
        "reason": "candidate reason",
        "risk": None,
        "suggested_context_before": 0,
        "suggested_context_after": 0,
    }


def _project_run(tmp_path, *, candidates: list[dict]):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    work_dir = tmp_path / "work"
    source_dir.mkdir()
    output_dir.mkdir()
    source_path = source_dir / "recording.mp4"
    source_path.write_bytes(b"source")
    settings = Settings(cheap_model_api_key="fake-key")
    repository = open_project_repository(tmp_path / "service")
    manager = ProjectManager(repository, settings)
    project = manager.create_project(
        name="项目",
        config=default_project_config(source_dir, output_dir),
        activation_state="active",
    )
    revision = repository.get_config_revision(project.project_id)
    assert revision is not None
    snapshot = resolve_parameter_snapshot(revision.config, settings)
    run = repository.create_normal_run(
        project_id=project.project_id,
        content_id=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        trigger_source="manual",
        first_seen_path=str(source_path),
        latest_seen_path=str(source_path),
        parameter_snapshot=snapshot,
        config_revision=revision.revision,
    ).run
    run_dir = work_dir / "projects" / project.project_id / "runs" / run.run_id
    run_dir.mkdir(parents=True)
    write_json(run_dir / "merged_candidates.json", candidates)
    write_json(run_dir / "codex_brief.json", {"source_name": source_path.name, "candidates": candidates})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    repository.transition_run(run.run_id, status="processing", stage="review", event_type="review_ready")
    return repository, project, repository.get_run(run.run_id), run_dir, output_dir


def _selected(candidate_id: str, *, start: float = 10, end: float = 20) -> dict:
    return {
        "candidate_id": candidate_id,
        "decision": "selected",
        "rank": 1,
        "reason": "完整且有价值",
        "rejection_reason_code": None,
        "selected_clip": {
            "clip_id": candidate_id,
            "source_start": start,
            "source_end": end,
            "title": "内部标题",
            "remove_ranges": [],
        },
        "material": {
            "titles": ["标题一", "标题二"],
            "description": "发布描述",
            "tags": ["高光"],
        },
    }


def _rejected(candidate_id: str, *, rank: int = 1) -> dict:
    return {
        "candidate_id": candidate_id,
        "decision": "rejected",
        "rank": rank,
        "reason": "价值不足",
        "rejection_reason_code": "low_value",
        "selected_clip": None,
        "material": None,
    }


def test_v1_project_request_is_saved_as_v2_with_secret_free_review_snapshot(tmp_path):
    repository, project, run, _run_dir, _output = _project_run(tmp_path, candidates=[])

    revision = repository.get_config_revision(project.project_id)

    assert revision is not None and revision.schema_version == 2
    assert revision.config["processing"]["review_strategy"] == "ai_auto"
    assert revision.config["resources"]["review_ref"] == revision.config["resources"]["analysis_ref"]
    assert run.parameter_snapshot["schema_version"] == 2
    assert run.parameter_snapshot["processing"]["review_policy_version"] == "auto_review_v1"
    assert run.parameter_snapshot["retry_policy"]["ai"]["delays_seconds"] == [30, 120]
    assert "fake-key" not in json.dumps(run.parameter_snapshot)


def test_structured_review_registers_selected_outputs_and_atomic_evidence(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("candidate-1")])

    result = run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "值得发布",
            "warnings": [],
            "decisions": [_selected("candidate-1")],
        },
    )

    assert result.status == "selected"
    assert (run_dir / "review_result.json").is_file()
    assert not (run_dir / "review_result.tmp.json").exists()
    assert read_json(run_dir / "selected_clips.json")[0]["clip_id"] == "candidate-1"
    sessions = repository.list_ai_review_sessions(run.run_id)
    assert sessions[0].evidence_sha256 == hashlib.sha256((run_dir / "review_result.json").read_bytes()).hexdigest()
    outputs = repository.list_run_outputs(run.run_id)
    assert len(outputs) == 1 and outputs[0].status == "pending"
    assert repository.get_output_material(outputs[0].output_id).title_candidates[0]["text"] == "标题一"


def test_review_evidence_and_material_redact_credentials_and_user_paths(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("candidate-1")])
    selected = _selected("candidate-1")
    selected["selected_clip"]["title"] = "sk-secretvalue"
    selected["material"] = {
        "titles": ["sk-secretvalue"],
        "description": "token=private-value /Users/example/private/file",
        "tags": ["safe"],
    }

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "password=private-value",
            "warnings": [{"summary": "sk-anothersecret"}],
            "decisions": [selected],
        },
    )

    evidence = (run_dir / "review_result.json").read_text(encoding="utf-8")
    material = repository.get_output_material(repository.list_run_outputs(run.run_id)[0].output_id)
    assert "private-value" not in evidence
    assert "/Users/example" not in evidence
    assert "secretvalue" not in json.dumps(material.__dict__)


def test_candidate_limit_creates_system_rejections_for_unsent_candidates(tmp_path):
    candidates = [_candidate("high", score=9), _candidate("low", score=1, start=30, end=40)]
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=candidates)

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        max_candidates=1,
        adapter=lambda payload: {
            "format_version": 1,
            "overall_summary": "零成片",
            "warnings": [],
            "decisions": [_rejected(payload["candidates"][0]["candidate_id"])],
        },
    )

    session = repository.list_ai_review_sessions(run.run_id)[0]
    decisions = repository.get_candidate_decisions(session.review_session_id)
    assert [(item.candidate_id, item.rejection_reason_code) for item in decisions] == [
        ("high", "low_value"),
        ("low", "candidate_limit"),
    ]
    assert repository.get_run(run.run_id).status == "completed"
    assert repository.get_run_result(run.run_id).result_type == "no_clip"


@pytest.mark.parametrize("adapter_result", [None, {}, {"format_version": 1, "overall_summary": "", "warnings": [], "decisions": []}])
def test_invalid_or_empty_model_response_is_an_issue_not_no_clip(tmp_path, adapter_result):
    repository, project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("candidate-1")])

    with pytest.raises(ProjectReviewError):
        run_project_review(repository, run.run_id, run_dir=run_dir, adapter=lambda _payload: adapter_result)

    assert repository.get_run_result(run.run_id) is None
    assert repository.get_run(run.run_id).status == "failed"
    issues = repository.list_issues(project_id=project.project_id, run_id=run.run_id, active_only=True)
    assert [item.issue_code for item in issues] == ["ai_review_invalid"]
    assert not (run_dir / "review_result.json").exists()


@pytest.mark.parametrize(
    "adapter_result",
    [
        {
            "format_version": 1,
            "warnings": [],
            "decisions": [_rejected("candidate-1")],
        },
        {
            "format_version": 1,
            "overall_summary": "没有可发布片段",
            "warnings": [],
            "decisions": [{key: value for key, value in _rejected("candidate-1").items() if key != "rank"}],
        },
        {
            "format_version": 1,
            "overall_summary": "没有可发布片段",
            "warnings": [],
            "decisions": [{key: value for key, value in _rejected("candidate-1").items() if key != "reason"}],
        },
    ],
    ids=["missing-overall-summary", "missing-rank", "missing-reason"],
)
def test_model_contract_fields_are_not_invented_when_missing(tmp_path, adapter_result):
    repository, project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("candidate-1")])

    with pytest.raises(ProjectReviewError, match="does not match review_result"):
        run_project_review(repository, run.run_id, run_dir=run_dir, adapter=lambda _payload: adapter_result)

    assert repository.get_run_result(run.run_id) is None
    assert repository.get_run(run.run_id).status == "failed"
    issues = repository.list_issues(project_id=project.project_id, run_id=run.run_id, active_only=True)
    assert [item.issue_code for item in issues] == ["ai_review_invalid"]


def test_model_must_cover_every_candidate_it_received(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(
        tmp_path,
        candidates=[_candidate("one"), _candidate("two", start=30, end=40)],
    )

    with pytest.raises(ProjectReviewError, match="cover"):
        run_project_review(
            repository,
            run.run_id,
            run_dir=run_dir,
            adapter=lambda _payload: {
                "format_version": 1,
                "overall_summary": "incomplete",
                "warnings": [],
                "decisions": [_rejected("one")],
            },
        )


def test_transient_ai_failure_records_bounded_retry_without_raw_error(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    current = datetime(2026, 8, 27, tzinfo=UTC)

    with pytest.raises(ProjectReviewError):
        run_project_review(
            repository,
            run.run_id,
            run_dir=run_dir,
            adapter=lambda _payload: (_ for _ in ()).throw(TimeoutError("sk-secret timeout")),
            clock=lambda: current,
        )

    issue = repository.list_issues(run_id=run.run_id, active_only=True)[0]
    assert issue.issue_code == "ai_review_failed"
    assert issue.status == "retrying"
    assert issue.automatic_attempt_count == 1
    assert datetime.fromisoformat(issue.next_retry_at.replace("Z", "+00:00")).timestamp() - current.timestamp() == 30
    assert "sk-secret" not in json.dumps(issue.__dict__)


def test_registered_review_with_missing_evidence_is_invalidated(tmp_path):
    repository, project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "none",
            "warnings": [],
            "decisions": [_rejected("one")],
        },
    )
    (run_dir / "review_result.json").unlink()

    state = reconcile_review_evidence(repository, run.run_id, run_dir=run_dir)

    assert state == "invalid"
    assert repository.list_ai_review_sessions(run.run_id)[0].status == "invalid"
    assert repository.get_run(run.run_id).status == "failed"
    assert any(issue.issue_code == "ai_review_invalid" for issue in repository.list_issues(project_id=project.project_id))


def test_evidence_landed_before_database_failure_is_registered_on_retry(tmp_path, monkeypatch):
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    result = {
        "format_version": 1,
        "overall_summary": "none",
        "warnings": [],
        "decisions": [_rejected("one")],
    }
    real_register = repository.register_verified_review
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic transaction boundary")
        return real_register(*args, **kwargs)

    monkeypatch.setattr(repository, "register_verified_review", fail_once)
    with pytest.raises(ProjectReviewError, match="durable registration"):
        run_project_review(repository, run.run_id, run_dir=run_dir, adapter=lambda _payload: result)

    assert (run_dir / "review_result.json").is_file()
    assert repository.list_ai_review_sessions(run.run_id)[0].status == "running"
    assert reconcile_review_evidence(repository, run.run_id, run_dir=run_dir) == "registration_pending"

    session = run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: read_json(run_dir / "review_result.json"),
    )

    assert session.status == "no_clip"
    assert repository.get_run_result(run.run_id).result_type == "no_clip"


def test_worker_tick_is_nonblocking_and_serializes_review_then_render(tmp_path):
    repository, _project, run, run_dir, _output = _project_run(tmp_path, candidates=[_candidate("one")])
    pool = ProjectWorkerPool(
        review_adapter=lambda _settings, _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
        renderer=lambda _source, _transcript, _clip, _work, partial: partial.write_bytes(b"clip"),
        probe=lambda path: MediaMetadata(1000, 1280, 720, "mp4", "h264", path.stat().st_size),
    )
    settings = Settings(cheap_model_api_key="fake-key")
    try:
        first = pool.tick(repository, settings, work_dir=tmp_path / "work")
        assert first.scheduled_review_run_ids == (run.run_id,)
        pool.wait_for_idle()
        second = pool.tick(repository, settings, work_dir=tmp_path / "work")
        assert second.scheduled_render_run_ids == (run.run_id,)
        pool.wait_for_idle()
        pool.tick(repository, settings, work_dir=tmp_path / "work")
    finally:
        pool.shutdown()

    assert repository.get_run(run.run_id).status == "completed"
    assert repository.get_run_result(run.run_id).result_type == "clips_ready"
    assert not (run_dir / "review_result.tmp.json").exists()
