from __future__ import annotations

import json
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from live_clipper import cli, service, web
from live_clipper.project_storage import ProjectRepository, database_path
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


def test_migration_http_contract_and_backup_grant_auth(tmp_path):
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
        "GET", "/api/migration/not-real/backup-grant", paths, auth_context="browser"
    )
    assert status == 403 and denied["error_code"] == "bearer_required"
    status, _headers, invalid = handle_api_request(
        "GET", "/api/migration/%2Funsafe/backup-grant", paths, auth_context="bearer"
    )
    assert status == 404 and invalid["error_code"] == "backup_not_available"
    status, _headers, retired = handle_api_request(
        "GET", "/api/migration/not-real/backup-action", paths, auth_context="bearer"
    )
    assert status == 404 and retired["error_code"] == "route_not_found"


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
    handler.send_response = lambda _status: None
    handler.send_header = lambda _key, _value: None
    handler.end_headers = lambda: None
    handler._emit_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
    assert handler.close_connection is True


def test_api_response_header_disconnect_is_silent_transport_completion():
    handler = object.__new__(LiveClipperRequestHandler)
    handler.close_connection = False
    handler.send_response = lambda _status: None
    handler.send_header = lambda _key, _value: None
    handler.end_headers = lambda: (_ for _ in ()).throw(BrokenPipeError("client reset"))
    handler._emit_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
    assert handler.close_connection is True


def test_restricted_real_http_executes_then_switches_to_project_api(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True})

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
        _, startup = request("GET", "/api/onboarding")
        assert startup["entry"]["mode"] == "workbench"
        assert startup["migration"]["entry"] == "completed"
        assert startup["migration"]["session"]["migration_id"] == migration_id
        assert startup["migration"]["report"]["acknowledged_at"] is None
        with HTTPErrorContext(403):
            request(
                "GET",
                f"/api/migration/{migration_id}/backup-grant",
                headers={"Cookie": "lc_token=migration-token"},
            )
        grant_status, grant = request("GET", f"/api/migration/{migration_id}/backup-grant")
        assert grant_status == 200 and grant == {
            "ok": True,
            "grant": {
                "grant_version": 1,
                "kind": "migration_backup_reveal",
                "migration_id": migration_id,
                "backup_path": str(paths.service_dir.parent / "migration-backups" / migration_id),
            },
        }
        with HTTPErrorContext(404):
            request("GET", f"/api/migration/{migration_id}/backup-action")
        assert "backup_path" not in json.dumps(current)
        assert "backup_path" not in json.dumps(startup)
        _, acknowledged = request(
            "POST",
            "/api/migration/acknowledge",
            {
                "request_id": "acknowledge-http-1",
                "migration_id": migration_id,
                "expected_revision": current["session"]["revision"],
            },
        )
        assert acknowledged["project_id"] == current["session"]["project_id"]
        _, startup_after_acknowledge = request("GET", "/api/onboarding")
        assert startup_after_acknowledge["entry"]["mode"] == "workbench"
        assert startup_after_acknowledge["migration"] is None
        studio_status, studio = request("GET", "/api/studio")
        assert studio_status == 200 and studio["ok"] is True and len(studio["projects"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_recheck_closes_recovered_migration_readiness_issue_without_new_work(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "synthetic-ready-ai-key")
    service_ready = {"value": False}
    monkeypatch.setattr(
        service,
        "ensure_service_ready",
        lambda *args, **kwargs: (
            {"ok": True}
            if service_ready["value"]
            else {"ok": False, "error_code": "service_not_ready"}
        ),
    )

    class Handler(LiveClipperRequestHandler):
        access_token = "migration-repair-token"
        restricted_startup = "migration_required"

    Handler.paths = paths
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    bearer = {"Authorization": "Bearer migration-repair-token", "Content-Type": "application/json"}

    def request(method, route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        try:
            response = urlopen(Request(base + route, method=method, data=data, headers=bearer), timeout=5)
        except HTTPError as error:
            return error.code, json.loads(error.read())
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
                "request_id": "http-attention-execute",
                "source_fingerprint": plan["source_fingerprint"],
                "plan_hash": plan["plan_hash"],
                "choices": plan["choices"],
            },
        )
        assert status == 202
        for _ in range(200):
            _, current = request("GET", "/api/migration")
            if current["session"] and current["session"]["state"] == "completed_attention":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("HTTP migration did not reach completed_attention")

        migration_id = accepted["session"]["migration_id"]
        project_id = current["session"]["project_id"]
        _, acknowledged = request(
            "POST",
            "/api/migration/acknowledge",
            {
                "request_id": "http-attention-acknowledge",
                "migration_id": migration_id,
                "expected_revision": current["session"]["revision"],
            },
        )
        assert acknowledged["project_id"] == project_id

        with ProjectRepository(paths.service_dir) as repository:
            issue = repository.list_issues(project_id=project_id, active_only=True)[0]
            run_ids = [run.run_id for run in repository.list_runs(project_id=project_id)]
            scan_ids = [scan.scan_id for scan in repository.list_scan_events(project_id)]

        from resource_test_support import assign_test_resources
        with ProjectRepository(paths.service_dir) as repository:
            revision = repository.get_config_revision(project_id)
            repository.add_config_revision(project_id, assign_test_resources(repository, revision.config), expected_revision=revision.revision)
        service_ready["value"] = True
        enable_status, enabled = request(
            "POST",
            f"/api/projects/{project_id}/enable",
            {"request_id": "http-enable-after-repair"},
        )
        assert enable_status == 200, enabled
        assert enabled["project"]["activation_state"] == "active"
        assert enabled["project"]["runtime"]["readiness_state"] == "ready"
        assert enabled["initial_scan"] is None

        recheck_status, rechecked = request(
            "POST",
            f"/api/issue-groups/{issue.issue_group_key}/recheck",
            {
                "request_id": "http-recheck-after-repair",
                "issue_revisions": {issue.issue_id: issue.issue_revision},
            },
        )
        assert recheck_status == 200
        assert rechecked["issues"][0]["issue_id"] == issue.issue_id
        assert rechecked["issues"][0]["status"] == "resolved"

        with ProjectRepository(paths.service_dir) as repository:
            project = repository.get_project(project_id)
            runtime = repository.get_runtime(project_id)
            assert project is not None and project.activation_state == "active"
            assert runtime is not None and runtime.readiness_state == "ready"
            assert runtime.auto_scan_state == "off" and runtime.failure_code is None
            assert repository.list_issues(project_id=project_id, active_only=True) == []
            assert [item.project_id for item in repository.list_projects()] == [project_id]
            assert [run.run_id for run in repository.list_runs(project_id=project_id)] == run_ids
            assert [scan.scan_id for scan in repository.list_scan_events(project_id)] == scan_ids
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


@contextmanager
def _incomplete_app(tmp_path, monkeypatch):
    from test_migration_coordinator import _incomplete_migration

    coordinator, service_dir = _incomplete_migration(tmp_path)
    monkeypatch.setenv('LIVE_CLIPPER_HOME', str(tmp_path))
    monkeypatch.setenv('LIVE_CLIPPER_WEB_TOKEN', 'migration-token')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setenv('HOME', str(runtime))
    ready = Event()
    servers, failures = [], []
    original_init = web._LocalHTTPServer.__init__

    def observed_init(server, *args, **kwargs):
        original_init(server, *args, **kwargs)
        servers.append(server)
        ready.set()

    monkeypatch.setattr(web._LocalHTTPServer, '__init__', observed_init)

    def run():
        try:
            cli.run_app(host='127.0.0.1', port=0)
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = Thread(target=run)
    thread.start()
    try:
        assert ready.wait(10) and servers, failures
        server = servers[0]
        base = f'http://127.0.0.1:{server.server_port}'

        def request(route, *, authenticated=True):
            headers = {'Authorization': 'Bearer migration-token'} if authenticated else {}
            started = time.monotonic()
            try:
                response = urlopen(Request(base + route, headers=headers), timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.code, json.load(response), time.monotonic() - started

        yield coordinator, service_dir, request
    finally:
        if servers:
            servers[0].shutdown()
        thread.join(10)
        assert not thread.is_alive() and not failures, failures
        if servers:
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(servers[0].server_address) != 0


def test_app_migration_reads_are_bounded_under_exclusive_lock_and_recover(tmp_path, monkeypatch):
    from test_first_run_detection import _fingerprint
    from test_startup_snapshot import _track_readers

    observations = []
    with _incomplete_app(tmp_path, monkeypatch) as (_coordinator, service_dir, request):
        path = database_path(service_dir)
        before = _fingerprint(tmp_path)
        with closing(sqlite3.connect(path, isolation_level=None)) as writer:
            contents = tuple(writer.iterdump())
            readers = _track_readers(monkeypatch)
            assert request('/api/migration', authenticated=False)[0] == 401
            assert not readers
            routes = ['/api/onboarding', '/api/migration', '/api/migration/migration-1/history']
            for phase in ('normal', 'locked', 'recovered'):
                if phase == 'locked':
                    writer.execute('BEGIN EXCLUSIVE')
                    with closing(sqlite3.connect(path, timeout=0)) as control:
                        try:
                            control.execute('SELECT rootpage FROM sqlite_master LIMIT 1')
                        except sqlite3.OperationalError as error:
                            assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                        else:
                            raise AssertionError('Exclusive transaction did not block a real read')
                elif phase == 'recovered':
                    writer.execute('ROLLBACK')
                for route in routes:
                    count = len(readers)
                    status, payload, elapsed = request(route)
                    observations.append({'phase': phase, 'route': route, 'status': status,
                                         'payload': payload, 'elapsed': elapsed, 'reads': len(readers) - count})
                    if phase == 'locked':
                        assert writer.in_transaction
                assert tuple(writer.iterdump()) == contents
            assert writer.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert readers and all(reader.cleared_before_close for reader in readers)
        assert _fingerprint(tmp_path) == before
    (tmp_path / 'http-lock-results.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2))
    for item in observations:
        assert item['elapsed'] < 2, item
        locked = item['phase'] == 'locked'
        history = item['route'].endswith('/history')
        assert item['reads'] == (2 if history and not locked else 1), item
        assert item['status'] == (409 if locked and item['route'] != '/api/onboarding' else 200), item
        payload = item['payload']
        if locked and item['route'] != '/api/onboarding':
            assert payload['error_code'] == 'diagnostic_required' and 'history' not in payload
        elif item['route'] == '/api/onboarding':
            assert payload['entry']['mode'] == ('diagnostic_required' if locked else 'migration_required')
        elif history:
            assert [row['run_id'] for row in payload['history']] == ['retained-run']
        else:
            assert payload['session']['project_id'] and payload['session']['state'] == 'validating'


@pytest.mark.parametrize('unreadable_first', [False, True])
def test_app_history_preserves_inspection_then_lock_order(tmp_path, monkeypatch, unreadable_first):
    from test_first_run_detection import _fingerprint
    from test_startup_snapshot import _track_readers

    with _incomplete_app(tmp_path, monkeypatch) as (_coordinator, service_dir, request):
        before = _fingerprint(tmp_path)
        inspected, resume = Event(), Event()
        inspections, errors = [], []
        original_inspection = web._startup_inspection
        original_history = web.migration_coordinator.MigrationCoordinator.history

        def inspect(paths):
            value = original_inspection(paths)
            inspections.append(value)
            inspected.set()
            assert resume.wait(5)
            return value

        def history(self, migration_id):
            try:
                return original_history(self, migration_id)
            except sqlite3.DatabaseError as error:
                errors.append(error)
                raise

        monkeypatch.setattr(web, '_startup_inspection', inspect)
        monkeypatch.setattr(web.migration_coordinator.MigrationCoordinator, 'history', history)
        with closing(sqlite3.connect(database_path(service_dir), isolation_level=None)) as writer:
            contents = tuple(writer.iterdump())
            if unreadable_first:
                writer.execute('BEGIN EXCLUSIVE')
            readers = _track_readers(monkeypatch)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(request, '/api/migration/migration-1/history')
                try:
                    assert inspected.wait(5)
                    assert inspections[0].facts.unreadable is unreadable_first
                    writer.execute('ROLLBACK' if unreadable_first else 'BEGIN EXCLUSIVE')
                    resume.set()
                    status, payload, elapsed = pending.result(timeout=5)
                    assert status == (409 if unreadable_first else 500)
                    assert payload['error_code'] == ('diagnostic_required' if unreadable_first else 'internal_error')
                    assert 'history' not in payload
                    assert len(readers) == (1 if unreadable_first else 2)
                    assert bool(errors) is not unreadable_first
                    assert writer.in_transaction is not unreadable_first
                finally:
                    resume.set()
                    if writer.in_transaction:
                        writer.execute('ROLLBACK')
            count = len(readers)
            recovered_status, recovered, _ = request('/api/migration/migration-1/history')
            assert recovered_status == 200 and recovered['history'][0]['run_id'] == 'retained-run'
            assert len(readers) - count == 2 and not inspections[-1].facts.unreadable
            assert tuple(writer.iterdump()) == contents
            assert all(reader.cleared_before_close for reader in readers)
        assert _fingerprint(tmp_path) == before
    (tmp_path / 'http-lock-order.json').write_text(json.dumps({
        'unreadable_first': unreadable_first, 'status': status, 'code': payload['error_code'],
        'elapsed_including_event_pause': elapsed, 'database_errors': [type(error).__name__ for error in errors],
        'recovered_status': recovered_status,
    }, indent=2))
