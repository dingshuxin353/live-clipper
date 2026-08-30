from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from live_clipper import asr_models, onboarding_resources, service
from live_clipper.config import Settings
from live_clipper.onboarding_coordinator import OnboardingCoordinator


class _ResourceHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, bytes, dict[str, str]]] = []
    response_payload = {"choices": [{"message": {"content": "OK"}}]}
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append((self.path, body, dict(self.headers)))
        payload = json.dumps(self.response_payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


@pytest.fixture
def resource_server():
    _ResourceHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResourceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_ai_test_uses_bounded_openai_request(resource_server):
    result = onboarding_resources.test_ai_service(resource_server, "model-a", "sentinel-key", allow_loopback=True)
    assert result["ok"] is True
    assert result["model"] == "model-a"
    assert _ResourceHandler.requests[-1][0] == "/chat/completions"
    assert _ResourceHandler.requests[-1][2]["Authorization"] == "Bearer sentinel-key"


def test_asr_test_sends_in_memory_wav(resource_server):
    _ResourceHandler.response_payload = {"text": ""}
    result = onboarding_resources.test_asr_service(resource_server, "whisper-test", "sentinel-key", allow_loopback=True)
    assert result["ok"] is True
    path, body, headers = _ResourceHandler.requests[-1]
    assert path == "/audio/transcriptions"
    assert b"venus-m1-check.wav" in body
    assert b"whisper-test" in body
    assert headers["Authorization"] == "Bearer sentinel-key"


def test_resource_error_classification_and_url_policy(resource_server):
    _ResourceHandler.status = 401
    result = onboarding_resources.test_ai_service(resource_server, "m", "k", allow_loopback=True)
    assert result["error_code"] == "ai_auth_failed"
    assert onboarding_resources.test_ai_service("http://example.test", "m", "k")["error_code"] == "invalid_api_base"
    _ResourceHandler.status = 200


def test_write_env_secret_is_atomic_and_preserves_other_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=value\nKEY=old\n", encoding="utf-8")
    onboarding_resources.write_env_secret(env_path, "KEY", "new")
    assert env_path.read_text(encoding="utf-8") == "KEEP=value\nKEY=new\n"
    assert env_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(onboarding_resources.ResourceError):
        onboarding_resources.write_env_secret(env_path, "KEY", "bad\nvalue")


def test_recommended_model_is_unique_balanced_entry():
    entry = asr_models.recommended_model()
    assert entry["tier"] == "balanced"
    assert entry["recommended"] is True
    assert sum(1 for item in asr_models.REGISTRY if item.get("tier") == "balanced" and item.get("recommended")) == 1


def _coordinator(tmp_path, monkeypatch):
    source = tmp_path / "recordings"
    source.mkdir()
    settings = Settings(cheap_model_api_key="ai-secret", cheap_model_name="ai-model", asr_model="local-model")
    monkeypatch.setattr(asr_models, "local_path_for", lambda _model: tmp_path / "installed")
    return OnboardingCoordinator(
        service_dir=tmp_path / "service",
        config_path=tmp_path / "live-clipper.toml",
        input_dir=tmp_path / "input",
        output_root=tmp_path / "output",
        settings_loader=lambda: settings,
    ), source


def test_first_run_api_session_and_finish_is_idempotent(tmp_path, monkeypatch):
    coordinator, source = _coordinator(tmp_path, monkeypatch)
    assert coordinator.snapshot()["entry"]["onboarding"] == "new"
    status, started = coordinator.start({})
    assert status == 201
    assert started["session"]["revision"] == 1
    status, patched = coordinator.patch_session({"request_id": "draft-1", "expected_revision": 1, "current_step": "project", "patch": {"project": {"name": "第一个项目", "source_directory": str(source), "output_directory": str(tmp_path / "output"), "trigger_mode": "manual"}}})
    assert status == 200
    monkeypatch.setattr(onboarding_resources, "test_ai_service", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True, "ready": True})
    status, finished = coordinator.finish({"request_id": "finish-1", "expected_revision": patched["session"]["revision"]})
    assert status == 201
    assert finished["session"]["state"] == "completed"
    assert finished["project"]["activation_state"] == "active"
    assert (tmp_path / "output").is_dir()
    project_id = finished["project"]["project_id"]
    status, repeated = coordinator.finish({"request_id": "finish-1", "expected_revision": finished["session"]["revision"]})
    assert status == 200
    assert repeated["reused"] is True
    assert repeated["project"]["project_id"] == project_id


def test_finish_service_failure_is_activation_pending_then_retry(tmp_path, monkeypatch):
    coordinator, source = _coordinator(tmp_path, monkeypatch)
    coordinator.start({})
    _status, patched = coordinator.patch_session({"request_id": "draft-1", "expected_revision": 1, "current_step": "project", "patch": {"project": {"name": "项目", "source_directory": str(source), "output_directory": str(tmp_path / "out"), "trigger_mode": "manual"}}})
    monkeypatch.setattr(onboarding_resources, "test_ai_service", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": False, "error_code": "service_not_ready", "message": "服务未启动"})
    status, pending = coordinator.finish({"request_id": "finish-1", "expected_revision": patched["session"]["revision"]})
    assert status == 202
    assert pending["session"]["state"] == "activation_pending"
    assert pending["project"]["project_id"]
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True, "ready": True})
    status, completed = coordinator.retry({"request_id": "finish-1", "expected_revision": pending["session"]["revision"]})
    assert status == 200
    assert completed["session"]["state"] == "completed"


def test_project_validation_reports_creatable_output_without_creating_it(tmp_path, monkeypatch):
    coordinator, source = _coordinator(tmp_path, monkeypatch)
    coordinator.start({})
    _status, patched = coordinator.patch_session({"request_id": "draft-1", "expected_revision": 1, "current_step": "project", "patch": {"project": {"name": "项目", "source_directory": str(source), "output_directory": str(tmp_path / "new-output"), "trigger_mode": "manual"}}})
    status, result = coordinator.validate_project({"request_id": "validate-1", "expected_revision": patched["session"]["revision"]})
    assert status == 200
    assert result["checks"]["output_directory"]["status"] == "creatable"
    assert not (tmp_path / "new-output").exists()
