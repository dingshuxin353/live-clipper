from __future__ import annotations

from pathlib import Path

import pytest

from live_clipper import asr_models, onboarding_resources
from live_clipper.config import write_default_config


def test_normalize_api_base_rejects_private_and_credential_urls() -> None:
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.normalize_api_base("https://user:pass@example.test/v1")
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.normalize_api_base("https://10.0.0.1/v1")
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.normalize_api_base("https://169.254.169.254/latest")


def test_loopback_is_only_allowed_when_explicitly_enabled() -> None:
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.normalize_api_base("http://127.0.0.1:1234/v1")
    assert onboarding_resources.normalize_api_base("http://127.0.0.1:1234/v1", allow_loopback=True).endswith("/v1")


def test_explicit_env_loader_does_not_read_working_directory_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / "isolated.env"
    write_default_config(config_path)
    env_path.write_text("CHEAP_MODEL_API_KEY=isolated-secret\n", encoding="utf-8")
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "process-secret")
    settings = onboarding_resources.load_settings_explicit(config_path, env_path)
    assert settings.llm.api_key == "isolated-secret"
    assert settings.cheap_model_api_key == "isolated-secret"


def test_commit_failure_restores_config_and_keeps_secret_out_of_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    write_default_config(config_path)
    original = config_path.read_bytes()
    monkeypatch.setattr(
        onboarding_resources,
        "write_env_secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write blocked")),
    )
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.commit_llm_configuration(
            config_path=config_path,
            env_path=env_path,
            provider_label="Test",
            api_base="https://provider.example/v1",
            model="model-a",
            api_key="sentinel-secret",
        )
    assert config_path.read_bytes() == original
    assert not env_path.exists()


def test_catalog_exposes_one_recommended_balanced_model() -> None:
    recommended = asr_models.recommended_model()
    assert recommended["tier"] == "balanced"
    assert recommended["recommended"] is True
