from __future__ import annotations

import json
import tomllib

import pytest

from live_clipper import asr_models, onboarding
from live_clipper.config import RecordingSourceDefaultConfig, Settings, write_default_config
from live_clipper.config_editor import load_editable_config


def test_onboarding_status_needs_when_unconfigured(tmp_path):
    status = onboarding.onboarding_status(Settings(), tmp_path / "service")
    assert status["needs_onboarding"] is True
    assert status["completed"] is False
    assert status["skipped"] is False
    assert status["asr_api_base"] == ""
    assert status["asr_model"] == "mlx-community/whisper-large-v3-turbo"
    assert status["asr_key_present"] is False
    assert status["asr_backend"] == "mlx_whisper"
    assert status["initial_asr_mode"] == "local"
    assert status["initial_local_model"] == "mlx-community/whisper-small-mlx-q4"
    assert status["initial_local_model"] in asr_models.registry_ids()
    deepseek = next(preset for preset in status["presets"] if preset["id"] == "deepseek")
    assert deepseek["api_base"] == "https://api.deepseek.com"
    assert deepseek["model"] == "deepseek-v4-flash"


def test_onboarding_status_marker_wins(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "onboarding.json").write_text("{}", encoding="utf-8")
    status = onboarding.onboarding_status(Settings(), service_dir)
    assert status["needs_onboarding"] is False
    assert status["completed"] is True
    assert status["skipped"] is False


def test_onboarding_status_configured_without_marker_is_neither_finished_nor_needed(tmp_path):
    settings = Settings(recording_source_default=RecordingSourceDefaultConfig(source_dir=tmp_path / "recordings"))

    status = onboarding.onboarding_status(settings, tmp_path / "service")

    assert status["needs_onboarding"] is False
    assert status["completed"] is False
    assert status["skipped"] is False


def test_skip_onboarding_writes_minimal_marker_and_updates_status(tmp_path):
    service_dir = tmp_path / "service"

    result = onboarding.skip_onboarding(service_dir=service_dir)

    assert result == {"ok": True, "skipped": True, "completed": False}
    marker = json.loads((service_dir / "onboarding.json").read_text(encoding="utf-8"))
    assert set(marker) == {"skipped_at"}
    assert marker["skipped_at"]
    status = onboarding.onboarding_status(Settings(), service_dir)
    assert status["needs_onboarding"] is False
    assert status["completed"] is False
    assert status["skipped"] is True


def test_skip_onboarding_has_no_config_env_or_source_side_effects(tmp_path):
    service_dir = tmp_path / "service"
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    config_path.write_bytes(b"config-before\n")
    env_path.write_bytes(b"SECRET=before\n")

    onboarding.skip_onboarding(service_dir=service_dir)

    assert config_path.read_bytes() == b"config-before\n"
    assert env_path.read_bytes() == b"SECRET=before\n"
    assert not (tmp_path / "recordings").exists()
    marker_text = (service_dir / "onboarding.json").read_text(encoding="utf-8")
    for forbidden in ["source_dir", "api_key", "model", "job"]:
        assert forbidden not in marker_text


def test_skip_onboarding_does_not_create_config_or_env(tmp_path):
    onboarding.skip_onboarding(service_dir=tmp_path / "service")

    assert not (tmp_path / "live-clipper.toml").exists()
    assert not (tmp_path / ".env").exists()


def test_skip_onboarding_is_idempotent(tmp_path):
    service_dir = tmp_path / "service"
    first = onboarding.skip_onboarding(service_dir=service_dir)
    marker_before = (service_dir / "onboarding.json").read_bytes()

    second = onboarding.skip_onboarding(service_dir=service_dir)

    assert first == second == {"ok": True, "skipped": True, "completed": False}
    assert (service_dir / "onboarding.json").read_bytes() == marker_before


def test_skip_onboarding_does_not_overwrite_completed_marker(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    marker_path = service_dir / "onboarding.json"
    marker_path.write_text(
        json.dumps({"completed_at": "2026-07-27T10:00:00+09:00", "source_dir": "/recordings"}),
        encoding="utf-8",
    )
    marker_before = marker_path.read_bytes()

    result = onboarding.skip_onboarding(service_dir=service_dir)

    assert result == {
        "ok": True,
        "skipped": False,
        "completed": True,
        "already_finished": True,
    }
    assert marker_path.read_bytes() == marker_before


def test_skip_onboarding_does_not_overwrite_legacy_empty_marker(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    marker_path = service_dir / "onboarding.json"
    marker_path.write_text("{}", encoding="utf-8")

    result = onboarding.skip_onboarding(service_dir=service_dir)

    assert result["completed"] is True
    assert result["skipped"] is False
    assert marker_path.read_text(encoding="utf-8") == "{}"


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


def test_save_llm_api_key_persists_current_env_name_without_echo(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_LLM_KEY=old\nHF_TOKEN=keep\n", encoding="utf-8")

    result = onboarding.save_llm_api_key(
        "  sk-new  ",
        api_key_env="SECRET_LLM_KEY",
        env_path=env_path,
    )

    assert result == {
        "ok": True,
        "saved": True,
        "api_key_env": "SECRET_LLM_KEY",
        "message": "AI API key 已保存",
    }
    assert env_path.read_text(encoding="utf-8") == "SECRET_LLM_KEY=sk-new\nHF_TOKEN=keep\n"
    assert onboarding.os.environ["SECRET_LLM_KEY"] == "sk-new"
    assert "sk-new" not in str(result)
    monkeypatch.delenv("SECRET_LLM_KEY", raising=False)


@pytest.mark.parametrize(
    ("api_key_env", "api_key", "error_code"),
    [
        ("bad-name", "sk-valid", "invalid_api_key_env"),
        ("SECRET_LLM_KEY", "", "empty_api_key"),
        ("SECRET_LLM_KEY", "sk-first\nsk-injected", "invalid_api_key"),
    ],
)
def test_save_llm_api_key_rejects_unsafe_input(tmp_path, api_key_env, api_key, error_code):
    env_path = tmp_path / ".env"

    result = onboarding.save_llm_api_key(api_key, api_key_env=api_key_env, env_path=env_path)

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert not env_path.exists()


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
            "asr_mode": "cloud",
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
    assert result["asr_mode"] == "cloud"
    assert result["current_backend"] == "openai"
    assert result["current_model"] == "whisper-1"


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
        "asr_mode": "cloud",
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
            "asr_mode": "cloud",
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


def _local_payload(source) -> dict[str, str]:
    return {
        "source_dir": str(source),
        "llm_api_base": "https://llm.example.test/v1",
        "llm_model": "test-model",
        "llm_api_key": "sk-llm-secret",
        "asr_mode": "local",
        "asr_model": "mlx-community/whisper-small-mlx-q4",
        "asr_model_source": "modelscope",
    }


@pytest.mark.parametrize("model_source", ["modelscope", "huggingface"])
def test_complete_local_asr_writes_model_and_preserves_cloud_fields(
    tmp_path,
    monkeypatch,
    model_source,
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '[asr]\n',
            '[asr]\nunknown_asr_field = "keep-me"\n',
        ),
        encoding="utf-8",
    )
    before = tomllib.loads(config_path.read_text(encoding="utf-8"))["asr"]
    env_path = tmp_path / ".env"
    env_path.write_text("ASR_API_KEY=existing-cloud-key\nHF_TOKEN=existing-hf\n", encoding="utf-8")
    service_dir = tmp_path / "service"
    monkeypatch.setattr(
        onboarding.asr_models,
        "local_path_for",
        lambda model_id: tmp_path / "installed" if model_id == _local_payload(source)["asr_model"] else None,
    )
    payload = _local_payload(source)
    payload["asr_model_source"] = model_source

    result = onboarding.complete_onboarding(
        payload,
        config_path=config_path,
        env_path=env_path,
        service_dir=service_dir,
    )

    assert result == {
        "ok": True,
        "message": "初始设置完成",
        "config_path": str(config_path),
        "requires_service_restart": True,
        "asr_mode": "local",
        "current_backend": "mlx_whisper",
        "current_model": "mlx-community/whisper-small-mlx-q4",
        "model_source": model_source,
    }
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert raw["asr"]["backend"] == "mlx_whisper"
    assert raw["asr"]["model"] == "mlx-community/whisper-small-mlx-q4"
    assert raw["asr"]["model_source"] == model_source
    for field in ["language", "api_base", "api_key_env", "hf_token_env", "unknown_asr_field"]:
        assert raw["asr"][field] == before[field]
    env_text = env_path.read_text(encoding="utf-8")
    assert "ASR_API_KEY=existing-cloud-key" in env_text
    assert "HF_TOKEN=existing-hf" in env_text
    assert "CHEAP_MODEL_API_KEY=sk-llm-secret" in env_text
    marker_text = (service_dir / "onboarding.json").read_text(encoding="utf-8")
    combined = json.dumps(result, ensure_ascii=False) + config_path.read_text(encoding="utf-8") + marker_text
    assert "sk-llm-secret" not in combined
    assert "existing-cloud-key" not in combined


@pytest.mark.parametrize(
    ("payload_changes", "error_code"),
    [
        ({"asr_mode": "invalid"}, "invalid_asr_mode"),
        ({"asr_model": "unknown/model"}, "unknown_model"),
        ({"asr_model_source": "unknown"}, "unknown_model_source"),
        ({"asr_model_source": "hf-mirror"}, "unsupported_model_source"),
    ],
)
def test_complete_local_rejects_invalid_contract_without_writes(
    tmp_path,
    monkeypatch,
    payload_changes,
    error_code,
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    original_config = config_path.read_text(encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("ASR_API_KEY=keep\n", encoding="utf-8")
    original_env = env_path.read_text(encoding="utf-8")
    payload = _local_payload(source)
    payload.update(payload_changes)
    monkeypatch.setattr(
        onboarding.config_editor,
        "save_editable_config",
        lambda *args, **kwargs: pytest.fail("invalid payload must not save"),
    )

    result = onboarding.complete_onboarding(
        payload,
        config_path=config_path,
        env_path=env_path,
        service_dir=tmp_path / "service",
    )

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert config_path.read_text(encoding="utf-8") == original_config
    assert env_path.read_text(encoding="utf-8") == original_env
    assert not (tmp_path / "service" / "onboarding.json").exists()


@pytest.mark.parametrize("install_state", ["missing", "partial", "damaged"])
def test_complete_local_requires_installed_model(tmp_path, monkeypatch, install_state):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(onboarding.asr_models, "local_path_for", lambda model_id: None)

    result = onboarding.complete_onboarding(
        _local_payload(source),
        config_path=config_path,
        env_path=tmp_path / ".env",
        service_dir=tmp_path / "service",
    )

    assert result["ok"] is False
    assert result["error_code"] == "model_not_ready"
    assert config_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "service" / "onboarding.json").exists()
    assert install_state


@pytest.mark.parametrize("variable", ["ASR_MODEL", "ASR_BACKEND"])
def test_complete_local_rejects_environment_override(tmp_path, monkeypatch, variable):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.setenv(variable, "forced")
    monkeypatch.setattr(onboarding.asr_models, "local_path_for", lambda model_id: tmp_path / "installed")

    result = onboarding.complete_onboarding(
        _local_payload(source),
        config_path=config_path,
        env_path=tmp_path / ".env",
        service_dir=tmp_path / "service",
    )

    assert result["ok"] is False
    assert result["error_code"] == "asr_overridden_by_environment"
    assert config_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env").exists()


def test_complete_cloud_and_local_save_failure_leave_env_and_marker_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    write_default_config(config_path)
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")
    monkeypatch.setattr(onboarding.asr_models, "local_path_for", lambda model_id: tmp_path / "installed")
    monkeypatch.setattr(
        onboarding.config_editor,
        "save_editable_config",
        lambda *args, **kwargs: {"ok": False, "saved": False, "message": "save failed"},
    )
    cloud_payload = _local_payload(source) | {
        "asr_mode": "cloud",
        "asr_api_base": "https://asr.example.test/v1",
        "asr_model": "whisper-1",
        "asr_api_key": "sk-asr-secret",
    }

    for payload in [_local_payload(source), cloud_payload]:
        result = onboarding.complete_onboarding(
            payload,
            config_path=config_path,
            env_path=env_path,
            service_dir=tmp_path / "service",
        )
        assert result["ok"] is False
        assert env_path.read_text(encoding="utf-8") == "EXISTING=1\n"
        assert not (tmp_path / "service" / "onboarding.json").exists()
