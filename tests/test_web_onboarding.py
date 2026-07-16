from __future__ import annotations

from live_clipper.config import write_default_config
from live_clipper.web import WebPaths, handle_api_request


def _paths(tmp_path):
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def test_get_onboarding_needs_true_when_unconfigured(tmp_path):
    status, _headers, payload = handle_api_request("GET", "/api/onboarding", _paths(tmp_path))
    assert status == 200
    assert payload["needs_onboarding"] is True


def test_post_onboarding_test_source(tmp_path):
    source = tmp_path / "recordings"
    source.mkdir()
    status, _headers, payload = handle_api_request(
        "POST", "/api/onboarding/test-source", _paths(tmp_path), body={"source_dir": str(source)}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["video_count"] == 0


def test_post_onboarding_complete_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = _paths(tmp_path)
    write_default_config(paths.config_path)
    source = tmp_path / "recordings"
    source.mkdir()

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/onboarding/complete",
        paths,
        body={
            "source_dir": str(source),
            "llm_api_base": "https://example.test/v1",
            "llm_model": "test-model",
            "llm_api_key": "sk-test",
        },
    )
    assert status == 200
    assert payload["ok"] is True

    status, _headers, payload = handle_api_request("GET", "/api/onboarding", paths)
    assert status == 200
    assert payload["needs_onboarding"] is False
    assert payload["completed"] is True
