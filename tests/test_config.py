from __future__ import annotations

from pathlib import Path

import pytest

from live_clipper.config import DEFAULT_CONFIG_TEMPLATE, load_settings, write_default_config


def test_load_settings_uses_mvp_defaults_when_env_is_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in [
        "CHEAP_MODEL_API_BASE",
        "CHEAP_MODEL_API_KEY",
        "CHEAP_MODEL_NAME",
        "ASR_BACKEND",
        "ASR_MODEL",
        "HF_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.cheap_model_api_base == "https://apihub.agnes-ai.com/v1"
    assert settings.cheap_model_api_key is None
    assert settings.cheap_model_name == "agnes-2.0-flash"
    assert settings.asr_backend == "mlx_whisper"
    assert settings.asr_model == "mlx-community/whisper-large-v3-turbo"
    assert settings.asr.model_source == "modelscope"


def test_load_settings_defaults_openai_asr_model_when_backend_is_openai(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in [
        "CHEAP_MODEL_API_BASE",
        "CHEAP_MODEL_API_KEY",
        "CHEAP_MODEL_NAME",
        "ASR_MODEL",
        "HF_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASR_BACKEND", "openai")

    settings = load_settings()

    assert settings.asr_backend == "openai"
    assert settings.asr_model == "whisper-1"


def test_load_settings_prefers_local_dotenv_over_existing_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "bad-placeholder")
    (tmp_path / ".env").write_text("CHEAP_MODEL_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    settings = load_settings()

    assert settings.cheap_model_api_key == "sk-from-dotenv"


def test_load_settings_reads_grouped_config_and_env_secret(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in [
        "ASR_BACKEND",
        "ASR_MODEL",
        "ASR_LANGUAGE",
        "CHEAP_MODEL_API_BASE",
        "CHEAP_MODEL_API_KEY",
        "CHEAP_MODEL_NAME",
        "LLM_API_BASE",
        "LLM_MODEL",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLIC_LLM_KEY", "sk-from-env")
    (tmp_path / "live-clipper.toml").write_text(
        "\n".join([
            "[paths]",
            "input_dir = 'media/input'",
            "output_root = 'media/output'",
            "",
            "[asr]",
            "backend = 'openai'",
            "language = 'auto'",
            "",
            "[llm]",
            "api_base = 'https://example.test/v1'",
            "api_key_env = 'PUBLIC_LLM_KEY'",
            "model = 'small-json-model'",
            "",
            "[prompts]",
            "directory = 'prompts.local'",
            "",
            "[privacy]",
            "failure_log_mode = 'full'",
            "failure_log_max_chars = 123",
            "",
            "[web]",
            "host = '127.0.0.1'",
            "port = 9876",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.paths.input_dir == Path("media/input")
    assert settings.paths.output_root == Path("media/output")
    assert settings.asr.backend == "openai"
    assert settings.asr.language == "auto"
    assert settings.llm.api_base == "https://example.test/v1"
    assert settings.llm.api_key == "sk-from-env"
    assert settings.llm.model == "small-json-model"
    assert settings.prompts.directory == Path("prompts.local")
    assert settings.privacy.failure_log_mode == "full"
    assert settings.privacy.failure_log_max_chars == 123
    assert settings.web.host == "127.0.0.1"
    assert settings.web.port == 9876
    assert settings.cheap_model_api_key == "sk-from-env"


def test_load_settings_supports_service_and_default_recording_source(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "live-clipper.toml").write_text(
        "\n".join([
            "[service]",
            "enabled = true",
            "scan_interval_minutes = 15",
            "auto_render_after_selection = true",
            "cleanup_mode = 'preview_only'",
            "",
            "[recording_source.default]",
            "source_dir = '/Volumes/recordings'",
            "input_dir = 'input'",
            "output_root = 'output'",
            "since_hours = 168",
            "min_age_minutes = 20",
            "stable_check_seconds = 5",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.service.scan_interval_minutes == 15
    assert settings.service.cleanup_mode == "preview_only"
    assert settings.recording_source_default.source_id == "default"
    assert settings.recording_source_default.source_dir == Path("/Volumes/recordings")
    assert settings.recording_source_default.input_dir == Path("input")
    assert settings.recording_source_default.output_root == Path("output")
    assert settings.recording_source_default.since_hours == 168
    assert settings.recording_source_default.min_age_minutes == 20
    assert settings.recording_source_default.stable_check_seconds == 5


def test_load_settings_uses_default_scheduler_jobs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.scheduler.enabled is True
    assert settings.scheduler.timezone == "Asia/Shanghai"
    assert settings.scheduler.tick_seconds == 30
    assert settings.scheduler.missed_policy == "run_once"
    assert [job.id for job in settings.scheduler.jobs] == ["weekly_recording_scan", "weekly_review_due"]
    assert settings.scheduler.jobs[0].type == "scan_recordings"
    assert settings.scheduler.jobs[0].day_of_week == "sun"
    assert settings.scheduler.jobs[0].time == "00:00"
    assert settings.scheduler.jobs[1].type == "review_due_check"
    assert settings.scheduler.jobs[1].time == "12:00"
    assert settings.review_automation.enabled is False
    assert settings.review_automation.mode == "local_agent"
    assert settings.review_automation.local_agent.provider == "codex_cli"
    assert settings.review_automation.local_agent.allow_agent_file_writes is False
    assert settings.review_automation.model.provider == "openai_compatible"
    assert settings.review_automation.model.use_llm_config is True


def test_load_settings_reads_scheduler_config_from_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "live-clipper.toml").write_text(
        "\n".join([
            "[scheduler]",
            "enabled = true",
            "timezone = 'Asia/Tokyo'",
            "tick_seconds = 10",
            "missed_policy = 'skip'",
            "state_dir = 'work/service'",
            "",
            "[[scheduler.jobs]]",
            "id = 'daily_scan'",
            "name = '每日扫描'",
            "enabled = false",
            "type = 'scan_recordings'",
            "schedule = 'daily'",
            "time = '08:30'",
            "skip_if_running = true",
            "",
            "[[scheduler.jobs]]",
            "id = 'interval_maintenance'",
            "name = '维护检查'",
            "enabled = true",
            "type = 'maintenance_check'",
            "schedule = 'interval_minutes'",
            "interval_minutes = 60",
            "skip_if_running = true",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.scheduler.timezone == "Asia/Tokyo"
    assert settings.scheduler.tick_seconds == 10
    assert settings.scheduler.missed_policy == "skip"
    assert settings.scheduler.jobs[0].id == "daily_scan"
    assert settings.scheduler.jobs[0].enabled is False
    assert settings.scheduler.jobs[0].schedule == "daily"
    assert settings.scheduler.jobs[0].time == "08:30"
    assert settings.scheduler.jobs[1].interval_minutes == 60


def test_load_settings_reads_review_automation_config_from_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "live-clipper.toml").write_text(
        "\n".join([
            "[review_automation]",
            "enabled = true",
            "mode = 'model'",
            "max_runs_per_tick = 2",
            "auto_render_after_selection = true",
            "on_failure = 'mark_failed'",
            "timeout_minutes = 45",
            "prompt_template = 'default_clip_review'",
            "",
            "[review_automation.local_agent]",
            "provider = 'claude_code'",
            "command_timeout_minutes = 30",
            "include_review_package_inline = false",
            "allow_agent_file_writes = false",
            "",
            "[review_automation.model]",
            "provider = 'openai_compatible'",
            "use_llm_config = true",
            "model = 'review-model'",
            "max_candidates = 12",
            "temperature = 0.4",
            "max_tokens = 2048",
            "retry_attempts = 3",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.review_automation.enabled is True
    assert settings.review_automation.mode == "model"
    assert settings.review_automation.max_runs_per_tick == 2
    assert settings.review_automation.on_failure == "mark_failed"
    assert settings.review_automation.timeout_minutes == 45
    assert settings.review_automation.local_agent.provider == "claude_code"
    assert settings.review_automation.local_agent.command_timeout_minutes == 30
    assert settings.review_automation.local_agent.include_review_package_inline is False
    assert settings.review_automation.model.model == "review-model"
    assert settings.review_automation.model.max_candidates == 12
    assert settings.review_automation.model.temperature == 0.4
    assert settings.review_automation.model.max_tokens == 2048
    assert settings.review_automation.model.retry_attempts == 3


def test_write_default_config_creates_friendly_template(tmp_path):
    output_path = tmp_path / "live-clipper.toml"

    write_default_config(output_path)

    text = output_path.read_text(encoding="utf-8")
    assert text == DEFAULT_CONFIG_TEMPLATE
    assert "[paths]" in text
    assert "[llm]" in text
    assert "[prompts]" in text
    assert "[scheduler]" in text
    assert "[[scheduler.jobs]]" in text
    assert "[review_automation]" in text
    assert "[review_automation.local_agent]" in text
    assert "[review_automation.model]" in text
    assert 'model_source = "modelscope"' in text


@pytest.mark.parametrize("source", ["modelscope", "hf-mirror", "huggingface"])
def test_load_settings_preserves_explicit_model_source(monkeypatch, tmp_path, source):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "live-clipper.toml").write_text(
        f'[asr]\nmodel_source = "{source}"\n',
        encoding="utf-8",
    )

    assert load_settings().asr.model_source == source
