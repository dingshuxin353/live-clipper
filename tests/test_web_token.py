from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from live_clipper.web import LiveClipperRequestHandler, WebPaths


@pytest.fixture()
def token_server(tmp_path):
    paths = WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "live-clipper.toml",
    )
    handler = type(
        "TokenHandler",
        (LiveClipperRequestHandler,),
        {"paths": paths, "access_token": "secret-token-123"},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_api_rejects_missing_token(token_server):
    status, payload = _get(f"{token_server}/api/onboarding")
    assert status == 401
    assert payload["error_code"] == "unauthorized"


def test_api_accepts_bearer_token(token_server):
    status, payload = _get(
        f"{token_server}/api/onboarding",
        headers={"Authorization": "Bearer secret-token-123"},
    )
    assert status == 200
    assert payload["ok"] is True


def test_api_accepts_cookie_token(token_server):
    status, payload = _get(
        f"{token_server}/api/onboarding",
        headers={"Cookie": "lc_token=secret-token-123"},
    )
    assert status == 200
    assert payload["ok"] is True


def test_api_rejects_wrong_token(token_server):
    status, _payload = _get(
        f"{token_server}/api/onboarding",
        headers={"Authorization": "Bearer wrong"},
    )
    assert status == 401


def test_static_shell_is_served_without_token(token_server):
    request = urllib.request.Request(f"{token_server}/")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_no_token_configured_keeps_api_open(tmp_path):
    paths = WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "live-clipper.toml",
    )
    handler = type("OpenHandler", (LiveClipperRequestHandler,), {"paths": paths, "access_token": None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _get(f"http://127.0.0.1:{server.server_address[1]}/api/onboarding")
        assert status == 200
        assert payload["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
