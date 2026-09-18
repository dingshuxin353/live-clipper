from __future__ import annotations

import hashlib
import json
from pathlib import Path

from resource_test_support import ready_project_config

from live_clipper.config import PathsConfig, Settings
from live_clipper.project_resources import resolve_parameter_snapshot
from live_clipper.project_result_api import ProjectResultAPI
from live_clipper.project_service import ProjectManager, open_project_repository


def result_api_fixture(tmp_path: Path, *, output_container: str = "mp4"):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    work_dir = tmp_path / "work"
    source_dir.mkdir()
    output_dir.mkdir()
    source = source_dir / "recording.mp4"
    source.write_bytes(b"source-media")
    settings = Settings(
        cheap_model_api_key="secret-key",
        paths=PathsConfig(work_dir=work_dir, output_root=output_dir),
    )
    repository = open_project_repository(tmp_path / "service")
    manager = ProjectManager(repository, settings)
    project = manager.create_project(
        name="高光项目",
        config=ready_project_config(repository, source_dir, output_dir),
        activation_state="active",
    )
    revision = repository.get_config_revision(project.project_id)
    assert revision is not None
    run = repository.create_normal_run(
        project_id=project.project_id,
        content_id=hashlib.sha256(source.read_bytes()).hexdigest(),
        trigger_source="manual",
        first_seen_path=str(source),
        latest_seen_path=str(source),
        parameter_snapshot=resolve_parameter_snapshot(revision.config, settings, repository=repository),
        config_revision=revision.revision,
        queued_at="2026-08-27T00:00:00Z",
    ).run
    repository.transition_run(run.run_id, status="processing", stage="review", event_type="started")
    session = repository.create_ai_review_session(
        run.run_id,
        attempt_number=1,
        resource_ref="legacy.analysis.default",
        model_name="model",
        strategy_version="auto_review_v1",
        parameter_snapshot={"private": "must-not-leak"},
        started_at="2026-08-27T00:00:01Z",
    )
    repository.register_verified_review(
        session.review_session_id,
        status="selected",
        decisions=[{
            "candidate_id": "candidate-1",
            "decision": "selected",
            "rank": 1,
            "candidate_type": "highlight",
            "source_start_ms": 1000,
            "source_end_ms": 4000,
            "selected_start_ms": 1000,
            "selected_end_ms": 4000,
            "reason": "完整叙事",
            "internal_sort_value": 9.9,
        }],
        outputs=[{
            "output_id": "output-1",
            "candidate_id": "candidate-1",
            "display_order": 1,
            "relative_path": "output-1.mp4",
            "file_name": "output-1.mp4",
        }],
        materials=[{
            "material_id": "material-1",
            "output_id": "output-1",
            "title_candidates": [{"title_id": "title-1", "text": "标题"}],
            "preferred_title_id": "title-1",
            "description": "描述",
            "tags": ["标签"],
        }],
        overall_summary="值得发布",
        evidence_sha256="a" * 64,
        completed_at="2026-08-27T00:00:02Z",
    )
    media = b"0123456789abcdef"
    media_path = output_dir / "output-1.mp4"
    media_path.write_bytes(media)
    repository.update_output_and_reproject_result("output-1", status="rendering")
    repository.update_output_and_reproject_result(
        "output-1",
        status="ready",
        media_metadata={
            "duration_ms": 3000,
            "width": 1920,
            "height": 1080,
            "container": output_container,
            "video_codec": "h264",
            "byte_size": len(media),
        },
        occurred_at="2026-08-27T00:00:03Z",
    )
    integrity = work_dir / "projects" / project.project_id / "runs" / run.run_id / "outputs" / "output-1"
    integrity.mkdir(parents=True)
    (integrity / "media_integrity.json").write_text(json.dumps({
        "format_version": 1,
        "output_id": "output-1",
        "sha256": hashlib.sha256(media).hexdigest(),
        "media_metadata": {
            "duration_ms": 3000,
            "width": 1920,
            "height": 1080,
            "container": output_container,
            "video_codec": "h264",
            "byte_size": len(media),
        },
    }), encoding="utf-8")
    api = ProjectResultAPI(repository, settings, service_dir=tmp_path / "service", config_path=tmp_path / "live-clipper.toml")
    return repository, api, project, run, media_path, media


def test_result_dto_clips_cursor_and_seen_are_revision_bound(tmp_path):
    repository, api, _project, run, _path, _media = result_api_fixture(tmp_path)

    status, clips = api.handle("GET", "/api/clips?view=new&limit=1")
    assert status == 200
    assert clips["unseen_result_count"] == 1
    assert clips["results"][0]["primary_output_id"] == "output-1"
    status, detail = api.handle("GET", f"/api/runs/{run.run_id}/result")
    serialized = json.dumps(detail)
    assert status == 200 and "must-not-leak" not in serialized and "internal_sort_value" not in serialized

    revision = repository.get_run_result(run.run_id).result_revision
    status, seen = api.handle("POST", f"/api/runs/{run.run_id}/result/seen", body={
        "request_id": "seen-1",
        "expected_result_revision": revision,
    })
    assert status == 200 and seen["unseen_result_count"] == 0 and seen["result"]["seen"]
    status, repeated = api.handle("POST", f"/api/runs/{run.run_id}/result/seen", body={
        "request_id": "seen-1",
        "expected_result_revision": revision,
    })
    assert status == 200 and repeated["reused"] is True


def test_material_patch_preserves_candidate_ids_and_is_idempotent(tmp_path):
    _repository, api, _project, _run, _path, _media = result_api_fixture(tmp_path)
    payload = {
        "request_id": "material-1",
        "expected_revision": 1,
        "titles": [{"title_id": "title-1", "text": "新标题"}],
        "preferred_title_id": "title-1",
        "description": "新描述",
        "tags": ["#高光", "高光", "直播"],
    }
    status, updated = api.handle("PATCH", "/api/outputs/output-1/material", body=payload)
    assert status == 200
    assert updated["material"]["titles"][0]["text"] == "新标题"
    assert updated["material"]["tags"] == ["高光", "直播"]
    status, repeated = api.handle("PATCH", "/api/outputs/output-1/material", body=payload)
    assert status == 200 and repeated["reused"] is True

    status, invalid = api.handle("PATCH", "/api/outputs/output-1/material", body={
        **payload,
        "request_id": "material-2",
        "expected_revision": 2,
        "titles": [{"title_id": "invented", "text": "标题"}],
    })
    assert status == 422 and invalid["error"]["code"] == "validation_failed"


def test_material_and_replay_identity_are_atomic(tmp_path, monkeypatch):
    repository, api, *_ = result_api_fixture(tmp_path)
    payload = {
        'request_id': 'atomic-material', 'expected_revision': 1,
        'titles': [{'title_id': 'title-1', 'text': '完整提交'}],
        'preferred_title_id': 'title-1', 'description': '新描述', 'tags': ['高光'],
    }
    original = api._save_idempotency

    def interrupted(*args):
        raise RuntimeError('interrupted before operation record')

    monkeypatch.setattr(api, '_save_idempotency', interrupted)
    status, _ = api.handle('PATCH', '/api/outputs/output-1/material', body=payload)
    assert status == 500
    assert repository.get_output_material('output-1').material_revision == 1
    monkeypatch.setattr(api, '_save_idempotency', original)
    status, saved = api.handle('PATCH', '/api/outputs/output-1/material', body=payload)
    assert status == 200
    status, replay = api.handle('PATCH', '/api/outputs/output-1/material', body=payload)
    assert status == 200 and replay['reused']
    assert replay['material'] == saved['material']


def test_issue_recheck_and_recovery_replay_before_live_revision_checks(tmp_path, monkeypatch):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    repository.transition_run(run.run_id, status="failed", stage="render", event_type="failed")
    issue = repository.discover_issue(
        issue_code="output_unwritable", category="storage", scope_type="run",
        project_id=project.project_id, run_id=run.run_id, issue_group_key="output",
        recovery_capability="continue_run", reuse_stages=("review",), redo_stages=("render",),
    )
    monkeypatch.setattr("live_clipper.project_recovery._default_check", lambda *_: {"ok": True})
    body = {"request_id": "check-original", "expected_issue_revision": issue.issue_revision}
    status, checked = api.handle("POST", f"/api/issues/{issue.issue_id}/recheck", body=body)
    assert status == 200
    revision = checked["issue"]["issue_revision"]
    assert revision > issue.issue_revision
    status, replay = api.handle("POST", f"/api/issues/{issue.issue_id}/recheck", body=body)
    assert status == 200 and replay["reused"] and replay["issue"]["issue_revision"] == revision
    recovery = {"request_id": "recover-original", "expected_issue_revision": revision}
    status, accepted = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body=recovery)
    assert status == 200
    status, replay = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body=recovery)
    assert status == 200 and replay["recovery_attempt_id"] == accepted["recovery_attempt_id"]
    assert len(repository.list_recovery_attempts(issue.issue_id)) == 1
    status, _ = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={**recovery, "expected_issue_revision": revision + 1})
    assert status == 409


def test_group_recheck_keeps_atomic_original_identity(tmp_path, monkeypatch):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="output_unwritable", category="storage", scope_type="run",
        project_id=project.project_id, run_id=run.run_id, issue_group_key="output",
        recovery_capability="continue_run",
    )
    monkeypatch.setattr("live_clipper.project_recovery._default_check", lambda *_: {"ok": True})
    payload = {"request_id": "group-original", "issue_revisions": {issue.issue_id: issue.issue_revision}}
    save = api._save_idempotency
    monkeypatch.setattr(api, "_save_idempotency", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
    assert api.handle("POST", "/api/issue-groups/output/recheck", body=payload)[0] == 500
    assert repository.get_issue(issue.issue_id).issue_revision == issue.issue_revision
    monkeypatch.setattr(api, "_save_idempotency", save)
    assert api.handle("POST", "/api/issue-groups/output/recheck", body=payload)[0] == 200
    revision = repository.get_issue(issue.issue_id).issue_revision
    replay = api.handle("POST", "/api/issue-groups/output/recheck", body=payload)
    assert replay[0] == 200 and replay[1]["reused"]
    assert repository.get_issue(issue.issue_id).issue_revision == revision
