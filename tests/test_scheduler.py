from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from live_clipper import review_automation, service
from live_clipper.config import SchedulerConfig, SchedulerJobConfig, Settings
from live_clipper.scheduler import (
    get_scheduler_status,
    next_run_at,
    pause_job,
    read_scheduler_events,
    resume_job,
    run_job_now,
    tick_scheduler,
    validate_scheduler_job,
)
from live_clipper.utils import read_json, write_json

TZ = ZoneInfo("Asia/Shanghai")


def _settings(jobs: list[SchedulerJobConfig] | None = None) -> Settings:
    return Settings(scheduler=SchedulerConfig(jobs=jobs or []))


def test_next_run_calculates_weekly_daily_and_interval_times():
    weekly = SchedulerJobConfig(
        id="weekly_recording_scan",
        name="每周录播扫描",
        enabled=True,
        type="scan_recordings",
        schedule="weekly",
        day_of_week="sun",
        time="00:00",
    )
    daily = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:30",
    )
    interval = SchedulerJobConfig(
        id="interval_maintenance",
        name="维护",
        enabled=True,
        type="maintenance_check",
        schedule="interval_minutes",
        interval_minutes=60,
    )
    now = datetime(2026, 6, 30, 18, 0, tzinfo=TZ)

    assert next_run_at(weekly, now=now, timezone="Asia/Shanghai").isoformat() == "2026-07-05T00:00:00+08:00"
    assert next_run_at(daily, now=now, timezone="Asia/Shanghai").isoformat() == "2026-07-01T08:30:00+08:00"
    assert next_run_at(
        interval,
        now=now,
        timezone="Asia/Shanghai",
        last_run_at=datetime(2026, 6, 30, 17, 30, tzinfo=TZ),
    ).isoformat() == "2026-06-30T18:30:00+08:00"


def test_tick_scheduler_run_once_executes_recent_missed_job(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
    )
    calls = []
    monkeypatch.setattr(service, "run_service_once", lambda settings, service_dir: calls.append(service_dir) or {"ok": True})
    write_json(
        tmp_path / "scheduler_runs.json",
        {"jobs": {"daily_scan": {"last_run_at": "2026-06-29T08:00:00+08:00", "next_run_at": "2026-06-30T08:00:00+08:00"}}},
    )

    result = tick_scheduler(
        _settings([job]),
        service_dir=tmp_path,
        now=datetime(2026, 6, 30, 9, 0, tzinfo=TZ),
    )

    assert result["ran_jobs"] == ["daily_scan"]
    assert calls == [tmp_path]
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]["status"] == "success"
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]["next_run_at"] == "2026-07-01T08:00:00+08:00"
    second = tick_scheduler(
        _settings([job]),
        service_dir=tmp_path,
        now=datetime(2026, 6, 30, 9, 1, tzinfo=TZ),
    )
    assert second["ran_jobs"] == []
    assert calls == [tmp_path]
    assert any(event["type"] == "scheduler_job_missed_run_once" for event in read_scheduler_events(tmp_path))


def test_tick_scheduler_skip_if_running_skips_job(tmp_path):
    job = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
        skip_if_running=True,
    )
    write_json(
        tmp_path / "scheduler_runs.json",
        {"jobs": {"daily_scan": {"status": "running", "next_run_at": "2026-06-30T08:00:00+08:00"}}},
    )

    result = tick_scheduler(
        _settings([job]),
        service_dir=tmp_path,
        now=datetime(2026, 6, 30, 9, 0, tzinfo=TZ),
    )

    assert result["skipped_jobs"] == ["daily_scan"]
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]["status"] == "skipped"


def test_validate_scheduler_job_accepts_ai_review_in_v6():
    result = validate_scheduler_job({
        "id": "ai_review",
        "name": "AI 审阅",
        "enabled": True,
        "type": "ai_review",
        "schedule": "weekly",
        "day_of_week": "sun",
        "time": "12:00",
        "skip_if_running": True,
    })

    assert result["ok"] is True
    assert result["errors"] == []


def test_run_ai_review_job_delegates_to_review_automation(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="weekly_ai_review",
        name="每周 AI 审阅",
        enabled=True,
        type="ai_review",
        schedule="weekly",
        day_of_week="sun",
        time="12:00",
    )

    def fake_run_due(settings, service_dir):
        assert service_dir == tmp_path
        return {"ok": True, "processed_runs": ["run-1"]}

    monkeypatch.setattr(review_automation, "run_due_ai_reviews", fake_run_due)

    result = run_job_now(job, _settings([job]), service_dir=tmp_path)

    assert result["ok"] is True
    assert result["result"]["processed_runs"] == ["run-1"]


def test_run_scan_recordings_job_calls_existing_service_action(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="scan_now",
        name="扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
    )
    monkeypatch.setattr(service, "run_service_once", lambda settings, service_dir: {"ok": True, "started_runs": 2})

    result = run_job_now(job, _settings([job]), service_dir=tmp_path)

    assert result["ok"] is True
    assert result["result"]["started_runs"] == 2
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["scan_now"]["status"] == "success"


def test_review_due_check_marks_needs_review_without_selected_clips(tmp_path):
    run_dir = tmp_path / "output" / "default" / "run-1"
    run_dir.mkdir(parents=True)
    write_json(run_dir / "codex_brief.json", {"run_id": "run-1"})
    write_json(
        tmp_path / "runs.json",
        {
            "runs": [
                {
                    "run_id": "run-1",
                    "phase": "needs_review",
                    "run_dir": str(run_dir),
                    "updated_at": "2026-06-30T00:00:00+08:00",
                }
            ]
        },
    )
    job = SchedulerJobConfig(
        id="weekly_review_due",
        name="每周审阅检查",
        enabled=True,
        type="review_due_check",
        schedule="weekly",
        day_of_week="sun",
        time="12:00",
    )

    result = run_job_now(job, _settings([job]), service_dir=tmp_path)

    assert result["ok"] is True
    assert result["result"]["due_runs"] == ["run-1"]
    assert read_json(tmp_path / "runs.json")["runs"][0]["review_due_at"]
    assert (run_dir / "codex_task.md").exists()
    assert not (run_dir / "selected_clips.json").exists()


def test_get_scheduler_status_lists_jobs_and_next_due(tmp_path):
    job = SchedulerJobConfig(
        id="weekly_review_due",
        name="每周审阅检查",
        enabled=True,
        type="review_due_check",
        schedule="weekly",
        day_of_week="sun",
        time="12:00",
    )

    status = get_scheduler_status(
        _settings([job]),
        service_dir=tmp_path,
        now=datetime(2026, 6, 30, 9, 0, tzinfo=TZ),
    )

    assert status["ok"] is True
    assert status["scheduler"]["timezone"] == "Asia/Shanghai"
    assert status["jobs"][0]["id"] == "weekly_review_due"
    assert status["jobs"][0]["next_run_at"] == "2026-07-05T12:00:00+08:00"
    assert status["next_due_job_id"] == "weekly_review_due"


def test_pause_and_resume_job_persist_state(tmp_path):
    job_id = "weekly_review_due"

    paused = pause_job(job_id, service_dir=tmp_path)
    resumed = resume_job(job_id, service_dir=tmp_path)

    assert paused["ok"] is True
    assert resumed["ok"] is True
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"][job_id]["paused"] is False


def test_weekly_persisted_overdue_job_runs_once_and_advances(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="weekly_scan",
        name="每周扫描",
        enabled=True,
        type="scan_recordings",
        schedule="weekly",
        day_of_week="sun",
        time="08:00",
    )
    write_json(
        tmp_path / "scheduler_runs.json",
        {"jobs": {"weekly_scan": {"last_run_at": "2026-06-21T08:00:00+08:00", "next_run_at": "2026-06-28T08:00:00+08:00"}}},
    )
    calls = []
    monkeypatch.setattr(service, "run_service_once", lambda settings, service_dir: calls.append(service_dir) or {"ok": True})

    first = tick_scheduler(_settings([job]), service_dir=tmp_path, now=datetime(2026, 6, 30, 9, 0, tzinfo=TZ))
    second = tick_scheduler(_settings([job]), service_dir=tmp_path, now=datetime(2026, 6, 30, 9, 1, tzinfo=TZ))

    state = read_json(tmp_path / "scheduler_runs.json")["jobs"]["weekly_scan"]
    assert first["ran_jobs"] == ["weekly_scan"]
    assert second["ran_jobs"] == []
    assert calls == [tmp_path]
    assert state["next_run_at"] == "2026-07-05T08:00:00+08:00"


def test_missed_policy_skip_advances_without_running(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
    )
    settings = Settings(scheduler=SchedulerConfig(jobs=[job], missed_policy="skip"))
    write_json(
        tmp_path / "scheduler_runs.json",
        {"jobs": {"daily_scan": {"next_run_at": "2026-06-29T08:00:00+08:00"}}},
    )
    monkeypatch.setattr(service, "run_service_once", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")))

    result = tick_scheduler(settings, service_dir=tmp_path, now=datetime(2026, 6, 30, 9, 0, tzinfo=TZ))

    state = read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]
    assert result["ran_jobs"] == []
    assert result["skipped_jobs"] == ["daily_scan"]
    assert state["status"] == "skipped"
    assert state["next_run_at"] == "2026-07-01T08:00:00+08:00"


def test_manual_run_preserves_persisted_schedule(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
    )
    write_json(tmp_path / "scheduler_runs.json", {"jobs": {"daily_scan": {"next_run_at": "2026-07-01T08:00:00+08:00"}}})
    monkeypatch.setattr(service, "run_service_once", lambda *_args, **_kwargs: {"ok": True})

    result = run_job_now(job, _settings([job]), service_dir=tmp_path, now=datetime(2026, 6, 30, 12, 0, tzinfo=TZ))

    assert result["ok"] is True
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]["next_run_at"] == "2026-07-01T08:00:00+08:00"


def test_manual_run_consumes_overdue_schedule_without_duplicate_tick(monkeypatch, tmp_path):
    job = SchedulerJobConfig(
        id="daily_scan",
        name="每日扫描",
        enabled=True,
        type="scan_recordings",
        schedule="daily",
        time="08:00",
    )
    write_json(tmp_path / "scheduler_runs.json", {"jobs": {"daily_scan": {"next_run_at": "2026-06-30T08:00:00+08:00"}}})
    calls = []
    monkeypatch.setattr(service, "run_service_once", lambda *_args, **_kwargs: calls.append("run") or {"ok": True})

    manual = run_job_now(job, _settings([job]), service_dir=tmp_path, now=datetime(2026, 6, 30, 12, 0, tzinfo=TZ))
    tick = tick_scheduler(_settings([job]), service_dir=tmp_path, now=datetime(2026, 6, 30, 12, 1, tzinfo=TZ))

    assert manual["ok"] is True
    assert tick["ran_jobs"] == []
    assert calls == ["run"]
    assert read_json(tmp_path / "scheduler_runs.json")["jobs"]["daily_scan"]["next_run_at"] == "2026-07-01T08:00:00+08:00"
