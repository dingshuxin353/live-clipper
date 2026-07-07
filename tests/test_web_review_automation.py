from __future__ import annotations

import json
from pathlib import Path

from live_clipper import review_automation, web
from live_clipper.utils import read_json, write_json
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
    write_json(run_dir / "codex_brief.json", {"summary": "brief"})
    (run_dir / "codex_review.md").write_text("# Review\n", encoding="utf-8")
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


def test_get_api_review_automation_returns_status(tmp_path):
    paths = _paths(tmp_path)

    status, _headers, payload = handle_api_request("GET", "/api/review-automation", paths)

    assert status == 200
    assert payload["ok"] is True
    assert payload["review_automation"]["enabled"] is False
    assert payload["environment"]["llm"]["api_key_env"] == "CHEAP_MODEL_API_KEY"
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).replace("api_key_env", "").replace("api_key_configured", "")


def test_post_api_review_automation_check_uses_environment_status(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(review_automation, "check_environment", lambda settings: {"ok": True, "current_mode_available": True})

    status, _headers, payload = handle_api_request("POST", "/api/review-automation/check", paths)

    assert status == 200
    assert payload["ok"] is True
    assert payload["current_mode_available"] is True


def test_post_api_run_ai_review_executes_and_writes_selection(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _write_config(paths.config_path)
    run_dir = _write_run(paths)

    def fake_runner(_prompt: str, **_kwargs):
        return {"ok": True, "stdout": json.dumps(_selection()), "stderr": ""}

    monkeypatch.setattr(review_automation, "_default_local_runner", fake_runner)

    status, _headers, payload = handle_api_request("POST", "/api/runs/run-1/ai-review", paths)

    assert status == 200
    assert payload["ok"] is True
    assert read_json(run_dir / "selected_clips.json")[0]["clip_id"] == "clip-1"


def test_post_api_run_ai_review_rejects_invalid_phase_with_chinese_error(tmp_path):
    paths = _paths(tmp_path)
    _write_config(paths.config_path)
    _write_run(paths, phase="processing")

    status, _headers, payload = handle_api_request("POST", "/api/runs/run-1/ai-review", paths)

    assert status == 400
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_phase"
    assert "needs_review" in payload["message"]


def test_post_api_run_ai_review_rejects_existing_selection(tmp_path):
    paths = _paths(tmp_path)
    _write_config(paths.config_path)
    run_dir = _write_run(paths)
    write_json(run_dir / "selected_clips.json", _selection())

    status, _headers, payload = handle_api_request("POST", "/api/runs/run-1/ai-review", paths)

    assert status == 400
    assert payload["ok"] is False
    assert payload["error_code"] == "selected_clips_exists"


def test_post_api_review_automation_run_due_delegates(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _write_config(paths.config_path)

    def fake_run_due(settings, service_dir):
        assert service_dir == paths.service_dir
        return {"ok": True, "processed_runs": ["run-1"], "results": []}

    monkeypatch.setattr(review_automation, "run_due_ai_reviews", fake_run_due)

    status, _headers, payload = handle_api_request("POST", "/api/review-automation/run-due", paths)

    assert status == 200
    assert payload["ok"] is True
    assert payload["processed_runs"] == ["run-1"]


def test_build_run_detail_includes_ai_review_failure_for_same_run(tmp_path):
    paths = _paths(tmp_path)
    _write_run(paths)
    write_json(review_automation._summary_path(paths.service_dir), {
        "last_run_id": "run-1",
        "last_status": "failed",
        "last_error": "boom",
        "last_run_at": "2026-07-06T00:00:00+00:00",
    })

    detail = web.build_run_detail("run-1", paths)

    assert detail["ai_review"]["status"] == "failed"
    assert detail["ai_review"]["error"] == "boom"

    write_json(review_automation._summary_path(paths.service_dir), {
        "last_run_id": "other-run",
        "last_status": "failed",
        "last_error": "boom",
        "last_run_at": "2026-07-06T00:00:00+00:00",
    })

    detail = web.build_run_detail("run-1", paths)

    assert detail["ai_review"] is None
