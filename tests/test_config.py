from __future__ import annotations

from pathlib import Path

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


def test_write_default_config_creates_friendly_template(tmp_path):
    output_path = tmp_path / "live-clipper.toml"

    write_default_config(output_path)

    text = output_path.read_text(encoding="utf-8")
    assert text == DEFAULT_CONFIG_TEMPLATE
    assert "[paths]" in text
    assert "[llm]" in text
    assert "[prompts]" in text
