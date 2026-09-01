from __future__ import annotations

import hashlib
import sqlite3

import pytest

from live_clipper.config import PathsConfig, Settings
from live_clipper.project_api import ProjectAPI
from live_clipper.project_domain import default_project_config
from live_clipper.project_resources import resolve_parameter_snapshot
from live_clipper.project_runtime import dispatch_queued, run_work_dir


def _api_with_terminal_run(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    work = tmp_path / "work"
    source.mkdir()
    output.mkdir()
    source_file = source / "recording.mp4"
    source_file.write_bytes(b"same recording bytes")
    settings = Settings(cheap_model_api_key="fake", paths=PathsConfig(work_dir=work))
    api = ProjectAPI(tmp_path / "service", settings, auth_context="bearer")
    project = api.manager.create_project(
        name="Project",
        config=default_project_config(source, output),
        activation_state="active",
    )
    revision = api.repository.get_config_revision(project.project_id)
    assert revision is not None
    run = api.repository.create_normal_run(
        project_id=project.project_id,
        content_id=hashlib.sha256(source_file.read_bytes()).hexdigest(),
        trigger_source="manual",
        first_seen_path=str(source_file),
        latest_seen_path=str(source_file),
        parameter_snapshot=resolve_parameter_snapshot(revision.config, settings),
    ).run
    run = api.repository.transition_run(run.run_id, status="completed", stage="render", event_type="completed")
    return api, run, source_file, work


def test_preflight_is_read_only_and_reprocess_is_atomic_idempotent_and_versioned(tmp_path):
    api, origin, source, _work = _api_with_terminal_run(tmp_path)
    before = (
        len(api.repository.list_runs()),
        len(api.repository.list_issues()),
        len(api.repository.list_workspace_events()),
    )

    status, preflight = api.handle("GET", f"/api/runs/{origin.run_id}/reprocess-preflight")

    assert status == 200 and preflight["can_reprocess"]
    assert preflight["source"]["content_id"] == origin.content_id
    assert preflight["next_processing_sequence"] == 2
    assert before == (
        len(api.repository.list_runs()),
        len(api.repository.list_issues()),
        len(api.repository.list_workspace_events()),
    )
    status, created = api.handle(
        "POST",
        f"/api/runs/{origin.run_id}/reprocess",
        body={"request_id": "rerun-1", "expected_preflight_revision": preflight["preflight_revision"]},
    )
    assert status == 201 and created["created"]
    rerun = api.repository.get_run(created["run"]["run_id"])
    assert rerun is not None
    assert rerun.processing_sequence == 2 and rerun.origin_run_id == origin.run_id
    assert rerun.parameter_snapshot["source"] == {
        "bytes": source.stat().st_size,
        "content_id": origin.content_id,
        "mtime_ns": source.stat().st_mtime_ns,
    }
    status, repeated = api.handle(
        "POST",
        f"/api/runs/{origin.run_id}/reprocess",
        body={"request_id": "rerun-1", "expected_preflight_revision": preflight["preflight_revision"]},
    )
    assert status == 200 and repeated["run"]["run_id"] == rerun.run_id
    assert repeated["reuse_reason"] == "idempotent_request"

    status, active_preflight = api.handle("GET", f"/api/runs/{origin.run_id}/reprocess-preflight")
    assert status == 200 and active_preflight["active_run"]["run_id"] == rerun.run_id
    status, reused = api.handle(
        "POST",
        f"/api/runs/{origin.run_id}/reprocess",
        body={"request_id": "rerun-2", "expected_preflight_revision": active_preflight["preflight_revision"]},
    )
    assert status == 200 and reused["reuse_reason"] == "active_run"
    assert reused["run"]["run_id"] == rerun.run_id and len(api.repository.list_runs()) == 2
    status, versions = api.handle("GET", f"/api/runs/{origin.run_id}/versions")
    assert status == 200
    assert [item["processing_sequence"] for item in versions["versions"]] == [2, 1]


def test_reprocess_transaction_rolls_back_run_event_and_idempotency_together(tmp_path):
    api, origin, _source, _work = _api_with_terminal_run(tmp_path)
    preflight = api.reprocess.preflight(origin.run_id)
    api.repository.connection.executescript(
        """CREATE TRIGGER fail_reprocess_event BEFORE INSERT ON workspace_events
           WHEN NEW.payload_json LIKE '%origin_run_id%'
           BEGIN SELECT RAISE(ABORT, 'injected reprocess event failure'); END;"""
    )

    with pytest.raises(sqlite3.IntegrityError):
        api.reprocess.create(
            origin.run_id,
            request_id="rollback-1",
            expected_preflight_revision=preflight["preflight_revision"],
        )

    assert len(api.repository.list_runs()) == 1
    assert api.repository.get_idempotency_key(f"run_reprocess:{origin.run_id}", "rollback-1") is None


def test_dispatch_rechecks_source_facts_before_creating_work_directory(tmp_path):
    api, origin, source, work = _api_with_terminal_run(tmp_path)
    preflight = api.reprocess.preflight(origin.run_id)
    created, status = api.reprocess.create(
        origin.run_id,
        request_id="dispatch-1",
        expected_preflight_revision=preflight["preflight_revision"],
    )
    assert status == 201
    rerun = api.repository.get_run(created["run"]["run_id"])
    source.write_bytes(b"changed after queue")
    called = False

    def processor(_run, _target):
        nonlocal called
        called = True

    report = dispatch_queued(api.repository, work_dir=work, processor=processor)

    assert report.failed_run_ids == (rerun.run_id,)
    assert not called and not run_work_dir(work, rerun).exists()
    assert api.repository.get_run(rerun.run_id).error_code == "source_identity_mismatch"


def test_source_repair_resolves_issue_and_updates_only_latest_seen_path(tmp_path):
    api, origin, source, _work = _api_with_terminal_run(tmp_path)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(source.read_bytes())
    source.unlink()

    status, response = api.handle("POST", f"/api/runs/{origin.run_id}/reprocess-source-repair", body={})
    assert status == 201 and not response["reused"]
    issue_id = response["issue"]["issue_id"]
    issue = api.repository.get_issue(issue_id)
    status, selection = api.handle(
        "POST",
        "/api/desktop/file-selections",
        body={"issue_id": issue_id, "kind": "source", "selected_path": str(replacement)},
    )
    assert status == 201
    status, repaired_response = api.handle(
        "POST",
        f"/api/issues/{issue_id}/source",
        body={
            "request_id": "repair-source-1",
            "expected_issue_revision": issue.issue_revision,
            "selection_token": selection["selection_token"],
        },
    )

    repaired = api.repository.get_run(origin.run_id)
    assert status == 200 and repaired_response["issue"]["status"] == "resolved"
    assert repaired.latest_seen_path == str(replacement.resolve())
    assert repaired.first_seen_path == origin.first_seen_path
    assert repaired.parameter_snapshot == origin.parameter_snapshot
    assert not api.repository.list_recovery_attempts(issue.issue_id)


def test_preflight_rejects_non_terminal_runs_and_strict_requests(tmp_path):
    api, origin, _source, _work = _api_with_terminal_run(tmp_path)
    api.repository.transition_run(origin.run_id, status="processing", stage="review", event_type="resumed")

    status, preflight = api.handle("GET", f"/api/runs/{origin.run_id}/reprocess-preflight")
    assert status == 200 and not preflight["can_reprocess"]
    assert preflight["blockers"][0]["code"] == "run_not_terminal"
    status, invalid = api.handle(
        "POST",
        f"/api/runs/{origin.run_id}/reprocess",
        body={"request_id": "x", "expected_preflight_revision": preflight["preflight_revision"], "extra": True},
    )
    assert status == 422 and invalid["error"]["code"] == "validation_failed"
