from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    run_due_ai_reviews,
    run_structured_review_adapter,
)
from live_clipper.utils import write_json


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
    write_json(run_dir / "review_brief.json", {"summary": "brief"})
    (run_dir / "review_notes.md").write_text("# Review\n", encoding="utf-8")
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
            local_agent=ReviewAutomationLocalAgentConfig(provider="claude_code", command_timeout_minutes=1),
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


@pytest.mark.parametrize("provider", ["claude_code"])
def test_local_agent_isolates_cwd_for_claude(provider, tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "output" / "default" / "run-1"
    run = _write_run(service_dir, run_dir)
    payload = build_review_payload(run)
    settings = Settings(
        review_automation=ReviewAutomationConfig(
            mode="local_agent",
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
            mode="local_agent",
            local_agent=ReviewAutomationLocalAgentConfig(allow_agent_file_writes=True),
        )
    )

    from live_clipper.review_automation import ReviewAutomationError, _run_local_agent_adapter
    with pytest.raises(ReviewAutomationError, match="直接写文件"):
        _run_local_agent_adapter(settings, {}, run_dir=run_dir, local_runner=lambda *_a, **_k: pytest.fail("must not start"))
    assert not (run_dir / "selected_clips.json").exists()






def test_run_due_ai_reviews_respects_enabled_and_max_runs_per_tick(tmp_path):
    service_dir = tmp_path / "service"
    first = tmp_path / "output" / "default" / "run-1"
    second = tmp_path / "output" / "default" / "run-2"
    run1 = _write_run(service_dir, first, run_id="run-1")
    run2 = _write_run(service_dir, second, run_id="run-2")
    write_json(service_dir / "runs.json", {"runs": [run1, run2]})

    def fake_runner(_prompt: str, **_kwargs):
        pytest.fail("unknown original identity must not invoke Agent")

    disabled = run_due_ai_reviews(_settings(enabled=False), service_dir=service_dir, local_runner=fake_runner)
    enabled = run_due_ai_reviews(_settings(max_runs=1), service_dir=service_dir, local_runner=fake_runner)

    assert disabled["ok"] is True
    assert disabled["processed_runs"] == []
    assert disabled["skipped_reason"] == "review_automation_disabled"
    assert enabled["processed_runs"] == []
    assert all(result["error_code"] == "original_configuration_unknown" for result in enabled["results"])
    assert not (first / "selected_clips.json").exists()
    assert not (second / "selected_clips.json").exists()


def test_check_environment_reports_tools_and_llm_key_without_secret(monkeypatch):
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "sk-secret")
    settings = _settings(mode="model")
    settings = Settings(review_automation=settings.review_automation, llm=settings.llm)

    result = check_environment(settings, command_resolver=lambda command: f"/usr/bin/{command}")

    assert result["ok"] is True
    assert "codex_cli" not in result
    assert result["claude_code"]["available"] is True
    assert result["llm"]["api_key_env"] == "CHEAP_MODEL_API_KEY"
    assert result["llm"]["api_key_configured"] is True
    assert "sk-secret" not in str(result)


def test_structured_project_adapter_uses_one_request_and_versioned_object():
    settings = Settings(
        cheap_model_api_key="fake-key",
        review_automation=ReviewAutomationConfig(
            mode="model",
            model=ReviewAutomationModelConfig(model="review-model", retry_attempts=2),
        ),
    )

    class FakeClient:
        def complete_json(self, system_prompt, payload, **_kwargs):
            schema_text = system_prompt.split("<review_result_json_schema>\n", 1)[1].split(
                "\n</review_result_json_schema>",
                1,
            )[0]
            schema = json.loads(schema_text)
            assert schema["required"] == ["format_version", "overall_summary", "decisions"]
            assert schema["$defs"]["ReviewDecision"]["required"] == [
                "candidate_id",
                "decision",
                "rank",
                "reason",
            ]
            assert payload["candidates"][0]["candidate_id"] == "one"
            return {
                "format_version": 1,
                "overall_summary": "none",
                "warnings": [],
                "decisions": [
                    {
                        "candidate_id": "one",
                        "decision": "rejected",
                        "rank": 1,
                        "reason": "low value",
                        "rejection_reason_code": "low_value",
                        "selected_clip": None,
                        "material": None,
                    }
                ],
            }

    def factory(_settings, timeout, request_attempts):
        assert timeout == 3600
        assert request_attempts == 1
        return FakeClient()

    result = run_structured_review_adapter(
        settings,
        {"candidates": [{"candidate_id": "one"}]},
        client_factory=factory,
    )

    assert result["format_version"] == 1
    assert result["decisions"][0]["decision"] == "rejected"


def test_structured_project_adapter_contract_covers_empty_candidates():
    settings = Settings(
        cheap_model_api_key="fake-key",
        review_automation=ReviewAutomationConfig(
            mode="model",
            model=ReviewAutomationModelConfig(model="review-model"),
        ),
    )

    class FakeClient:
        def complete_json(self, system_prompt, payload, **_kwargs):
            assert "没有候选时返回空数组" in system_prompt
            assert payload["candidates"] == []
            return {
                "format_version": 1,
                "overall_summary": "没有形成候选片段",
                "warnings": [],
                "decisions": [],
            }

    result = run_structured_review_adapter(
        settings,
        {"candidates": []},
        client_factory=lambda *_args, **_kwargs: FakeClient(),
    )

    assert result["overall_summary"] == "没有形成候选片段"
    assert result["decisions"] == []
