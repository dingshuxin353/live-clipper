from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from live_clipper.cheap_model_client import CheapModelClient, CheapModelServiceError
from live_clipper.config import PrivacyConfig, Settings


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class InvalidJsonResponse:
    status_code = 200
    text = "<html>not json</html>"

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("invalid json body")


def test_complete_json_posts_openai_compatible_request_and_parses_content(monkeypatch):
    requests = []

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        requests.append((url, headers, json, timeout))
        return FakeResponse({
            "choices": [
                {"message": {"content": "{\"ok\": true, \"items\": [1]}"}},
            ]
        })

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(
        Settings(
            cheap_model_api_base="https://apihub.agnes-ai.com/v1",
            cheap_model_api_key="secret",
            cheap_model_name="agnes-2.0-flash",
        ),
        retry_delay_seconds=0,
    )

    result = client.complete_json("system prompt", {"window_id": "w001"}, max_tokens=512)

    assert result == {"ok": True, "items": [1]}
    assert requests == [(
        "https://apihub.agnes-ai.com/v1/chat/completions",
        {"Authorization": "Bearer secret", "Content-Type": "application/json"},
        {
            "model": "agnes-2.0-flash",
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "{\"window_id\":\"w001\"}"},
            ],
            "temperature": 0.1,
            "max_tokens": 512,
            "stream": False,
        },
        300,
    )]


def test_complete_json_does_not_repeat_paid_inference_for_invalid_output(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    responses = [
        FakeResponse({"choices": [{"message": {"content": "not json"}}]}),
        FakeResponse({"choices": [{"message": {"content": "{\"ok\": true}"}}]}),
    ]

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return responses.pop(0)

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    with pytest.raises(CheapModelServiceError, match="output_format_invalid"):
        client.complete_json("system", {"x": 1})
    assert len(responses) == 1


def test_complete_json_retries_request_failures_until_success(monkeypatch):
    calls = 0

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectTimeout("request timed out")
        return FakeResponse({
            "choices": [
                {"message": {"content": "{\"ok\": true}"}},
            ]
        })

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(
        Settings(
            cheap_model_api_base="https://apihub.agnes-ai.com/v1",
            cheap_model_api_key="secret",
            cheap_model_name="agnes-2.0-flash",
            privacy=PrivacyConfig(failure_log_mode="full"),
        ),
        retry_delay_seconds=0,
    )

    assert client.complete_json("system", {"x": 1}) == {"ok": True}
    assert calls == 2


def test_complete_json_accepts_markdown_json_fence(monkeypatch):
    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return FakeResponse({
            "choices": [
                {"message": {"content": "```json\n{\"ok\": true}\n```"}},
            ]
        })

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    assert client.complete_json("system", {"x": 1}) == {"ok": True}


def test_complete_json_extracts_json_after_leading_explanation(monkeypatch):
    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return FakeResponse({
            "choices": [
                {"message": {"content": "下面是结果：\n{\"ok\": true}\n请查收。"}},
            ]
        })

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    assert client.complete_json("system", {"x": 1}) == {"ok": True}


def test_complete_json_writes_failure_log_after_retry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return FakeResponse({
            "choices": [
                {"message": {"content": "still not json"}},
            ]
        })

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    try:
        client.complete_json("system", {"x": 1})
    except CheapModelServiceError:
        pass

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    assert "still not json" not in logs[0].read_text(encoding="utf-8")
    assert "output_format_invalid" in logs[0].read_text(encoding="utf-8")


def test_complete_json_writes_failure_log_for_request_exception(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = 0

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        nonlocal calls
        calls += 1
        raise requests.Timeout("request timed out")

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(
        Settings(
            cheap_model_api_base="https://apihub.agnes-ai.com/v1",
            cheap_model_api_key="secret",
            cheap_model_name="agnes-2.0-flash",
            privacy=PrivacyConfig(failure_log_mode="full"),
        ),
        request_attempts=3,
        retry_delay_seconds=0,
    )

    with pytest.raises(CheapModelServiceError, match="result_unknown"):
        client.complete_json("system", {"window_id": "w001"})

    assert calls == 1
    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "Timeout"
    assert log["error_code"] == "result_unknown"
    assert "user_payload" not in log
    assert log["attempt"] == 1
    assert log["request_attempts"] == 3


def test_complete_json_writes_failure_log_for_malformed_response(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return FakeResponse({"error": {"message": "bad gateway shape"}})

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    with pytest.raises(CheapModelServiceError, match="output_format_invalid"):
        client.complete_json("system", {"window_id": "w001"})

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "ValueError"
    assert log["error_code"] == "output_format_invalid"
    assert "bad gateway shape" not in logs[0].read_text()


def test_complete_json_writes_failure_log_for_non_json_http_body(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return InvalidJsonResponse()

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    with pytest.raises(CheapModelServiceError, match="output_format_invalid"):
        client.complete_json("system", {"window_id": "w001"})

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "ValueError"
    assert log["error_code"] == "output_format_invalid"
    assert "<html>" not in logs[0].read_text()


def test_client_redacts_failure_payload_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
    ))

    client._write_failure_log(
        "system prompt with private context",
        {"sentences": [{"text": "private transcript"}]},
        "private model response",
    )

    [log_path] = sorted((tmp_path / "work" / "logs").glob("cheap_model_failure_*.json"))
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert not {"system_prompt", "user_payload", "content"} & log.keys()
    assert "private" not in log_path.read_text()
