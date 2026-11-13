from __future__ import annotations

from pathlib import Path

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


def test_removed_scheduler_mutations_leave_config_unchanged(tmp_path):
    paths = _paths(tmp_path)
    paths.config_path.write_text("[service]\nenabled = false\n")
    before = paths.config_path.read_bytes()
    for suffix in ("", "/daily_review/run-now", "/daily_review/pause", "/daily_review/resume"):
        status, _, payload = handle_api_request("POST", "/api/scheduler/jobs" + suffix,
            paths, body={"job": {"id": "daily_review"}})
        assert status == 404
        assert payload["error_code"] == "route_not_found"
    assert paths.config_path.read_bytes() == before
