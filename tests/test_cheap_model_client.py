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
    text = "<html>not json</html>"

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("invalid json body")


def test_complete_json_posts_openai_compatible_request_and_parses_content(monkeypatch):
    requests = []

    def fake_post(url, headers, json, timeout):
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


def test_complete_json_retries_once_when_content_is_not_json(monkeypatch):
    responses = [
        FakeResponse({"choices": [{"message": {"content": "not json"}}]}),
        FakeResponse({"choices": [{"message": {"content": "{\"ok\": true}"}}]}),
    ]

    def fake_post(url, headers, json, timeout):
        return responses.pop(0)

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    assert client.complete_json("system", {"x": 1}) == {"ok": True}


def test_complete_json_retries_request_failures_until_success(monkeypatch):
    calls = 0

    def fake_post(url, headers, json, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("request timed out")
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
    def fake_post(url, headers, json, timeout):
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
    def fake_post(url, headers, json, timeout):
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

    def fake_post(url, headers, json, timeout):
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
    except ValueError:
        pass

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    assert "still not json" in logs[0].read_text(encoding="utf-8")


def test_complete_json_writes_failure_log_for_request_exception(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = 0

    def fake_post(url, headers, json, timeout):
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

    with pytest.raises(CheapModelServiceError, match="request timed out"):
        client.complete_json("system", {"window_id": "w001"})

    assert calls == 3
    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "Timeout"
    assert log["error"] == "request timed out"
    assert log["user_payload"] == {"window_id": "w001"}
    assert log["attempt"] == 3
    assert log["request_attempts"] == 3


def test_complete_json_writes_failure_log_for_malformed_response(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_post(url, headers, json, timeout):
        return FakeResponse({"error": {"message": "bad gateway shape"}})

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    with pytest.raises(ValueError, match="OpenAI-compatible"):
        client.complete_json("system", {"window_id": "w001"})

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "ValueError"
    assert "OpenAI-compatible" in log["error"]
    assert log["response_payload"] == {"error": {"message": "bad gateway shape"}}


def test_complete_json_writes_failure_log_for_non_json_http_body(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_post(url, headers, json, timeout):
        return InvalidJsonResponse()

    monkeypatch.setattr("live_clipper.cheap_model_client.requests.post", fake_post)
    client = CheapModelClient(Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        privacy=PrivacyConfig(failure_log_mode="full"),
    ))

    with pytest.raises(ValueError, match="invalid json body"):
        client.complete_json("system", {"window_id": "w001"})

    logs = list(Path("work/logs").glob("cheap_model_failure_*.json"))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["error_type"] == "ValueError"
    assert log["error"] == "invalid json body"
    assert log["content"] == "<html>not json</html>"


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
    assert log["system_prompt"] == "[redacted]"
    assert log["user_payload"] == "[redacted]"
    assert log["content"] == "[redacted]"
