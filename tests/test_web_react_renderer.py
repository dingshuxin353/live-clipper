from __future__ import annotations

import http.client
import re
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

from live_clipper.web import LiveClipperRequestHandler, WebPaths, _static_path

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "live_clipper" / "web_static"
REACT_DIR = STATIC_DIR / "react"


@contextmanager
def _server(tmp_path):
    handler = type(
        "TestReactRendererHandler",
        (LiveClipperRequestHandler,),
        {"paths": WebPaths(config_path=tmp_path / "live-clipper.toml"), "access_token": None},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(port: int, method: str, path: str):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


def test_root_serves_only_react_index_and_assets(tmp_path):
    index = (REACT_DIR / "index.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="([^"]+)"', index)

    assert references
    assert all(reference.startswith("/static/react/") for reference in references)
    assert not re.search(r"https?://", index)
    assert not re.search(r"app\.js|onboarding\.js|styles\.css", index)

    with _server(tmp_path) as port:
        status, headers, body = _request(port, "GET", "/")
        assert status == 200
        assert body == (REACT_DIR / "index.html").read_bytes()
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Type"].startswith("text/html")

        for reference in references:
            asset_status, asset_headers, asset_body = _request(port, "GET", reference)
            assert asset_status == 200
            assert asset_body
            if reference.endswith(".js"):
                assert "javascript" in asset_headers["Content-Type"]
            if reference.endswith(".css"):
                assert asset_headers["Content-Type"].startswith("text/css")


def test_head_has_no_body_and_traversal_stays_rejected(tmp_path):
    with _server(tmp_path) as port:
        status, headers, body = _request(port, "HEAD", "/")
        assert status == 200
        assert body == b""
        assert int(headers["Content-Length"]) == (REACT_DIR / "index.html").stat().st_size
        traversal_status, _headers, _body = _request(port, "GET", "/static/%2e%2e/web.py")
        assert traversal_status == 404

    assert _static_path("/static/../web.py") is None
    assert _static_path("/static/%2e%2e/web.py") is None


def test_core_workbench_deep_links_serve_the_react_entry(tmp_path):
    expected = (REACT_DIR / "index.html").read_bytes()
    deep_links = [
        "/studio",
        "/projects",
        "/projects/project-1",
        "/projects/project-1/runs/run-1",
        "/clips",
    ]

    with _server(tmp_path) as port:
        for path in deep_links:
            status, headers, body = _request(port, "GET", path)
            assert status == 200
            assert body == expected
            assert headers["Content-Type"].startswith("text/html")
            assert headers["Cache-Control"] == "no-store"


def test_production_build_is_flat_nonempty_and_secret_free():
    files = sorted(path for path in REACT_DIR.rglob("*") if path.is_file())
    relative = [path.relative_to(REACT_DIR) for path in files]

    assert any(path.name == "index.html" for path in relative)
    assert any(path.suffix == ".js" for path in relative)
    assert any(path.suffix == ".css" for path in relative)
    assert all(path.stat().st_size > 0 for path in files)
    assert all(len(path.parts) <= 2 for path in relative)
    assert all(path.parts[0] == "assets" for path in relative if path.name != "index.html")

    combined = b"\n".join(path.read_bytes() for path in files)
    for forbidden in [
        str(ROOT).encode(),
        b"node_modules",
        b"lc_token",
        b"Authorization",
        b"transcript_raw",
        b"/api/onboarding/test-source",
        b"/api/onboarding/test-llm",
        b"/api/onboarding/complete",
        b"/api/onboarding/skip",
        b"onboardingSkipDialog",
        b"showEnter",
        "Venus 没有初始化或修改这些数据。请等待迁移工具准备完成后再继续。".encode(),
    ]:
        assert forbidden not in combined

    for required in [
        "检查现有内容，准备升级".encode(),
        "确认升级内容".encode(),
        "请保持 Venus 运行".encode(),
        "在 Finder 中显示备份".encode(),
        b"/api/migration/inspect",
        b"/api/migration/validate",
        b"/api/migration/execute",
        b"/api/migration/retry",
        b"/api/migration/acknowledge",
    ]:
        assert required in combined


def test_legacy_renderer_runtime_is_absent():
    for name in ["index.html", "app.js", "onboarding.js", "styles.css"]:
        assert not (STATIC_DIR / name).exists()
