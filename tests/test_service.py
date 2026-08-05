from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from live_clipper import service
from live_clipper.config import (
    PathsConfig,
    RecordingSourceDefaultConfig,
    SchedulerConfig,
    SchedulerJobConfig,
    ServiceConfig,
    Settings,
)
from live_clipper.utils import read_json, write_json


def test_validate_service_settings_rejects_non_preview_cleanup():
    settings = Settings(service=ServiceConfig(cleanup_mode="delete_after_render"))

    with pytest.raises(ValueError, match="preview_only"):
        service.validate_service_settings(settings)


def test_build_run_identity_uses_source_fingerprint(tmp_path):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"video")

    identity = service.build_run_identity("default", source, output_root=tmp_path / "output")

    assert identity["fingerprint"]
    assert identity["run_id"].startswith("recording__")
    assert identity["run_dir"].parent == tmp_path / "output" / "default"


def test_build_run_identity_uses_per_run_workspace_layout(tmp_path):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"video")

    identity = service.build_run_identity(
        "default",
        source,
        output_root=tmp_path / "legacy-output",
        input_dir=tmp_path / "legacy-input",
        workspace_root=tmp_path / "workspace",
    )

    assert identity["workspace_dir"] == tmp_path / "workspace" / "runs" / identity["run_id"]
    assert identity["input_dir"] == identity["workspace_dir"] / "input"
    assert identity["run_dir"] == identity["workspace_dir"] / "output"


def test_scan_recording_source_filters_recent_stable_videos(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    stable = source_dir / "stable.mkv"
    uploading = source_dir / "uploading.mkv"
    old = source_dir / "old.mkv"
    ignored = source_dir / "note.txt"
    for path in [stable, uploading, old, ignored]:
        path.write_bytes(b"x")

    now = datetime.now()
    stable_time = (now - timedelta(minutes=30)).timestamp()
    uploading_time = (now - timedelta(minutes=2)).timestamp()
    old_time = (now - timedelta(hours=200)).timestamp()
    os.utime(stable, (stable_time, stable_time))
    os.utime(uploading, (uploading_time, uploading_time))
    os.utime(old, (old_time, old_time))

    config = RecordingSourceDefaultConfig(
        source_dir=source_dir,
        since_hours=168,
        min_age_minutes=10,
        stable_check_seconds=0,
    )

    assert service.scan_recording_source(config) == [stable]


def test_run_service_once_stages_and_launches_pipeline(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    source = source_dir / "recording.mkv"
    source.write_bytes(b"video")
    stable_time = (datetime.now() - timedelta(minutes=30)).timestamp()
    os.utime(source, (stable_time, stable_time))
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, stdout, stderr, start_new_session):
        calls.append((command, stdout.name, stderr, start_new_session))
        return FakeProcess()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    settings = Settings(
        cheap_model_api_key="test-key",
        service=ServiceConfig(scan_interval_minutes=30),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            min_age_minutes=10,
            stable_check_seconds=0,
        ),
    )

    report = service.run_service_once(settings, service_dir=tmp_path / "service")

    assert report["started_runs"] == 1
    run = read_json(tmp_path / "service" / "runs.json")["runs"][0]
    assert run["phase"] == "processing"
    assert run["source_path"] == str(source)
    assert run["local_source_path"] == str(tmp_path / "input" / "recording.mkv")
    assert run["run_dir"].startswith(str(tmp_path / "output" / "default"))
    assert calls[0][0][1:4] == ["-m", "live_clipper", "pipeline"]
    assert "--output-dir" in calls[0][0]
    events = (tmp_path / "service" / "events.jsonl").read_text(encoding="utf-8")
    assert "pipeline_started" in events


def test_run_service_once_blocks_before_scan_stage_or_process_when_ai_key_missing(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    (source_dir / "recording.mkv").write_bytes(b"video")
    calls = []
    monkeypatch.setattr(service, "scan_recording_source", lambda config: calls.append("scan") or [])
    monkeypatch.setattr(service, "stage_source_file", lambda *args, **kwargs: calls.append("stage"))
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: calls.append("process"))
    settings = Settings(
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            stable_check_seconds=0,
        ),
    )
    service_dir = tmp_path / "service"

    with pytest.raises(service.PipelineConfigurationError, match="设置 → AI 服务"):
        service.run_service_once(settings, service_dir=service_dir)

    assert calls == []
    assert not (service_dir / "runs.json").exists()
    assert not (tmp_path / "input").exists()
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "pipeline_configuration_blocked" in events
    assert "test-key" not in events


def test_start_run_workspace_stages_into_own_input_and_persists_absolute_paths(tmp_path, monkeypatch):
    source = tmp_path / "nas" / "recording.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    calls = []
    monkeypatch.setattr(
        service,
        "_start_pipeline_process",
        lambda source_path, *, input_dir, run_dir, log_path: calls.append(
            (source_path, input_dir, run_dir, log_path)
        )
        or 4321,
    )
    workspace = tmp_path / "workspace"
    settings = Settings(
        cheap_model_api_key="test-key",
        paths=PathsConfig(workspace_root=workspace),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source.parent,
            input_dir=tmp_path / "legacy-input",
            output_root=tmp_path / "legacy-output",
            stable_check_seconds=0,
        ),
    )

    run = service.start_run_for_source(source, settings=settings, service_dir=tmp_path / "service")

    assert Path(run["workspace_dir"]).is_absolute()
    assert Path(run["input_dir"]).is_absolute()
    assert Path(run["run_dir"]).is_absolute()
    assert Path(run["local_source_path"]).read_bytes() == b"video"
    assert Path(run["local_source_path"]).parent == Path(run["input_dir"])
    assert Path(run["run_dir"]) == Path(run["workspace_dir"]) / "output"
    assert calls[0][1] == Path(run["input_dir"])
    assert calls[0][2] == Path(run["run_dir"])
    saved = read_json(tmp_path / "service" / "runs.json")["runs"][0]
    for field in ("workspace_dir", "input_dir", "run_dir", "local_source_path"):
        assert saved[field] == run[field]


def test_workspace_runs_with_same_filename_keep_independent_inputs(tmp_path, monkeypatch):
    first = tmp_path / "nas-a" / "recording.mkv"
    second = tmp_path / "nas-b" / "recording.mkv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: 4321)
    workspace = tmp_path / "workspace"
    settings = Settings(
        cheap_model_api_key="test-key",
        paths=PathsConfig(workspace_root=workspace),
        recording_source_default=RecordingSourceDefaultConfig(
            input_dir=tmp_path / "legacy-input",
            output_root=tmp_path / "legacy-output",
            stable_check_seconds=0,
        ),
    )
    service_dir = tmp_path / "service"

    first_run = service.start_run_for_source(first, settings=settings, service_dir=service_dir)
    second_run = service.start_run_for_source(second, settings=settings, service_dir=service_dir)

    assert first_run["run_id"] != second_run["run_id"]
    assert Path(first_run["input_dir"]) != Path(second_run["input_dir"])
    assert Path(first_run["local_source_path"]).read_bytes() == b"first"
    assert Path(second_run["local_source_path"]).read_bytes() == b"second"
    assert not list(workspace.rglob("*.part"))


def test_retry_failed_run_reuses_local_input_when_original_source_is_unavailable(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    workspace_dir = tmp_path / "workspace" / "runs" / "recording__abc123"
    input_dir = workspace_dir / "input"
    run_dir = workspace_dir / "output"
    local_source = input_dir / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"video")
    original_source = tmp_path / "missing-nas" / "recording.mkv"
    run = {
        "run_id": "recording__abc123",
        "source_id": "default",
        "source_path": str(original_source),
        "local_source_path": str(local_source),
        "workspace_dir": str(workspace_dir),
        "input_dir": str(input_dir),
        "run_dir": str(run_dir),
        "fingerprint": "abc123",
        "phase": "failed",
        "pid": None,
        "log_path": str(service_dir / "runs" / "recording__abc123.log"),
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "last_error": "Pipeline stopped before codex_brief.json was created",
    }
    write_json(service_dir / "runs.json", {"runs": [run]})
    calls = []
    monkeypatch.setattr(
        service,
        "_start_pipeline_process",
        lambda source_path, *, input_dir, run_dir, log_path: calls.append(
            (source_path, input_dir, run_dir, log_path)
        ) or 9876,
    )

    retried = service.retry_failed_run(
        "recording__abc123",
        settings=Settings(cheap_model_api_key="test-key"),
        service_dir=service_dir,
    )

    assert calls == [(local_source, input_dir, run_dir, Path(run["log_path"]))]
    assert retried["run_id"] == run["run_id"]
    assert retried["fingerprint"] == run["fingerprint"]
    assert retried["phase"] == "processing"
    assert retried["pid"] == 9876
    assert retried["last_error"] is None
    assert retried["retry_count"] == 1


def test_retry_failed_run_missing_ai_key_preserves_failed_run(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    run = {
        "run_id": "run-failed",
        "source_path": str(tmp_path / "source.mkv"),
        "local_source_path": str(tmp_path / "input" / "source.mkv"),
        "input_dir": str(tmp_path / "input"),
        "run_dir": str(tmp_path / "output"),
        "phase": "failed",
        "pid": None,
        "last_error": "old error",
        "updated_at": "2026-08-05T00:00:00+00:00",
    }
    write_json(service_dir / "runs.json", {"runs": [run]})
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: pytest.fail("must not start"))

    with pytest.raises(service.PipelineConfigurationError, match="设置 → AI 服务"):
        service.retry_failed_run("run-failed", settings=Settings(), service_dir=service_dir)

    assert read_json(service_dir / "runs.json")["runs"] == [run]


def test_reconcile_marks_needs_review_when_brief_exists(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__abc123"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    monkeypatch.setattr(service, "pid_is_running", lambda pid: False)
    run = {
        "run_id": "recording__abc123",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "updated_at": "now",
    }
    settings = Settings()

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is True
    assert run["phase"] == "needs_review"
    assert run["pid"] is None


def test_reconcile_auto_renders_and_cleanup_preview_only(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__abc123"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(run_dir / "selected_clips.json", [{"clip_id": "clip-1"}])
    calls = []

    def fake_render(selection_path):
        calls.append(("render", selection_path))
        clips_dir = run_dir / "clips"
        clips_dir.mkdir(parents=True)
        clip = clips_dir / "clip-1.mp4"
        clip.write_bytes(b"clip")
        return [clip]

    def fake_cleanup(run_path, *, input_dir, confirm=False, force=False):
        calls.append(("cleanup", run_path, input_dir, confirm, force))
        return {"deleted": [], "targets": []}

    monkeypatch.setattr(service, "render_selected_clips", fake_render)
    monkeypatch.setattr(service, "cleanup_local_artifacts", fake_cleanup)
    run = {
        "run_id": "recording__abc123",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "needs_review",
        "pid": None,
        "updated_at": "now",
    }
    settings = Settings(
        service=ServiceConfig(auto_render_after_selection=True),
        recording_source_default=RecordingSourceDefaultConfig(input_dir=tmp_path / "input"),
    )

    service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert run["phase"] == "rendered"
    assert calls == [
        ("render", run_dir / "selected_clips.json"),
        ("cleanup", run_dir, tmp_path / "input", False, False),
    ]


def test_reconcile_auto_cleanup_uses_saved_run_input(tmp_path, monkeypatch):
    run_dir = tmp_path / "workspace" / "runs" / "recording__abc123" / "output"
    run_input = run_dir.parent / "input"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(run_dir / "selected_clips.json", [{"clip_id": "clip-1"}])
    calls = []

    def fake_render(selection_path):
        clips_dir = run_dir / "clips"
        clips_dir.mkdir(parents=True)
        clip = clips_dir / "clip-1.mp4"
        clip.write_bytes(b"clip")
        return [clip]

    def fake_cleanup(run_path, *, input_dir, confirm=False, force=False):
        calls.append((run_path, input_dir, confirm, force))
        return {"deleted": [], "targets": []}

    monkeypatch.setattr(service, "render_selected_clips", fake_render)
    monkeypatch.setattr(service, "cleanup_local_artifacts", fake_cleanup)
    run = {
        "run_id": "recording__abc123",
        "source_id": "default",
        "run_dir": str(run_dir),
        "input_dir": str(run_input),
        "phase": "needs_review",
        "pid": None,
        "updated_at": "now",
    }
    settings = Settings(
        service=ServiceConfig(auto_render_after_selection=True),
        recording_source_default=RecordingSourceDefaultConfig(input_dir=tmp_path / "legacy-input"),
    )

    service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert calls == [(run_dir, run_input, False, False)]


def test_start_service_background_writes_pid_and_refuses_duplicate(tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 9876

    def fake_popen(command, stdout, stderr, start_new_session):
        calls.append((command, stdout.name, stderr, start_new_session))
        return FakeProcess()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "pid_is_running", lambda pid: pid == 9876)
    settings = Settings()

    first = service.start_service(settings, service_dir=tmp_path / "service")
    second = service.start_service(settings, service_dir=tmp_path / "service")

    assert first["started"] is True
    assert first["pid"] == 9876
    assert second["started"] is False
    assert second["reason"] == "service_already_running"
    assert read_json(tmp_path / "service" / "service.json")["pid"] == 9876
    assert (tmp_path / "service" / "service.pid").read_text(encoding="utf-8") == "9876\n"
    assert calls[0][0][1:5] == ["-m", "live_clipper", "service", "start"]
    assert "--foreground" in calls[0][0]


def test_start_service_once_runs_single_iteration(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "run_service_once", lambda settings, service_dir: calls.append((settings, service_dir)) or {"ok": True, "started_runs": 0})

    report = service.start_service(Settings(), service_dir=tmp_path / "service", once=True)

    assert report["started"] is True
    assert report["once"] is True
    assert calls[0][1] == tmp_path / "service"
    state = read_json(tmp_path / "service" / "service.json")
    assert state["status"] == "stopped"


def test_run_service_tick_reconciles_runs_and_ticks_scheduler(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "recording__abc123"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(service_dir / "runs.json", {
        "runs": [
            {
                "run_id": "recording__abc123",
                "run_dir": str(run_dir),
                "phase": "processing",
                "pid": None,
            }
        ]
    })
    job = SchedulerJobConfig(
        id="maintenance",
        name="维护",
        enabled=True,
        type="maintenance_check",
        schedule="interval_minutes",
        interval_minutes=30,
    )
    settings = Settings(scheduler=SchedulerConfig(jobs=[job], tick_seconds=5))
    monkeypatch.setattr(service, "pid_is_running", lambda pid: False)

    report = service.run_service_tick(settings, service_dir=service_dir)

    assert report["ok"] is True
    assert report["scheduler"]["ok"] is True
    assert read_json(service_dir / "runs.json")["runs"][0]["phase"] == "needs_review"
    assert (service_dir / "scheduler.json").exists()
    assert "scheduler_tick" in (service_dir / "events.jsonl").read_text(encoding="utf-8")


def test_stop_service_marks_stopped_without_killing_pipeline_children(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "service.pid").write_text("1234\n", encoding="utf-8")
    killed = []
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    report = service.stop_service(service_dir=service_dir)

    assert report["stopped"] is True
    assert killed == [(1234, service.signal.SIGTERM)]
    assert read_json(service_dir / "service.json")["status"] == "stopped"


def test_get_service_status_summarizes_runs(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    write_json(service_dir / "service.json", {"status": "running", "pid": 1234})
    (service_dir / "service.pid").write_text("1234\n", encoding="utf-8")
    write_json(service_dir / "runs.json", {
        "runs": [
            {"run_id": "a", "phase": "needs_review"},
            {"run_id": "b", "phase": "rendered"},
            {"run_id": "c", "phase": "failed"},
        ]
    })
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)

    status = service.get_service_status(service_dir=service_dir)

    assert status["running"] is True
    assert status["phase_counts"] == {"failed": 1, "needs_review": 1, "rendered": 1}
    assert status["pending_review_runs"] == ["a"]


def test_read_service_logs_returns_tail(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "service.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert service.read_service_logs(service_dir=service_dir, max_lines=2) == "two\nthree"


def test_pid_is_running_reaps_exited_child(monkeypatch):
    def fake_waitpid(pid, flag):
        assert pid == 4321
        assert flag == os.WNOHANG
        return (4321, 0)  # 子进程已退出并被回收

    monkeypatch.setattr(service.os, "waitpid", fake_waitpid)

    assert service.pid_is_running(4321) is False


def test_pid_is_running_reports_live_child(monkeypatch):
    monkeypatch.setattr(service.os, "waitpid", lambda pid, flag: (0, 0))  # 仍在运行

    assert service.pid_is_running(4321) is True


def test_pid_is_running_falls_back_for_non_child(monkeypatch):
    def fake_waitpid(pid, flag):
        raise ChildProcessError

    kills = []

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(service.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(service.os, "kill", fake_kill)

    assert service.pid_is_running(4321) is False
    assert kills == [(4321, 0)]


def test_reconcile_recovers_stuck_run_when_output_ready(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__stuck01"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    # pid 仍被误判为存活（模拟僵尸进程），但产物已生成
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__stuck01",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",  # 远超阈值
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=180))

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is True
    assert run["phase"] == "needs_review"
    assert run["pid"] is None
    events = (tmp_path / "service" / "events.jsonl").read_text(encoding="utf-8")
    assert "stuck_run_recovered" in events


def test_reconcile_keeps_processing_when_running_and_not_stuck(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__live01"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__live01",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": service.now_utc(),
        "updated_at": service.now_utc(),  # 刚刚更新，未超时
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=180))

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is False
    assert run["phase"] == "processing"
    assert run["pid"] == 4321


def test_reconcile_disabled_stuck_guard_when_threshold_zero(tmp_path, monkeypatch):
    run_dir = tmp_path / "output" / "default" / "recording__live02"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    monkeypatch.setattr(service, "pid_is_running", lambda pid: True)
    run = {
        "run_id": "recording__live02",
        "source_id": "default",
        "run_dir": str(run_dir),
        "phase": "processing",
        "pid": 4321,
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    settings = Settings(service=ServiceConfig(stuck_after_minutes=0))  # 关闭兜底

    changed = service.reconcile_run(run, settings, service_dir=tmp_path / "service")

    assert changed is False
    assert run["phase"] == "processing"


def test_run_service_once_persists_reconcile_when_scan_source_missing(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "recording__abc123"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(service_dir / "runs.json", {
        "runs": [
            {
                "run_id": "recording__abc123",
                "run_dir": str(run_dir),
                "phase": "processing",
                "pid": None,
            }
        ]
    })

    def fake_scan(config):
        raise FileNotFoundError("/Volumes/nas/missing")

    monkeypatch.setattr(service, "scan_recording_source", fake_scan)

    report = service.run_service_once(Settings(cheap_model_api_key="test-key"), service_dir=service_dir)

    assert report["ok"] is True
    assert report["scan_error"] == "/Volumes/nas/missing"
    saved = read_json(service_dir / "runs.json")["runs"]
    assert saved[0]["phase"] == "needs_review"
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "recording_source_unavailable" in events
