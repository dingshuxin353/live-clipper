from __future__ import annotations

import threading
import time

from live_clipper import jobs


def _wait_terminal(service_dir, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.read_job(service_dir, job_id)
        if job and job["status"] in jobs.TERMINAL_STATUSES:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal status in time")


def test_start_job_runs_and_succeeds(tmp_path):
    service_dir = tmp_path / "service"
    job = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=lambda: {"ok": True, "selected_count": 3})
    final = _wait_terminal(service_dir, job["id"])
    assert final["status"] == "succeeded"
    assert final["result"]["selected_count"] == 3
    assert final["error"] is None


def test_start_job_records_failure(tmp_path):
    service_dir = tmp_path / "service"
    job = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=lambda: {"ok": False, "message": "boom"})
    final = _wait_terminal(service_dir, job["id"])
    assert final["status"] == "failed"
    assert final["error"] == "boom"


def test_start_job_records_exception(tmp_path):
    service_dir = tmp_path / "service"

    def boom():
        raise RuntimeError("kaboom")

    job = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=boom)
    final = _wait_terminal(service_dir, job["id"])
    assert final["status"] == "failed"
    assert "kaboom" in final["error"]


def test_start_job_dedups_running_jobs(tmp_path):
    service_dir = tmp_path / "service"
    release = threading.Event()

    def blocker():
        release.wait(timeout=5)
        return {"ok": True}

    first = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=blocker)
    second = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=blocker)
    assert first["id"] == second["id"]
    assert jobs.active_job_for(service_dir, "r1", "ai_review")["id"] == first["id"]
    release.set()
    _wait_terminal(service_dir, first["id"])


def test_sweep_interrupted_marks_running(tmp_path):
    service_dir = tmp_path / "service"
    release = threading.Event()
    job = jobs.start_job(service_dir, kind="ai_review", run_id="r1", fn=lambda: release.wait(timeout=5) or {"ok": True})
    jobs.sweep_interrupted(service_dir)
    swept = jobs.read_job(service_dir, job["id"])
    assert swept["status"] == "interrupted"
    release.set()
