from __future__ import annotations

import os
from datetime import datetime, timedelta

from live_clipper import mcp_tools
from live_clipper.config import RecordingSourceDefaultConfig, ServiceConfig, Settings
from live_clipper.utils import read_json, write_json


def _candidate() -> dict:
    return {
        "id": "clip-1",
        "start": 10.0,
        "end": 20.0,
        "score": 9.0,
        "clip_type": "highlight",
        "hook": "hook",
        "core_value": "value",
        "reason": "reason",
        "suggested_context_before": 2.0,
        "suggested_context_after": 2.0,
    }


def _selection() -> list[dict]:
    return [
        {
            "clip_id": "clip-1",
            "source_start": 10.0,
            "source_end": 20.0,
            "title": "clip title",
            "remove_ranges": [],
        }
    ]


def _write_run(service_dir, run_dir, *, phase="needs_review", run_id="run-1", local_source_path=None):
    run = {
        "run_id": run_id,
        "source_id": "default",
        "source_path": str(run_dir / "source.mp4"),
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


def test_read_tools_return_service_state_runs_detail_logs_and_review_package(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    run = _write_run(service_dir, run_dir)
    write_json(service_dir / "service.json", {"status": "running", "pid": 1234})
    write_json(service_dir / "confirmations.json", {"confirmations": [{"id": "confirm-1", "status": "pending"}]})
    (service_dir / "runs").mkdir()
    (service_dir / "runs" / "run-1.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    write_json(run_dir / "codex_brief.json", {"candidates": [_candidate()]})
    (run_dir / "codex_review.md").write_text("# Review\n", encoding="utf-8")
    write_json(run_dir / "selected_clips.template.json", _selection())
    write_json(run_dir / "refined_candidates.json", [_candidate()])
    write_json(run_dir / "selected_clips.json", _selection())
    (run_dir / "clips").mkdir(parents=True)
    (run_dir / "clips" / "clip-1.mp4").write_bytes(b"clip")
    monkeypatch.setattr(mcp_tools.service, "pid_is_running", lambda pid: True)

    status = mcp_tools.get_service_status(service_dir=service_dir)
    runs = mcp_tools.list_runs(phase="needs_review", service_dir=service_dir)
    detail = mcp_tools.get_run_detail("run-1", service_dir=service_dir)
    log = mcp_tools.get_run_log("run-1", lines=2, service_dir=service_dir)
    package = mcp_tools.get_review_package("run-1", service_dir=service_dir)

    assert status["ok"] is True
    assert status["pending_confirmation_count"] == 1
    assert runs["runs"][0]["run_id"] == "run-1"
    assert detail["run"]["run_dir"] == str(run_dir)
    assert detail["files"]["codex_brief.json"]["exists"] is True
    assert detail["selected_count"] == 1
    assert detail["rendered_clip_count"] == 1
    assert log["log"] == "two\nthree"
    assert package["files"]["codex_brief.json"]["content"]["candidates"][0]["id"] == "clip-1"
    assert package["files"]["codex_review.md"]["text"] == "# Review\n"
    assert run["run_id"] in status["pending_review_runs"]


def test_tool_manifest_exposes_v2_tool_names_and_schemas():
    manifest = mcp_tools.get_tool_manifest()
    names = {tool["name"] for tool in manifest["tools"]}

    assert names == {
        "get_service_status",
        "list_runs",
        "get_run_detail",
        "get_run_log",
        "get_review_package",
        "scan_now",
        "start_run_for_source",
        "write_selected_clips",
        "render_run",
        "preview_cleanup",
        "delete_clip",
        "cleanup_confirm",
        "delete_local_source",
    }
    delete_clip_schema = next(tool["input_schema"] for tool in manifest["tools"] if tool["name"] == "delete_clip")
    assert delete_clip_schema["required"] == ["run_id", "clip_filename", "reason"]


def test_call_tool_validates_required_arguments_and_dispatches(tmp_path):
    service_dir = tmp_path / "service"
    write_json(service_dir / "service.json", {"status": "stopped", "pid": None})

    missing = mcp_tools.call_tool("get_run_detail", {}, service_dir=service_dir)
    status = mcp_tools.call_tool("get_service_status", {}, service_dir=service_dir)
    unknown = mcp_tools.call_tool("unknown", {}, service_dir=service_dir)

    assert missing["ok"] is False
    assert missing["error_code"] == "invalid_arguments"
    assert status["ok"] is True
    assert unknown["error_code"] == "unknown_tool"


def test_write_selected_clips_validates_and_writes_selection(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)
    write_json(run_dir / "merged_candidates.json", [_candidate()])

    result = mcp_tools.write_selected_clips("run-1", _selection(), service_dir=service_dir)

    assert result["ok"] is True
    assert read_json(run_dir / "selected_clips.json")[0]["clip_id"] == "clip-1"
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "selected_clips_written" in events


def test_write_selected_clips_returns_structured_validation_error(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)
    write_json(run_dir / "merged_candidates.json", [_candidate()])

    result = mcp_tools.write_selected_clips("run-1", [{"clip_id": "unknown"}], service_dir=service_dir)

    assert result["ok"] is False
    assert result["error_code"] == "selection_validation_failed"


def test_render_run_and_preview_cleanup_do_not_delete_files(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    source = input_dir / "source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source")
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir, phase="needs_review", local_source_path=source)
    write_json(run_dir / "run_metadata.json", {"pipeline": {"local_source_path": str(source), "original_source_path": "/nas/source.mp4"}})
    write_json(run_dir / "selected_clips.json", _selection())

    def fake_render(selection_path):
        clips_dir = selection_path.parent / "clips"
        clips_dir.mkdir(parents=True)
        clip = clips_dir / "clip-1.mp4"
        clip.write_bytes(b"clip")
        return [clip]

    monkeypatch.setattr(mcp_tools.service, "render_selected_clips", fake_render)
    settings = Settings(recording_source_default=RecordingSourceDefaultConfig(input_dir=input_dir))

    rendered = mcp_tools.render_run("run-1", settings=settings, service_dir=service_dir)
    preview = mcp_tools.preview_cleanup("run-1", settings=settings, service_dir=service_dir)

    assert rendered["ok"] is True
    assert rendered["rendered_paths"] == [str(run_dir / "clips" / "clip-1.mp4")]
    assert preview["ok"] is True
    assert preview["cleanup"]["confirm"] is False
    assert source.exists()
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "mcp_render_completed" in events
    assert "cleanup_preview_created" in events


def test_scan_now_and_start_run_for_source_use_service_core(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    source = source_dir / "recording.mkv"
    source.write_bytes(b"video")
    stable_time = (datetime.now() - timedelta(minutes=30)).timestamp()
    os.utime(source, (stable_time, stable_time))

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(mcp_tools.service.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    settings = Settings(
        service=ServiceConfig(scan_interval_minutes=30),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            min_age_minutes=10,
            stable_check_seconds=0,
        ),
    )

    scan = mcp_tools.scan_now(settings=settings, service_dir=service_dir)
    duplicate = mcp_tools.start_run_for_source(str(source), settings=settings, service_dir=service_dir)

    assert scan["ok"] is True
    assert scan["started_runs"] == 1
    assert duplicate["ok"] is False
    assert duplicate["error_code"] == "duplicate_run"


def test_start_run_for_source_rejects_unstable_or_out_of_scope_source(tmp_path):
    service_dir = tmp_path / "service"
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    recent = source_dir / "recent.mkv"
    recent.write_bytes(b"video")
    settings = Settings(
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            min_age_minutes=10,
            stable_check_seconds=0,
        ),
    )

    unstable = mcp_tools.start_run_for_source(str(recent), settings=settings, service_dir=service_dir)
    escaped = mcp_tools.start_run_for_source(str(tmp_path / "elsewhere.mkv"), settings=settings, service_dir=service_dir)

    assert unstable["ok"] is False
    assert unstable["error_code"] == "source_not_stable"
    assert escaped["ok"] is False
    assert escaped["error_code"] == "path_rejected"


def test_destructive_tools_create_confirmations_and_never_delete(tmp_path):
    service_dir = tmp_path / "service"
    input_dir = tmp_path / "input"
    source = input_dir / "source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source")
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir, phase="rendered", local_source_path=source)
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True)
    clip = clips_dir / "clip-1.mp4"
    clip.write_bytes(b"clip")
    write_json(run_dir / "run_metadata.json", {"pipeline": {"local_source_path": str(source), "original_source_path": "/nas/source.mp4"}})
    write_json(run_dir / "selected_clips.json", _selection())
    settings = Settings(recording_source_default=RecordingSourceDefaultConfig(input_dir=input_dir))

    delete_clip = mcp_tools.delete_clip("run-1", "clip-1.mp4", reason="not needed", service_dir=service_dir)
    cleanup = mcp_tools.cleanup_confirm("run-1", reason="free space", settings=settings, service_dir=service_dir)
    delete_source = mcp_tools.delete_local_source("run-1", reason="rendered", settings=settings, service_dir=service_dir)

    assert delete_clip["status"] == "confirmation_required"
    assert cleanup["status"] == "confirmation_required"
    assert delete_source["status"] == "confirmation_required"
    assert clip.exists()
    assert source.exists()
    confirmations = read_json(service_dir / "confirmations.json")["confirmations"]
    assert [item["action"] for item in confirmations] == ["delete_clip", "cleanup_confirm", "delete_local_source"]
    assert all(item["status"] == "pending" for item in confirmations)
    assert all(item["created_by"] == "mcp" for item in confirmations)
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert events.count("confirmation_created") == 3


def test_delete_clip_rejects_path_traversal(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir, phase="rendered")

    result = mcp_tools.delete_clip("run-1", "../clip-1.mp4", reason="bad", service_dir=service_dir)

    assert result["ok"] is False
    assert result["error_code"] == "path_rejected"
    assert not (service_dir / "confirmations.json").exists()
