from datetime import UTC, datetime

from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_scheduler import next_project_scan_at, tick_due_projects
from live_clipper.project_service import ProjectManager, open_project_repository


def test_next_scan_and_missed_schedule_run_once(tmp_path):
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    config = default_project_config(tmp_path / "source", tmp_path / "output")
    config["schedule"].update({"enabled": True, "mode": "interval", "daily_time": None, "interval_minutes": 60})
    assert next_project_scan_at(config, now=now) == "2026-08-20T11:00:00Z"
    (tmp_path / "source").mkdir()
    (tmp_path / "output").mkdir()
    repo = open_project_repository(tmp_path / "service")
    project = ProjectManager(repo, Settings(cheap_model_api_key="fake")).create_project(
        name="P", config=config, activation_state="active"
    )
    repo.update_runtime(project.project_id, next_scan_at="2026-08-20T08:00:00Z")
    calls = []
    first = tick_due_projects(repo, now=now, scan_fn=lambda project_id, **kwargs: calls.append((project_id, kwargs)))
    second = tick_due_projects(repo, now=now, scan_fn=lambda project_id, **kwargs: calls.append((project_id, kwargs)))
    assert first == [project.project_id]
    assert second == []
    assert len(calls) == 1 and calls[0][1]["recovery_scan"] is True
