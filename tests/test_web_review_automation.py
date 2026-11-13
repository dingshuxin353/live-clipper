from __future__ import annotations

from pathlib import Path

from live_clipper import jobs
from live_clipper.utils import write_json
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


def _write_run(paths: WebPaths, *, phase: str = "needs_review") -> Path:
    run_dir = paths.output_root / "default" / "run-1"
    run_dir.mkdir(parents=True)
    write_json(run_dir / "review_brief.json", {"summary": "brief"})
    (run_dir / "review_notes.md").write_text("# Review\n", encoding="utf-8")
    write_json(run_dir / "selected_clips.template.json", _selection())
    write_json(run_dir / "merged_candidates.json", [_candidate()])
    write_json(
        paths.service_dir / "runs.json",
        {
            "runs": [
                {
                    "run_id": "run-1",
                    "source_id": "default",
                    "source_path": str(run_dir / "source.mp4"),
                    "local_source_path": None,
                    "run_dir": str(run_dir),
                    "fingerprint": "abc123",
                    "phase": phase,
                    "pid": None,
                    "log_path": str(paths.service_dir / "runs" / "run-1.log"),
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "updated_at": "2026-06-30T00:00:00+00:00",
                    "last_error": None,
                }
            ]
        },
    )
    return run_dir


def _write_config(path: Path) -> None:
    path.write_text(
        "\n".join([
            "[review_automation]",
            "enabled = true",
            "mode = 'local_agent'",
            "max_runs_per_tick = 1",
            "auto_render_after_selection = true",
            "on_failure = 'keep_needs_review'",
            "timeout_minutes = 60",
            "",
            "[review_automation.local_agent]",
            "provider = 'codex_cli'",
            "command_timeout_minutes = 1",
            "include_review_package_inline = true",
            "allow_agent_file_writes = false",
        ]),
        encoding="utf-8",
    )


def test_removed_paid_review_routes_preserve_legacy_record_and_create_no_job(tmp_path, monkeypatch):
    import pytest

    paths = _paths(tmp_path)
    run_dir = _write_run(paths)
    before = (paths.service_dir / "runs.json").read_bytes()
    monkeypatch.setattr(jobs, "start_job", lambda *a, **k: pytest.fail("must not schedule paid work"))
    for route in ("check", "run-due"):
        assert handle_api_request("POST", f"/api/review-automation/{route}", paths)[0] == 404
    status, _, payload = handle_api_request("POST", "/api/runs/run-1/ai-review", paths)
    assert status == 404
    assert payload["error_code"] == "route_not_found"
    assert (paths.service_dir / "runs.json").read_bytes() == before
    assert not (run_dir / "selected_clips.json").exists()
