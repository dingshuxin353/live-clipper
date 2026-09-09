from __future__ import annotations

from pathlib import Path

import pytest

from live_clipper import service
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


def test_application_api_excludes_model_configuration_and_rejects_legacy_write(tmp_path):
    paths = _paths(tmp_path)
    _write_config(paths.config_path, tmp_path / "nas")
    before = paths.config_path.read_bytes()
    status, _, payload = handle_api_request("GET", "/api/config", paths)
    assert status == 200
    assert set(payload) == {"ok", "storage"}
    assert Path(payload["storage"]["work_dir"]).is_absolute()
    assert "secret-token" not in str(payload)
    status, _, rejected = handle_api_request("POST", "/api/config", paths,
        body={"expected_revision": "retired-revision", "config": {"llm": {"model": "legacy"}}})
    assert status == 410
    assert not rejected["ok"]
    assert paths.config_path.read_bytes() == before


def test_retired_configuration_actions_do_not_write_or_restart(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_config(paths.config_path, tmp_path / "nas")
    before = paths.config_path.read_bytes()
    monkeypatch.setattr(service, "start_service", lambda *a, **k: pytest.fail("retired route started service"))
    for action in ("validate", "llm-key", "asr-key", "restart-service"):
        status, _, payload = handle_api_request("POST", f"/api/config/{action}", paths,
            body={"api_key": "sentinel-secret"})
        assert status == 410
        assert "sentinel-secret" not in str(payload)
    assert paths.config_path.read_bytes() == before
    assert not (tmp_path / ".env").exists()
