from __future__ import annotations

from live_clipper.config import load_settings


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
