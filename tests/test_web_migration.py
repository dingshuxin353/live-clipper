from __future__ import annotations

import json
import time
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from live_clipper import migration_coordinator, service
from live_clipper.web import LiveClipperRequestHandler, WebPaths, handle_api_request


def _paths(tmp_path):
    service = tmp_path / "work" / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    service.mkdir(parents=True)
    source.mkdir()
    output.mkdir()
    config = tmp_path / "live-clipper.toml"
    config.write_text(
        f'''[recording_source.default]
source_dir = "{source}"
output_root = "{output}"
[asr]
model = "asr"
api_key = "secret-asr"
[llm]
model = "ai"
api_key = "secret-ai"
''',
        encoding="utf-8",
    )
    (service / "runs.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
    return WebPaths(service_dir=service, config_path=config, input_dir=tmp_path / "input", output_root=output)


def test_migration_http_contract_and_backup_action_auth(tmp_path):
    paths = _paths(tmp_path)
    status, _headers, current = handle_api_request("GET", "/api/migration", paths)
    assert status == 200 and current["entry"] == "review"
    status, _headers, inspected = handle_api_request("POST", "/api/migration/inspect", paths, {})
    assert status == 200 and inspected["plan"]["plan_version"] == 3
    plan = inspected["plan"]
    status, _headers, validated = handle_api_request(
        "POST",
        "/api/migration/validate",
        paths,
        {
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": {"trigger_mode": "manual"},
        },
    )
    assert status == 200 and validated["plan"]["choices"]["trigger_mode"] == "manual"
    status, _headers, denied = handle_api_request(
        "GET", "/api/migration/not-real/backup-action", paths, auth_context="browser"
    )
    assert status == 403 and denied["error_code"] == "bearer_required"


def test_migration_rejects_unknown_body_fields(tmp_path):
    paths = _paths(tmp_path)
    status, _headers, payload = handle_api_request(
        "POST", "/api/migration/inspect", paths, {"project_id": "forged"}
    )
    assert status == 422 and payload["error_code"] == "validation_failed"


def test_api_response_disconnect_is_silent_transport_completion():
    class DisconnectedStream:
        def write(self, _body):
            raise BrokenPipeError("client closed")

    handler = object.__new__(LiveClipperRequestHandler)
    handler.wfile = DisconnectedStream()
    handler.close_connection = False
    handler._write_response_body(b'{"ok":true}')
    assert handler.close_connection is True


def test_restricted_real_http_executes_then_switches_to_project_api(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(migration_coordinator.shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    monkeypatch.setattr(
        migration_coordinator.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    class Handler(LiveClipperRequestHandler):
        access_token = "migration-token"
        restricted_startup = "migration_required"

    Handler.paths = paths
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    bearer = {"Authorization": "Bearer migration-token", "Content-Type": "application/json"}

    def request(method, route, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        response = urlopen(Request(base + route, method=method, data=data, headers=headers or bearer), timeout=5)
        return response.status, json.loads(response.read())

    try:
        _, inspected = request("POST", "/api/migration/inspect", {})
        plan = inspected["plan"]
        _, validated = request(
            "POST",
            "/api/migration/validate",
            {
                "source_fingerprint": plan["source_fingerprint"],
                "plan_hash": plan["plan_hash"],
                "choices": {"trigger_mode": "manual"},
            },
        )
        plan = validated["plan"]
        status, accepted = request(
            "POST",
            "/api/migration/execute",
            {
                "request_id": "http-execute-1",
                "source_fingerprint": plan["source_fingerprint"],
                "plan_hash": plan["plan_hash"],
                "choices": plan["choices"],
            },
        )
        assert status == 202
        for _ in range(200):
            _, current = request("GET", "/api/migration")
            if current["entry"] == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("HTTP migration did not complete")
        migration_id = accepted["session"]["migration_id"]
        with HTTPErrorContext(403):
            request(
                "GET",
                f"/api/migration/{migration_id}/backup-action",
                headers={"Cookie": "lc_token=migration-token"},
            )
        action_status, action = request("GET", f"/api/migration/{migration_id}/backup-action")
        assert action_status == 200 and action == {
            "ok": True,
            "action": "reveal_backup",
            "migration_id": migration_id,
        }
        studio_status, studio = request("GET", "/api/studio")
        assert studio_status == 200 and studio["ok"] is True and len(studio["projects"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class HTTPErrorContext:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, _traceback):
        assert error_type is HTTPError and error.code == self.status
        return True
