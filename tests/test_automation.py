from __future__ import annotations

from datetime import datetime, timedelta
import os

from live_clipper import automation
from live_clipper.automation import check_automation_runs, find_latest_recording, start_latest_recording_job
from live_clipper.utils import read_json, write_json


def test_find_latest_recording_uses_recent_stable_video(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    old = source_dir / "old.mkv"
    latest = source_dir / "latest.mkv"
    uploading = source_dir / "uploading.mkv"
    ignored = source_dir / "note.txt"
    for path in [old, latest, uploading, ignored]:
        path.write_bytes(b"x")

    now = datetime.now()
    old_time = (now - timedelta(hours=50)).timestamp()
    latest_time = (now - timedelta(minutes=30)).timestamp()
    uploading_time = (now - timedelta(minutes=2)).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(latest, (latest_time, latest_time))
    os.utime(uploading, (uploading_time, uploading_time))

    result = find_latest_recording(source_dir, since_hours=36, min_age_minutes=10)

    assert result == latest


def test_start_latest_recording_job_launches_background_pipeline(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    input_dir = tmp_path / "input"
    output_root = tmp_path / "output"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    source_dir.mkdir()
    source = source_dir / "recording.mkv"
    source.write_bytes(b"video")
    stable_time = (datetime.now() - timedelta(minutes=30)).timestamp()
    os.utime(source, (stable_time, stable_time))
    popen_calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, stdout, stderr, start_new_session):
        popen_calls.append((command, stdout.name, stderr, start_new_session))
        return FakeProcess()

    monkeypatch.setattr(automation.subprocess, "Popen", fake_popen)

    report = start_latest_recording_job(
        source_dir,
        input_dir=input_dir,
        output_root=output_root,
        state_dir=state_dir,
        log_dir=log_dir,
    )

    assert report["started"] is True
    assert report["pid"] == 4321
    command = popen_calls[0][0]
    assert command[1:4] == ["-m", "live_clipper", "pipeline"]
    assert str(source) in command
    assert "--refine" in command
    state = read_json(state_dir / "recording.json")
    assert state["phase"] == "running"
    assert state["requires_codex"] is False
    assert state["run_dir"] == str(output_root / "recording")


def test_start_latest_recording_job_skips_running_existing_job(tmp_path, monkeypatch):
    source_dir = tmp_path / "nas"
    state_dir = tmp_path / "state"
    source_dir.mkdir()
    source = source_dir / "recording.mkv"
    source.write_bytes(b"video")
    stable_time = (datetime.now() - timedelta(minutes=30)).timestamp()
    os.utime(source, (stable_time, stable_time))
    write_json(state_dir / "recording.json", {"pid": 4321, "log_path": str(tmp_path / "log")})
    monkeypatch.setattr(automation, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(automation.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not start")))

    report = start_latest_recording_job(source_dir, state_dir=state_dir)

    assert report["started"] is False
    assert report["reason"] == "已有后台任务正在运行"


def test_check_automation_runs_marks_selection_task(tmp_path):
    output_root = tmp_path / "output"
    run_dir = output_root / "recording"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", [])
    write_json(run_dir / "codex_brief.json", {"candidates": []})

    report = check_automation_runs(output_root, state_dir=tmp_path / "state")

    assert report["requires_codex"] is True
    assert report["codex_tasks"][0]["phase"] == "needs_codex_selection"
    task_path = run_dir / "codex_task.md"
    assert task_path.exists()
    assert "审阅直播切片候选" in task_path.read_text(encoding="utf-8")


def test_check_automation_runs_marks_failed_stopped_job(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    state_dir = tmp_path / "state"
    log_path = tmp_path / "logs" / "recording.log"
    run_dir = output_root / "recording"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(state_dir / "recording.json", {"pid": 4321, "log_path": str(log_path)})
    log_path.parent.mkdir(parents=True)
    log_path.write_text("line1\nAgnes SSL error\n", encoding="utf-8")
    monkeypatch.setattr(automation, "_pid_is_running", lambda pid: False)

    report = check_automation_runs(output_root, state_dir=state_dir)

    assert report["requires_codex"] is True
    task = report["codex_tasks"][0]
    assert task["phase"] == "failed_needs_codex"
    assert "Agnes SSL error" in task["log_tail"]
    assert "诊断流水线失败" in (run_dir / "codex_task.md").read_text(encoding="utf-8")
