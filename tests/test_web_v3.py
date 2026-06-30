from __future__ import annotations

from pathlib import Path

from live_clipper import service
from live_clipper.config import RecordingSourceDefaultConfig, ServiceConfig, Settings
from live_clipper.mcp_tools import cleanup_confirm, delete_clip, delete_local_source
from live_clipper.utils import read_json, write_json
from live_clipper.web import WebPaths, handle_api_request


def _settings(input_dir: Path) -> Settings:
    return Settings(
        service=ServiceConfig(scan_interval_minutes=15),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=Path("/nas"),
            input_dir=input_dir,
            output_root=Path("output"),
        ),
    )


def _write_service_run(
    service_dir: Path,
    run_dir: Path,
    *,
    run_id: str = "run-1",
    phase: str = "rendered",
    local_source_path: Path | None = None,
    original_source_path: Path | None = None,
) -> dict:
    metadata = {
        "source_name": "recording.mkv",
        "pipeline": {},
    }
    if local_source_path is not None:
        metadata["pipeline"]["local_source_path"] = str(local_source_path)
    if original_source_path is not None:
        metadata["pipeline"]["original_source_path"] = str(original_source_path)
    write_json(run_dir / "run_metadata.json", metadata)
    write_json(run_dir / "selected_clips.json", [{"clip_id": "clip-1"}])
    (run_dir / "clips").mkdir(parents=True, exist_ok=True)
    (run_dir / "clips" / "clip-1.mp4").write_bytes(b"clip")
    run = {
        "run_id": run_id,
        "source_id": "default",
        "source_path": str(original_source_path or Path("/nas/recording.mkv")),
        "local_source_path": str(local_source_path) if local_source_path else None,
        "run_dir": str(run_dir),
        "fingerprint": "abc123",
        "phase": phase,
        "pid": None,
        "log_path": str(service_dir / "runs" / f"{run_id}.log"),
        "created_at": "2026-06-30T00:00:00+00:00",
        "updated_at": "2026-06-30T00:00:00+00:00",
        "last_error": None,
    }
    write_json(service_dir / "runs.json", {"runs": [run]})
    return run


def _web_paths(tmp_path: Path, input_dir: Path, service_dir: Path) -> WebPaths:
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=input_dir,
        service_dir=service_dir,
    )


def test_service_core_approves_and_rejects_confirmations_with_events(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    local_source = input_dir / "recording.mkv"
    original_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    original_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local")
    original_source.write_bytes(b"nas")
    _write_service_run(service_dir, run_dir, local_source_path=local_source, original_source_path=original_source)
    settings = _settings(input_dir)
    created = delete_clip("run-1", "clip-1.mp4", reason="bad clip", service_dir=service_dir)
    confirmation_id = created["confirmation_id"]

    approved = service.approve_confirmation(confirmation_id, settings=settings, service_dir=service_dir)
    rejected_seed = delete_local_source("run-1", reason="not now", settings=settings, service_dir=service_dir)
    rejected = service.reject_confirmation(
        rejected_seed["confirmation_id"],
        reason="keep source",
        service_dir=service_dir,
    )

    assert approved["ok"] is True
    assert approved["confirmation"]["status"] == "approved_executed"
    assert not (run_dir / "clips" / "clip-1.mp4").exists()
    assert rejected["confirmation"]["status"] == "rejected"
    confirmations = read_json(service_dir / "confirmations.json")["confirmations"]
    assert [item["status"] for item in confirmations] == ["approved_executed", "rejected"]
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "confirmation_executed" in events
    assert "confirmation_rejected" in events


def test_batch_confirmation_returns_per_item_results_and_continues_after_failure(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_service_run(service_dir, run_dir)
    settings = _settings(input_dir)
    good = delete_clip("run-1", "clip-1.mp4", reason="bad", service_dir=service_dir)
    stale = service.create_confirmation(
        action="delete_clip",
        run_id="run-1",
        target_path=run_dir / "clips" / "missing.mp4",
        reason="missing",
        risk_level="low",
        validation={"must_be_relative_to": str(run_dir / "clips"), "allowed_suffixes": [".mp4"]},
        service_dir=service_dir,
    )

    result = service.approve_confirmations(
        [good["confirmation_id"], stale["id"]],
        settings=settings,
        service_dir=service_dir,
    )

    assert result["ok"] is False
    assert [item["ok"] for item in result["results"]] == [True, False]
    assert result["results"][1]["error_code"] == "path_rejected"


def test_delete_local_source_approval_protects_nas_original(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    original_source = input_dir / "recording.mkv"
    original_source.parent.mkdir(parents=True)
    original_source.write_bytes(b"same file")
    _write_service_run(
        service_dir,
        run_dir,
        local_source_path=original_source,
        original_source_path=original_source,
    )
    settings = _settings(input_dir)
    created = delete_local_source("run-1", reason="danger", settings=settings, service_dir=service_dir)

    approved = service.approve_confirmation(created["confirmation_id"], settings=settings, service_dir=service_dir)

    assert approved["ok"] is False
    assert approved["error_code"] == "path_rejected"
    assert original_source.exists()


def test_cleanup_confirm_approval_deletes_only_current_deletable_targets(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    local_source = input_dir / "recording.mkv"
    original_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    original_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local")
    original_source.write_bytes(b"nas")
    audio = run_dir / "audio.wav"
    _write_service_run(service_dir, run_dir, local_source_path=local_source, original_source_path=original_source)
    audio.write_bytes(b"audio")
    settings = _settings(input_dir)
    created = cleanup_confirm("run-1", reason="cleanup", settings=settings, service_dir=service_dir)

    approved = service.approve_confirmation(created["confirmation_id"], settings=settings, service_dir=service_dir)

    assert approved["ok"] is True
    assert not audio.exists()
    assert not local_source.exists()
    assert original_source.exists()


def test_web_v3_service_runs_confirmations_events_and_settings_api(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_service_run(service_dir, run_dir, phase="needs_review")
    write_json(service_dir / "service.json", {"status": "running", "pid": 1234, "last_error": None})
    service.append_event(service_dir, "phase_changed", run_id="run-1", phase="needs_review")
    created = delete_clip("run-1", "clip-1.mp4", reason="bad", service_dir=service_dir)
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    paths = _web_paths(tmp_path, input_dir, service_dir)

    service_status = handle_api_request("GET", "/api/service", paths)[2]
    runs = handle_api_request("GET", "/api/runs?phase=needs_review", paths)[2]
    detail = handle_api_request("GET", "/api/runs/run-1", paths)[2]
    log = handle_api_request("GET", "/api/runs/run-1/log", paths)[2]
    confirmations = handle_api_request("GET", "/api/confirmations", paths)[2]
    events = handle_api_request("GET", "/api/events", paths)[2]
    settings = handle_api_request("GET", "/api/settings", paths)[2]

    assert service_status["running"] is True
    assert service_status["pending_confirmation_count"] == 1
    assert runs["runs"][0]["phase"] == "needs_review"
    assert detail["run"]["run_id"] == "run-1"
    assert detail["cleanup"]["targets"] == []
    assert log["ok"] is True
    assert confirmations["confirmations"][0]["id"] == created["confirmation_id"]
    assert events["events"][0]["type"] == "phase_changed"
    assert settings["settings"]["service"]["scan_interval_minutes"] == 30


def test_web_v3_approval_rejection_and_batch_api(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    local_source = input_dir / "recording.mkv"
    original_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    original_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local")
    original_source.write_bytes(b"nas")
    _write_service_run(service_dir, run_dir, local_source_path=local_source, original_source_path=original_source)
    first = delete_clip("run-1", "clip-1.mp4", reason="bad", service_dir=service_dir)
    second = service.create_confirmation(
        action="delete_clip",
        run_id="run-1",
        target_path=run_dir / "clips" / "missing.mp4",
        reason="stale",
        risk_level="low",
        validation={"must_be_relative_to": str(run_dir / "clips"), "allowed_suffixes": [".mp4"]},
        service_dir=service_dir,
    )
    paths = _web_paths(tmp_path, input_dir, service_dir)

    approve_status, _headers, approve_body = handle_api_request(
        "POST",
        f"/api/confirmations/{first['confirmation_id']}/approve",
        paths,
    )
    batch_status, _headers, batch_body = handle_api_request(
        "POST",
        "/api/confirmations/batch-approve",
        paths,
        body={"ids": [second["id"]]},
    )
    reject_seed = delete_local_source("run-1", reason="later", settings=_settings(input_dir), service_dir=service_dir)
    reject_status, _headers, reject_body = handle_api_request(
        "POST",
        f"/api/confirmations/{reject_seed['confirmation_id']}/reject",
        paths,
        body={"reason": "no"},
    )

    assert approve_status == 200
    assert approve_body["ok"] is True
    assert batch_status == 200
    assert batch_body["results"][0]["ok"] is False
    assert reject_status == 200
    assert reject_body["confirmation"]["status"] == "rejected"


def test_web_v3_old_destructive_clip_endpoint_creates_confirmation(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_service_run(service_dir, run_dir)
    clip = run_dir / "clips" / "clip-1.mp4"
    paths = _web_paths(tmp_path, input_dir, service_dir)

    _status, _headers, body = handle_api_request(
        "POST",
        "/api/runs/run-1/clips/clip-1.mp4/delete",
        paths,
    )

    assert body["status"] == "confirmation_required"
    assert clip.exists()


def test_web_v3_returns_structured_errors(tmp_path):
    paths = _web_paths(tmp_path, tmp_path / "input", tmp_path / "service")

    status, _headers, body = handle_api_request("GET", "/api/runs/missing", paths)

    assert status == 404
    assert body["ok"] is False
    assert body["error_code"] == "run_not_found"
