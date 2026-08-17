from __future__ import annotations

import json
from pathlib import Path

import pytest

from live_clipper import service
from live_clipper.config import (
    ReviewAutomationConfig,
    ReviewAutomationLocalAgentConfig,
    ReviewAutomationModelConfig,
    Settings,
)
from live_clipper.review_automation import (
    _run_local_agent_adapter,
    build_review_payload,
    check_environment,
    extract_selection_json,
    read_review_automation_events,
    run_ai_review_for_run,
    run_due_ai_reviews,
)
from live_clipper.utils import read_json, write_json


def _candidate(clip_id: str = "clip-1", *, score: float = 9.0) -> dict:
    return {
        "id": clip_id,
        "start": 10.0,
        "end": 20.0,
        "score": score,
        "clip_type": "highlight",
        "hook": "hook",
        "core_value": "value",
        "reason": "reason",
        "suggested_context_before": 2.0,
        "suggested_context_after": 2.0,
    }


def _selection(clip_id: str = "clip-1") -> list[dict]:
    return [
        {
            "clip_id": clip_id,
            "source_start": 10.0,
            "source_end": 20.0,
            "title": "clip title",
            "remove_ranges": [],
        }
    ]


def _write_run(service_dir: Path, run_dir: Path, *, run_id: str = "run-1", phase: str = "needs_review") -> dict:
    run_dir.mkdir(parents=True)
    write_json(run_dir / "codex_brief.json", {"summary": "brief"})
    (run_dir / "codex_review.md").write_text("# Review\n", encoding="utf-8")
    write_json(run_dir / "selected_clips.template.json", _selection())
    write_json(run_dir / "merged_candidates.json", [_candidate()])
    run = {
        "run_id": run_id,
        "source_id": "default",
        "source_path": str(run_dir / "source.mp4"),
        "local_source_path": None,
        "run_dir": str(run_dir),
        "fingerprint": "abc123",
        "phase": phase,
        "pid": None,
        "log_path": str(service_dir / "runs" / f"{run_id}.log"),
        "created_at": "2026-06-30T00:00:00+00:00",
        "updated_at": "2026-06-30T00:00:00+00:00",
        "last_error": None,
    }
    write_json(service_dir / "runs.json", {"runs": [run]})
    return run


def _settings(*, mode: str = "local_agent", enabled: bool = True, max_runs: int = 1) -> Settings:
    return Settings(
        review_automation=ReviewAutomationConfig(
            enabled=enabled,
            mode=mode,
            max_runs_per_tick=max_runs,
            local_agent=ReviewAutomationLocalAgentConfig(provider="codex_cli", command_timeout_minutes=1),
            model=ReviewAutomationModelConfig(max_candidates=1, retry_attempts=1),
        )
    )


def test_build_review_payload_truncates_candidates_for_model(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    run = _write_run(service_dir, run_dir)
    write_json(run_dir / "merged_candidates.json", [_candidate("clip-low", score=1), _candidate("clip-high", score=9)])

    payload = build_review_payload(run, max_candidates=1)

    assert payload["run_id"] == "run-1"
    assert payload["review_markdown"]["text"] == "# Review\n"
    assert payload["refined_candidates"]["content"][0]["id"] == "clip-high"
    assert payload["truncated"] is True
    assert payload["output_contract"]["path"] == "selected_clips.json"


def test_extract_selection_json_accepts_explanatory_text_and_fences():
    text = "说明\n```json\n[{\"clip_id\":\"clip-1\",\"source_start\":10,\"source_end\":20,\"title\":\"t\",\"remove_ranges\":[]}]\n```"

    assert extract_selection_json(text)[0]["clip_id"] == "clip-1"


def test_local_agent_validation_failure_does_not_write_selected_clips(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)

    def fake_runner(_prompt: str, **_kwargs):
        return {"ok": True, "stdout": json.dumps(_selection("unknown")), "stderr": ""}

    result = run_ai_review_for_run("run-1", _settings(), service_dir=service_dir, local_runner=fake_runner)

    assert result["ok"] is False
    assert result["error_code"] == "selection_validation_failed"
    assert not (run_dir / "selected_clips.json").exists()
    assert not (run_dir / "selected_clips.tmp.json").exists()
    assert any(event["type"] == "ai_review_validation_failed" for event in read_review_automation_events(service_dir))


def test_empty_ai_selection_keeps_run_retryable_without_writing_final_file(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)

    result = run_ai_review_for_run(
        "run-1",
        _settings(),
        service_dir=service_dir,
        local_runner=lambda *_args, **_kwargs: {"ok": True, "stdout": "[]", "stderr": ""},
    )

    assert result["ok"] is True
    assert result["status"] == "selection_empty"
    assert result["selected_count"] == 0
    assert not (run_dir / "selected_clips.json").exists()
    saved = service.find_run("run-1", service_dir)
    assert saved["phase"] == "needs_review"
    assert saved["selection_result"]["status"] == "selection_empty"
    assert any(event["type"] == "ai_review_selection_empty" for event in read_review_automation_events(service_dir))


def test_local_agent_success_writes_validated_selection_and_events(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)

    def fake_runner(_prompt: str, **kwargs):
        assert kwargs["provider"] == "codex_cli"
        assert "删除" in _prompt
        return {"ok": True, "stdout": json.dumps(_selection()), "stderr": "debug"}

    result = run_ai_review_for_run("run-1", _settings(), service_dir=service_dir, local_runner=fake_runner)

    assert result["ok"] is True
    assert result["selection_path"] == str(run_dir / "selected_clips.json")
    assert read_json(run_dir / "selected_clips.json")[0]["clip_id"] == "clip-1"
    events = [event["type"] for event in read_review_automation_events(service_dir)]
    assert "ai_review_selection_written" in events
    assert "ai_review_completed" in events


def test_local_agent_runs_in_isolated_cwd_without_real_run_dir_in_prompt(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    run = _write_run(service_dir, run_dir)
    payload = build_review_payload(run)

    def fake_runner(prompt: str, **kwargs):
        cwd = Path(kwargs["cwd"])
        assert cwd != run_dir
        assert cwd.exists()
        assert str(run_dir) not in prompt
        assert "selected_clips.json" in prompt
        assert not (run_dir / "selected_clips.json").exists()
        return {"ok": True, "stdout": json.dumps(_selection()), "stderr": ""}

    selection = _run_local_agent_adapter(_settings(), payload, run_dir=run_dir, local_runner=fake_runner)

    assert selection[0]["clip_id"] == "clip-1"
    assert not (run_dir / "selected_clips.json").exists()


@pytest.mark.parametrize("provider", ["codex_cli", "claude_code"])
def test_local_agent_isolates_cwd_for_codex_and_claude_providers(provider, tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    run = _write_run(service_dir, run_dir)
    payload = build_review_payload(run)
    settings = Settings(
        review_automation=ReviewAutomationConfig(
            local_agent=ReviewAutomationLocalAgentConfig(provider=provider),
        )
    )

    observed = {}

    def fake_runner(_prompt: str, **kwargs):
        observed["provider"] = kwargs["provider"]
        observed["cwd"] = Path(kwargs["cwd"])
        assert observed["cwd"].exists()
        return {"ok": True, "stdout": json.dumps(_selection()), "stderr": ""}

    selection = _run_local_agent_adapter(settings, payload, run_dir=run_dir, local_runner=fake_runner)

    assert observed["provider"] == provider
    assert observed["cwd"] != run_dir
    assert selection[0]["clip_id"] == "clip-1"


def test_local_agent_rejects_file_write_mode_even_if_settings_are_constructed_directly(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)
    settings = Settings(
        review_automation=ReviewAutomationConfig(
            local_agent=ReviewAutomationLocalAgentConfig(allow_agent_file_writes=True),
        )
    )

    result = run_ai_review_for_run("run-1", settings, service_dir=service_dir, local_runner=lambda *_args, **_kwargs: {"ok": True})

    assert result["ok"] is False
    assert result["error_code"] == "agent_file_writes_disabled"
    assert not (run_dir / "selected_clips.json").exists()


def test_model_adapter_uses_fake_client_and_candidate_limit(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)

    class FakeClient:
        def complete_json(self, system_prompt, user_payload, *, max_tokens, temperature):
            assert "只返回 JSON 数组" in system_prompt
            assert len(user_payload["refined_candidates"]["content"]) == 1
            assert max_tokens == 4096
            assert temperature == 0.2
            return _selection()

    result = run_ai_review_for_run(
        "run-1",
        _settings(mode="model"),
        service_dir=service_dir,
        client_factory=lambda settings, timeout, request_attempts: FakeClient(),
    )

    assert result["ok"] is True
    assert read_json(run_dir / "selected_clips.json")[0]["clip_id"] == "clip-1"


def test_model_adapter_uses_review_model_override(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    _write_run(service_dir, run_dir)
    settings = Settings(
        cheap_model_api_key="sk-test",
        review_automation=ReviewAutomationConfig(
            enabled=True,
            mode="model",
            model=ReviewAutomationModelConfig(model="review-model", retry_attempts=0),
        ),
    )

    class FakeClient:
        def complete_json(self, *_args, **_kwargs):
            return _selection()

    def fake_factory(settings_for_client, timeout, request_attempts):
        assert settings_for_client.cheap_model_name == "review-model"
        return FakeClient()

    result = run_ai_review_for_run(
        "run-1",
        settings,
        service_dir=service_dir,
        client_factory=fake_factory,
    )

    assert result["ok"] is True


def test_run_due_ai_reviews_respects_enabled_and_max_runs_per_tick(tmp_path):
    service_dir = tmp_path / "service"
    first = tmp_path / "output" / "default" / "run-1"
    second = tmp_path / "output" / "default" / "run-2"
    run1 = _write_run(service_dir, first, run_id="run-1")
    run2 = _write_run(service_dir, second, run_id="run-2")
    write_json(service_dir / "runs.json", {"runs": [run1, run2]})

    def fake_runner(_prompt: str, **_kwargs):
        return {"ok": True, "stdout": json.dumps(_selection()), "stderr": ""}

    disabled = run_due_ai_reviews(_settings(enabled=False), service_dir=service_dir, local_runner=fake_runner)
    enabled = run_due_ai_reviews(_settings(max_runs=1), service_dir=service_dir, local_runner=fake_runner)

    assert disabled["ok"] is True
    assert disabled["processed_runs"] == []
    assert disabled["skipped_reason"] == "review_automation_disabled"
    assert enabled["processed_runs"] == ["run-1"]
    assert (first / "selected_clips.json").exists()
    assert not (second / "selected_clips.json").exists()


def test_check_environment_reports_tools_and_llm_key_without_secret(monkeypatch):
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "sk-secret")
    settings = _settings(mode="model")
    settings = Settings(review_automation=settings.review_automation, llm=settings.llm)

    result = check_environment(settings, command_resolver=lambda command: f"/usr/bin/{command}")

    assert result["ok"] is True
    assert result["codex_cli"]["available"] is True
    assert result["claude_code"]["available"] is True
    assert result["llm"]["api_key_env"] == "CHEAP_MODEL_API_KEY"
    assert result["llm"]["api_key_configured"] is True
    assert "sk-secret" not in str(result)
