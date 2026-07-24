from __future__ import annotations

from pathlib import Path

from live_clipper import service
from live_clipper.config_editor import load_editable_config
from live_clipper.utils import read_json, write_json
from live_clipper.web import WebPaths, handle_api_request


def _write_config(path: Path, source_dir: Path) -> None:
    path.write_text(
        "\n".join([
            "[paths]",
            "input_dir = 'input'",
            "output_root = 'output'",
            "",
            "[recording_source.default]",
            f"source_dir = '{source_dir}'",
            "input_dir = 'input'",
            "output_root = 'output'",
            "since_hours = 168",
            "min_age_minutes = 10",
            "stable_check_seconds = 60",
            "",
            "[llm]",
            "api_base = 'https://example.test/v1'",
            "api_key_env = 'SECRET_LLM_KEY'",
            "model = 'agnes-test'",
            "",
            "[asr]",
            "backend = 'mlx_whisper'",
            "model = 'mlx-community/whisper-large-v3-turbo'",
            "language = 'zh'",
            "api_key_env = 'ASR_API_KEY'",
            "hf_token_env = 'HF_TOKEN'",
            "",
            "[service]",
            "enabled = true",
            "scan_interval_minutes = 30",
            "auto_render_after_selection = true",
            "cleanup_mode = 'preview_only'",
            "",
            "[web]",
            "host = '127.0.0.1'",
            "port = 8765",
            "access_token = 'secret-token'",
        ]),
        encoding="utf-8",
    )


def _paths(tmp_path: Path) -> WebPaths:
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "work" / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def test_get_api_config_redacts_secrets_and_returns_env_status(monkeypatch, tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    monkeypatch.setenv("SECRET_LLM_KEY", "sk-secret")

    status, _headers, payload = handle_api_request("GET", "/api/config", _paths(tmp_path))

    assert status == 200
    assert payload["ok"] is True
    assert payload["config"]["llm"]["api_key_env"] == "SECRET_LLM_KEY"
    assert payload["env_status"]["SECRET_LLM_KEY"] is True
    assert "sk-secret" not in str(payload)
    assert "secret-token" not in str(payload)
    assert payload["config"]["asr"]["model_source"] == "modelscope"


def test_post_api_config_validate_returns_chinese_errors(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    _write_config(tmp_path / "live-clipper.toml", source_dir)
    draft = load_editable_config(config_path=tmp_path / "live-clipper.toml")["config"]
    draft["recording_source_default"]["source_dir"] = str(tmp_path / "missing")
    draft["service"]["scan_interval_minutes"] = 0

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/config/validate",
        _paths(tmp_path),
        body={"config": draft},
    )

    assert status == 200
    assert payload["ok"] is False
    messages = "\n".join(error["message"] for error in payload["errors"])
    assert "录播源目录不存在" in messages
    assert "必须在 1 到 1440 之间" in messages


def test_post_api_config_saves_backup_and_loadable_file(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    draft = load_editable_config(config_path=config_path)["config"]
    draft["service"]["scan_interval_minutes"] = 15
    draft["asr"]["model_source"] = "huggingface"

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/config",
        _paths(tmp_path),
        body={"config": draft},
    )

    assert status == 200
    assert payload["ok"] is True
    assert Path(payload["backup_path"]).exists()
    assert "scan_interval_minutes = 15" in config_path.read_text(encoding="utf-8")
    assert payload["requires_service_restart"] is True
    assert load_editable_config(config_path=config_path)["config"]["asr"]["model_source"] == "huggingface"


def test_config_api_migrates_legacy_hf_mirror_and_rejects_new_value(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[asr]\n",
            '[asr]\nmodel_source = "hf-mirror"\n',
        ),
        encoding="utf-8",
    )

    status, _headers, payload = handle_api_request("GET", "/api/config", _paths(tmp_path))
    assert status == 200
    assert payload["config"]["asr"]["model_source"] == "modelscope"

    draft = payload["config"]
    draft["asr"]["model_source"] = "hf-mirror"
    status, _headers, payload = handle_api_request(
        "POST",
        "/api/config",
        _paths(tmp_path),
        body={"config": draft},
    )
    assert status == 400
    assert payload["ok"] is False
    assert 'model_source = "hf-mirror"' in config_path.read_text(encoding="utf-8")


def test_post_api_config_refuses_parse_error_without_overwrite(tmp_path):
    config_path = tmp_path / "live-clipper.toml"
    config_path.write_text("[service\nbroken = true", encoding="utf-8")

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/config",
        _paths(tmp_path),
        body={"config": {"service": {"scan_interval_minutes": 15}}},
    )

    assert status == 400
    assert payload["ok"] is False
    assert "配置文件解析失败" in payload["message"]
    assert config_path.read_text(encoding="utf-8") == "[service\nbroken = true"


def test_restart_service_api_returns_stopped_when_service_is_not_running(tmp_path):
    status, _headers, payload = handle_api_request("POST", "/api/config/restart-service", _paths(tmp_path))

    assert status == 200
    assert payload["ok"] is True
    assert payload["restarted"] is False
    assert payload["reason"] == "service_not_running"


def test_restart_service_api_stops_and_starts_running_service(monkeypatch, tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    _write_config(tmp_path / "live-clipper.toml", source_dir)
    paths = _paths(tmp_path)
    service_dir = paths.service_dir
    service_dir.mkdir(parents=True)
    (service_dir / "service.pid").write_text("1234\n", encoding="utf-8")
    (service_dir / "service.json").write_text('{"status":"running","pid":1234}', encoding="utf-8")
    monkeypatch.setattr(service, "pid_is_running", lambda pid: pid == 1234)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: None)
    def fake_start_service(settings, service_dir):
        write_json(service_dir / "service.json", {"status": "running", "pid": 5678})
        return {"ok": True, "started": True, "pid": 5678, "service_dir": str(service_dir)}

    monkeypatch.setattr(service, "start_service", fake_start_service)

    status, _headers, payload = handle_api_request("POST", "/api/config/restart-service", paths)

    assert status == 200
    assert payload["ok"] is True
    assert payload["restarted"] is True
    assert payload["stop"]["stopped"] is True
    assert payload["start"]["started"] is True
    assert read_json(service_dir / "service.json")["status"] == "running"
