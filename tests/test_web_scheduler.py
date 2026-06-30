from __future__ import annotations

from pathlib import Path

from live_clipper.config_editor import load_editable_config
from live_clipper.web import WebPaths, handle_api_request


def _paths(tmp_path: Path) -> WebPaths:
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "work" / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def _job(job_id: str = "daily_review") -> dict:
    return {
        "id": job_id,
        "name": "每日审阅检查",
        "enabled": True,
        "type": "review_due_check",
        "schedule": "daily",
        "time": "12:00",
        "skip_if_running": True,
    }


def test_get_api_scheduler_returns_default_jobs(tmp_path):
    status, _headers, payload = handle_api_request("GET", "/api/scheduler", _paths(tmp_path))

    assert status == 200
    assert payload["ok"] is True
    assert payload["scheduler"]["timezone"] == "Asia/Shanghai"
    assert [job["id"] for job in payload["jobs"]] == ["weekly_recording_scan", "weekly_review_due"]


def test_post_api_scheduler_jobs_writes_config_and_returns_status(tmp_path):
    paths = _paths(tmp_path)

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/scheduler/jobs",
        paths,
        body={"job": _job()},
    )

    assert status == 200
    assert payload["ok"] is True
    config = load_editable_config(config_path=paths.config_path)["config"]
    assert any(job["id"] == "daily_review" for job in config["scheduler_jobs"])
    assert "daily_review" in [job["id"] for job in payload["jobs"]]


def test_post_api_scheduler_jobs_rejects_invalid_job_with_chinese_error(tmp_path):
    invalid = _job("Bad Job!")
    invalid["type"] = "delete_all"

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/scheduler/jobs",
        _paths(tmp_path),
        body={"job": invalid},
    )

    assert status == 400
    assert payload["ok"] is False
    messages = "\n".join(error["message"] for error in payload["errors"])
    assert "任务 id 只能使用小写字母" in messages
    assert "任务类型只能是 scan_recordings、review_due_check、maintenance_check 或 ai_review" in messages


def test_post_api_scheduler_run_now_pause_and_resume(tmp_path):
    paths = _paths(tmp_path)
    handle_api_request("POST", "/api/scheduler/jobs", paths, body={"job": _job()})

    run_status, _headers, run_payload = handle_api_request(
        "POST",
        "/api/scheduler/jobs/daily_review/run-now",
        paths,
    )
    pause_status, _headers, pause_payload = handle_api_request(
        "POST",
        "/api/scheduler/jobs/daily_review/pause",
        paths,
    )
    resume_status, _headers, resume_payload = handle_api_request(
        "POST",
        "/api/scheduler/jobs/daily_review/resume",
        paths,
    )
    events_status, _headers, events_payload = handle_api_request("GET", "/api/scheduler/events", paths)

    assert run_status == 200
    assert run_payload["ok"] is True
    assert run_payload["result"]["message"] == "当前没有待审阅任务"
    assert pause_status == 200
    assert pause_payload["paused"] is True
    assert resume_status == 200
    assert resume_payload["paused"] is False
    assert events_status == 200
    assert any(event["type"] == "scheduler_job_completed" for event in events_payload["events"])
