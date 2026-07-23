from __future__ import annotations

import json

import pytest

from live_clipper import onboarding
from live_clipper.config import Settings, write_default_config
from live_clipper.config_editor import load_editable_config


def test_onboarding_status_needs_when_unconfigured(tmp_path):
    status = onboarding.onboarding_status(Settings(), tmp_path / "service")
    assert status["needs_onboarding"] is True
    assert status["completed"] is False
    assert status["asr_api_base"] == ""
    assert status["asr_model"] == "mlx-community/whisper-large-v3-turbo"
    assert status["asr_key_present"] is False
    assert any(preset["id"] == "deepseek" for preset in status["presets"])


def test_onboarding_status_marker_wins(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "onboarding.json").write_text("{}", encoding="utf-8")
    status = onboarding.onboarding_status(Settings(), service_dir)
    assert status["needs_onboarding"] is False
    assert status["completed"] is True


def test_test_recording_source_missing(tmp_path):
    result = onboarding.test_recording_source(str(tmp_path / "nope"))
    assert result["ok"] is False
    assert result["error_code"] == "source_dir_missing"


def test_test_recording_source_counts_videos(tmp_path):
    source = tmp_path / "recordings"
    source.mkdir()
    (source / "a.mp4").write_bytes(b"x")
    (source / "b.txt").write_bytes(b"x")
    result = onboarding.test_recording_source(str(source))
    assert result["ok"] is True
    assert result["video_count"] == 1


def test_test_llm_requires_fields():
    result = onboarding.test_llm("", "", "")
    assert result["ok"] is False
    assert result["error_code"] == "llm_fields_missing"


def test_test_llm_auth_failure(monkeypatch):
    class FakeResponse:
        status_code = 401

    monkeypatch.setattr(onboarding.requests, "post", lambda *args, **kwargs: FakeResponse())
    result = onboarding.test_llm("https://example.test/v1", "bad-key", "test-model")
    assert result["ok"] is False
    assert result["error_code"] == "llm_auth_failed"


def test_test_llm_success(monkeypatch):
    class FakeResponse:
        status_code = 200

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr(onboarding.requests, "post", fake_post)
    result = onboarding.test_llm("https://example.test/v1/", "key", "test-model")
    assert result["ok"] is True
    assert captured["url"] == "https://example.test/v1/chat/completions"


def test_write_env_key_updates_existing_line(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CHEAP_MODEL_API_KEY=\nHF_TOKEN=abc\n", encoding="utf-8")
    onboarding._write_env_key(env_path, "CHEAP_MODEL_API_KEY", "sk-new")
    text = env_path.read_text(encoding="utf-8")
    assert "CHEAP_MODEL_API_KEY=sk-new" in text
    assert "HF_TOKEN=abc" in text
    assert text.count("CHEAP_MODEL_API_KEY") == 1


def test_complete_onboarding_writes_config_env_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    (source / "a.mp4").write_bytes(b"x")
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    env_path = tmp_path / ".env"
    service_dir = tmp_path / "work" / "service"

    result = onboarding.complete_onboarding(
        {
            "source_dir": str(source),
            "llm_api_base": "https://api.deepseek.com/v1",
            "llm_model": "deepseek-chat",
            "llm_api_key": "sk-llm-test",
            "asr_api_base": "https://asr.example.test/v1/",
            "asr_model": "whisper-1",
            "asr_api_key": "sk-asr-test",
        },
        config_path=config_path,
        env_path=env_path,
        service_dir=service_dir,
    )
    assert result["ok"] is True

    loaded = load_editable_config(config_path=config_path)
    assert loaded["config"]["recording_source_default"]["source_dir"] == str(source)
    assert loaded["config"]["llm"]["api_base"] == "https://api.deepseek.com/v1"
    assert loaded["config"]["llm"]["model"] == "deepseek-chat"
    assert loaded["config"]["asr"]["backend"] == "openai"
    assert loaded["config"]["asr"]["api_base"] == "https://asr.example.test/v1"
    assert loaded["config"]["asr"]["model"] == "whisper-1"
    env_text = env_path.read_text(encoding="utf-8")
    assert "CHEAP_MODEL_API_KEY=sk-llm-test" in env_text
    assert "ASR_API_KEY=sk-asr-test" in env_text
    config_text = config_path.read_text(encoding="utf-8")
    assert "sk-llm-test" not in config_text
    assert "sk-asr-test" not in config_text
    marker_text = (service_dir / "onboarding.json").read_text(encoding="utf-8")
    assert "sk-llm-test" not in marker_text
    assert "sk-asr-test" not in marker_text
    marker = json.loads(marker_text)
    assert marker["source_dir"] == str(source)


@pytest.mark.parametrize("missing_field", ["asr_api_base", "asr_model", "asr_api_key"])
def test_complete_onboarding_rejects_missing_asr_field(tmp_path, monkeypatch, missing_field):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    payload = {
        "source_dir": str(source),
        "llm_api_base": "https://llm.example.test/v1",
        "llm_model": "test-model",
        "llm_api_key": "sk-llm-test",
        "asr_api_base": "https://asr.example.test/v1",
        "asr_model": "whisper-1",
        "asr_api_key": "sk-asr-test",
    }
    payload[missing_field] = ""

    result = onboarding.complete_onboarding(
        payload,
        config_path=config_path,
        env_path=tmp_path / ".env",
        service_dir=tmp_path / "service",
    )

    assert result["ok"] is False
    assert result["error_code"] == "asr_fields_missing"
    assert not (tmp_path / "service" / "onboarding.json").exists()


def test_complete_onboarding_rejects_missing_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    result = onboarding.complete_onboarding(
        {
            "source_dir": str(tmp_path / "nope"),
            "llm_api_base": "https://x.test/v1",
            "llm_model": "m",
            "asr_api_base": "https://asr.example.test/v1",
            "asr_model": "whisper-1",
            "asr_api_key": "sk-asr-test",
        },
        config_path=config_path,
        env_path=tmp_path / ".env",
        service_dir=tmp_path / "service",
    )
    assert result["ok"] is False
    assert result["error_code"] == "source_dir_missing"
    assert not (tmp_path / "service" / "onboarding.json").exists()
