from __future__ import annotations

import os
import threading
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
from live_clipper.project_service import open_project_repository
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
    return [{"clip_id": "clip-1", "source_start": 10.0, "source_end": 20.0, "title": "clip", "remove_ranges": []}]


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


def test_scan_recording_source_includes_old_stable_videos_without_time_window(tmp_path):
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

    assert service.scan_recording_source(config) == [old, stable]


def test_content_identity_streams_sha256_and_reuses_stat_cache(tmp_path, monkeypatch):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"same-video-content")
    copied = tmp_path / "renamed.mkv"
    copied.write_bytes(source.read_bytes())
    service_dir = tmp_path / "service"
    calls = []
    original = service._sha256_file
    monkeypatch.setattr(
        service,
        "_sha256_file",
        lambda path: calls.append(path) or original(path),
    )

    first = service.content_identity(source, service_dir=service_dir)
    cached = service.content_identity(source, service_dir=service_dir)
    renamed = service.content_identity(copied, service_dir=service_dir)

    assert first["content_id"] == renamed["content_id"]
    assert first["bytes"] == len(b"same-video-content")
    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert renamed["cache_hit"] is False
    assert calls == [source, copied]
    cache = read_json(service_dir / "content-hash-cache.json")
    assert cache["version"] == 1
    assert len(cache["entries"]) == 2


def test_content_identity_rejects_file_changed_during_hash(tmp_path, monkeypatch):
    source = tmp_path / "growing.mkv"
    source.write_bytes(b"first")
    original = service._sha256_file

    def hash_then_grow(path):
        digest = original(path)
        path.write_bytes(path.read_bytes() + b"more")
        return digest

    monkeypatch.setattr(service, "_sha256_file", hash_then_grow)

    with pytest.raises(service.SourceChangedDuringHash):
        service.content_identity(source, service_dir=tmp_path / "service")

    assert not (tmp_path / "service" / "content-hash-cache.json").exists()


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


def test_run_service_once_discovers_all_content_and_starts_only_one_pipeline(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    first = source_dir / "first.mkv"
    second = source_dir / "second.mkv"
    first.write_bytes(b"first-video")
    second.write_bytes(b"second-video")
    calls = []
    monkeypatch.setattr(
        service,
        "_start_pipeline_process",
        lambda source_path, *, input_dir, run_dir, log_path: calls.append(source_path) or 4321,
    )
    settings = Settings(
        cheap_model_api_key="test-key",
        paths=PathsConfig(workspace_root=tmp_path / "workspace"),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )

    report = service.run_service_once(settings, service_dir=tmp_path / "service")

    runs = read_json(tmp_path / "service" / "runs.json")["runs"]
    assert report["discovered_runs"] == 2
    assert report["started_runs"] == 1
    assert report["queued_runs"] == 1
    assert [run["phase"] for run in runs] == ["processing", "queued"]
    assert len({run["content_id"] for run in runs}) == 2
    assert all(len(run["content_id"]) == 64 for run in runs)
    assert calls == [first]
    assert not Path(runs[1]["input_dir"]).exists()


def test_scan_message_explains_existing_active_work_when_queue_cannot_start(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {"run_id": "active", "phase": "processing", "run_dir": str(tmp_path / "active")},
                {"run_id": "queued", "phase": "queued", "run_dir": str(tmp_path / "queued")},
            ]
        },
    )
    monkeypatch.setattr(service, "_reconcile_runs", lambda runs, settings, service_dir: (0, []))

    def fake_dispatch(runs, *, settings, service_dir, failures):
        failures.append({"run_id": "queued", "error": "already active"})
        return []

    monkeypatch.setattr(service, "dispatch_queued_runs", fake_dispatch)
    monkeypatch.setattr(
        service,
        "scan_recording_source_report",
        lambda config: {
            "eligible": [],
            "unsupported_files": 0,
            "too_new_files": 0,
            "unstable_files": 0,
            "skipped_subdirectories": 0,
            "file_errors": [],
            "source_unconfigured": False,
        },
    )
    settings = Settings(
        cheap_model_api_key="test-key",
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )

    report = service.run_service_once(settings, service_dir=service_dir)

    assert report["started_runs"] == 0
    assert report["queued_runs"] == 1
    assert "本次发现 0 个，本轮启动 0 个，当前总排队 1 个" in report["message"]
    assert "队列启动失败 1 个" in report["message"]
    assert "已有任务正在处理" not in report["message"]

    monkeypatch.setattr(service, "dispatch_queued_runs", lambda runs, *, settings, service_dir, failures: [])
    recovered_message = service.run_service_once(settings, service_dir=service_dir)["message"]
    assert "已有任务正在处理，新任务会按顺序自动开始" in recovered_message


def test_run_service_once_deduplicates_renamed_copy_by_full_content_id(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    source = source_dir / "original.mkv"
    source.write_bytes(b"same-video")
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: 4321)
    settings = Settings(
        cheap_model_api_key="test-key",
        paths=PathsConfig(workspace_root=tmp_path / "workspace"),
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )
    service_dir = tmp_path / "service"
    service.run_service_once(settings, service_dir=service_dir)
    renamed = source_dir / "renamed.mkv"
    renamed.write_bytes(source.read_bytes())

    report = service.run_service_once(settings, service_dir=service_dir)

    runs = read_json(service_dir / "runs.json")["runs"]
    assert len(runs) == 1
    assert report["discovered_runs"] == 0
    assert report["duplicate_files"] == 2
    assert runs[0]["first_source_path"] == str(source)
    assert runs[0]["last_source_path"] == str(renamed)


def test_run_service_once_serializes_concurrent_scans_for_same_content(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    source = source_dir / "recording.mkv"
    source.write_bytes(b"same-video")
    service_dir = tmp_path / "service"
    settings = Settings(
        cheap_model_api_key="test-key",
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )
    first_identity_started = threading.Event()
    second_identity_started = threading.Event()
    identity_calls_lock = threading.Lock()
    identity_calls = 0
    launch_calls = []

    def fake_content_identity(path, *, service_dir):
        nonlocal identity_calls
        with identity_calls_lock:
            identity_calls += 1
            call_number = identity_calls
        if call_number == 1:
            first_identity_started.set()
            second_identity_started.wait(timeout=1)
        else:
            second_identity_started.set()
        return {"content_id": "a" * 64, "bytes": path.stat().st_size, "cache_hit": False}

    def fake_launch(run, *, settings, service_dir):
        launch_calls.append(run["run_id"])
        run["phase"] = "processing"
        run["pid"] = 4321
        return run

    monkeypatch.setattr(service, "content_identity", fake_content_identity)
    monkeypatch.setattr(service, "_launch_queued_run", fake_launch)
    monkeypatch.setattr(service, "append_event", lambda *args, **kwargs: None)
    reports = []
    errors = []

    def scan():
        try:
            reports.append(service.run_service_once(settings, service_dir=service_dir))
        except Exception as exc:  # pragma: no cover - asserted below for thread failures
            errors.append(exc)

    first = threading.Thread(target=scan)
    second = threading.Thread(target=scan)
    first.start()
    assert first_identity_started.wait(timeout=1)
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], service.ScanBusyError)
    assert len(service.load_runs(service_dir)) == 1
    assert len(launch_calls) == 1
    assert len(reports) == 1
    assert reports[0]["discovered_runs"] == 1


def test_run_service_once_migrates_legacy_run_content_id_without_restarting(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    source = source_dir / "renamed.mkv"
    source.write_bytes(b"legacy-video")
    local_source = tmp_path / "workspace" / "runs" / "legacy" / "input" / "original.mkv"
    local_source.parent.mkdir(parents=True)
    local_source.write_bytes(source.read_bytes())
    service_dir = tmp_path / "service"
    legacy = {
        "run_id": "legacy",
        "source_id": "default",
        "source_path": str(tmp_path / "missing-original.mkv"),
        "local_source_path": str(local_source),
        "input_dir": str(local_source.parent),
        "run_dir": str(local_source.parent.parent / "output"),
        "fingerprint": "old12345",
        "phase": "failed",
        "pid": None,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    write_json(service_dir / "runs.json", {"runs": [legacy]})
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: pytest.fail("must not start"))
    settings = Settings(
        cheap_model_api_key="test-key",
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )

    report = service.run_service_once(settings, service_dir=service_dir)

    runs = read_json(service_dir / "runs.json")["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == "legacy"
    assert len(runs[0]["content_id"]) == 64
    assert report["migrated_runs"] == 1
    assert report["discovered_runs"] == 0


def test_dispatch_queued_runs_starts_next_after_previous_finishes(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    first_source = tmp_path / "first.mkv"
    second_source = tmp_path / "second.mkv"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    runs = [
        {"run_id": "first", "phase": "needs_review", "source_path": str(first_source)},
        {
            "run_id": "second",
            "phase": "queued",
            "source_path": str(second_source),
            "local_source_path": None,
            "input_dir": str(tmp_path / "workspace" / "second" / "input"),
            "run_dir": str(tmp_path / "workspace" / "second" / "output"),
            "log_path": str(service_dir / "runs" / "second.log"),
        },
    ]
    calls = []
    monkeypatch.setattr(
        service,
        "_start_pipeline_process",
        lambda source_path, *, input_dir, run_dir, log_path: calls.append(source_path) or 9876,
    )

    started = service.dispatch_queued_runs(
        runs,
        settings=Settings(cheap_model_api_key="test-key"),
        service_dir=service_dir,
    )

    assert [run["run_id"] for run in started] == ["second"]
    assert runs[1]["phase"] == "processing"
    assert calls == [second_source]


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
    assert second_run["phase"] == "queued"
    assert second_run["local_source_path"] is None

    runs = service.load_runs(service_dir)
    next(run for run in runs if run["run_id"] == first_run["run_id"])["phase"] = "needs_review"
    service.dispatch_queued_runs(runs, settings=settings, service_dir=service_dir)
    second_started = next(run for run in runs if run["run_id"] == second_run["run_id"])
    assert Path(second_started["local_source_path"]).read_bytes() == b"second"
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
    write_json(run_dir / "merged_candidates.json", [_candidate()])
    write_json(run_dir / "selected_clips.json", _selection())
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
    write_json(run_dir / "merged_candidates.json", [_candidate()])
    write_json(run_dir / "selected_clips.json", _selection())
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


def test_empty_legacy_selection_is_recovered_without_blocking_new_scan(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    old_run_dir = tmp_path / "workspace" / "runs" / "old" / "output"
    write_json(old_run_dir / "codex_brief.json", {"candidates": []})
    write_json(old_run_dir / "merged_candidates.json", [])
    write_json(old_run_dir / "selected_clips.json", [])
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {
                    "run_id": "old",
                    "run_dir": str(old_run_dir),
                    "phase": "rendering",
                    "pid": None,
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00",
                }
            ]
        },
    )
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    (source_dir / "new.mkv").write_bytes(b"new-video")
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: 4321)
    monkeypatch.setattr(service, "render_selected_clips", lambda *_args, **_kwargs: pytest.fail("must not render"))
    monkeypatch.setattr(service, "cleanup_local_artifacts", lambda *_args, **_kwargs: pytest.fail("must not clean"))
    settings = Settings(
        cheap_model_api_key="test-key",
        recording_source_default=RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=0,
        ),
    )

    report = service.run_service_once(settings, service_dir=service_dir)

    runs = service.load_runs(service_dir)
    old = next(run for run in runs if run["run_id"] == "old")
    assert report["discovered_runs"] == 1
    assert report["started_runs"] == 1
    assert report["reconcile_failed_runs"] == 0
    assert report["migration_failed_runs"] == 1
    assert report["migration_failures"][0]["run_id"] == "old"
    assert old["phase"] == "needs_review"
    assert old["selection_result"]["status"] == "selection_empty"
    assert (old_run_dir / "selected_clips.json").exists()


def test_scan_stability_check_waits_once_for_multiple_candidates(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    (source_dir / "one.mkv").write_bytes(b"one")
    (source_dir / "two.mkv").write_bytes(b"two")
    waits = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: waits.append(seconds))

    report = service.scan_recording_source_report(
        RecordingSourceDefaultConfig(
            source_dir=source_dir,
            min_age_minutes=0,
            stable_check_seconds=7,
        )
    )

    assert report["eligible"] == [source_dir / "one.mkv", source_dir / "two.mkv"]
    assert waits == [7]


def test_scan_feedback_distinguishes_unconfigured_unsupported_and_subdirectories(tmp_path):
    unconfigured = service.run_service_once(
        Settings(cheap_model_api_key="test-key"),
        service_dir=tmp_path / "unconfigured-service",
    )
    assert unconfigured["source_unconfigured"] is True
    assert "设置 → 录像来源" in unconfigured["message"]

    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    (source_dir / "note.txt").write_text("not video", encoding="utf-8")
    (source_dir / "nested").mkdir()
    classified = service.run_service_once(
        Settings(
            cheap_model_api_key="test-key",
            recording_source_default=RecordingSourceDefaultConfig(
                source_dir=source_dir,
                min_age_minutes=0,
                stable_check_seconds=0,
            ),
        ),
        service_dir=tmp_path / "classified-service",
    )
    assert classified["unsupported_files"] == 1
    assert classified["skipped_subdirectories"] == 1
    assert "不支持格式 1 个" in classified["message"]
    assert "跳过子目录 1 个" in classified["message"]


def test_queue_start_failure_does_not_consume_capacity_for_next_run(tmp_path, monkeypatch):
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    runs = [
        {
            "run_id": name,
            "phase": "queued",
            "source_path": str(path),
            "input_dir": str(tmp_path / name / "input"),
            "run_dir": str(tmp_path / name / "output"),
            "log_path": str(tmp_path / "service" / f"{name}.log"),
            "discovered_at": f"2026-08-17T00:00:0{index}+00:00",
        }
        for index, (name, path) in enumerate((("first", first), ("second", second)))
    ]
    calls = []

    def launch(run, *, settings, service_dir):
        calls.append(run["run_id"])
        if run["run_id"] == "first":
            raise OSError("staging failed")
        run["phase"] = "processing"
        return run

    monkeypatch.setattr(service, "_launch_queued_run", launch)
    failures = []

    started = service.dispatch_queued_runs(
        runs,
        settings=Settings(cheap_model_api_key="test-key"),
        service_dir=tmp_path / "service",
        failures=failures,
    )

    assert calls == ["first", "second"]
    assert [run["run_id"] for run in started] == ["second"]
    assert runs[0]["phase"] == "failed"
    assert failures[0]["run_id"] == "first"


def test_source_file_failure_is_isolated_and_redacted(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    bad = source_dir / "bad.mkv"
    good = source_dir / "good.mkv"
    bad.write_bytes(b"bad")
    good.write_bytes(b"good")
    secret = "secret-test-key"

    def identity(path, *, service_dir):
        if path == bad:
            raise OSError(f"cannot read with {secret}")
        return {"content_id": "a" * 64, "bytes": path.stat().st_size, "cache_hit": False}

    monkeypatch.setattr(service, "content_identity", identity)
    monkeypatch.setattr(service, "_start_pipeline_process", lambda *args, **kwargs: 4321)
    report = service.run_service_once(
        Settings(
            cheap_model_api_key=secret,
            recording_source_default=RecordingSourceDefaultConfig(
                source_dir=source_dir,
                min_age_minutes=0,
                stable_check_seconds=0,
            ),
        ),
        service_dir=tmp_path / "service",
    )

    assert report["discovered_runs"] == 1
    assert report["file_error_count"] == 1
    assert report["file_errors"][0]["name"] == "bad.mkv"
    assert secret not in str(report)
    assert "[REDACTED]" in report["file_errors"][0]["error"]


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


def test_tick_failure_marks_degraded_and_success_recovers_without_losing_history(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    settings = Settings()
    service._write_service_state(
        service_dir,
        {
            "status": "running",
            "pid": 1234,
            "started_at": "2026-08-17T00:00:00+00:00",
            "last_successful_tick_at": "2026-08-17T00:01:00+00:00",
            "config_snapshot": {"source_id": "default"},
        },
    )
    monkeypatch.setattr(service, "pid_is_running", lambda _pid: True)

    service._record_tick_failure(service_dir, 1234, settings, RuntimeError("boom"))
    degraded = service.get_service_status(service_dir=service_dir)["service"]
    assert degraded["status"] == "degraded"
    assert degraded["consecutive_errors"] == 1
    assert degraded["started_at"] == "2026-08-17T00:00:00+00:00"
    assert degraded["last_successful_tick_at"] == "2026-08-17T00:01:00+00:00"
    assert degraded["last_error"] == "boom"
    assert degraded["next_retry_at"]

    service._record_tick_success(service_dir, 1234, settings, {"ok": True})
    recovered = service.get_service_status(service_dir=service_dir)["service"]
    assert recovered["status"] == "running"
    assert recovered["consecutive_errors"] == 0
    assert recovered["last_error"] is None
    assert recovered["last_error_at"] == degraded["last_error_at"]
    assert recovered["last_successful_tick_at"] != "2026-08-17T00:01:00+00:00"


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

    monkeypatch.setattr(service, "scan_recording_source_report", fake_scan)

    report = service.run_service_once(Settings(cheap_model_api_key="test-key"), service_dir=service_dir)

    assert report["ok"] is True
    assert report["scan_error"] == "/Volumes/nas/missing"
    saved = read_json(service_dir / "runs.json")["runs"]
    assert saved[0]["phase"] == "needs_review"
    events = (service_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "recording_source_unavailable" in events


def test_projects_mode_service_tick_uses_sqlite_runtime_without_runs_json(tmp_path, monkeypatch):
    service_dir = tmp_path / "service"
    open_project_repository(service_dir).close()
    monkeypatch.setattr(
        "live_clipper.project_runtime.tick_project_runtime",
        lambda settings, *, service_dir: {"ok": True, "mode": "projects", "started_run_ids": []},
    )
    monkeypatch.setattr(
        "live_clipper.project_scheduler.tick_project_schedules",
        lambda settings, *, service_dir: {"ok": True, "mode": "projects", "scanned_projects": []},
    )

    report = service.run_service_tick(Settings(), service_dir=service_dir)

    assert report["mode"] == "projects"
    assert report["runtime"]["mode"] == "projects"
    assert report["scheduler"]["mode"] == "projects"
    assert not (service_dir / "runs.json").exists()
    with pytest.raises(service.ProjectScopeRequiredError):
        service.run_service_once(Settings(), service_dir=service_dir)
