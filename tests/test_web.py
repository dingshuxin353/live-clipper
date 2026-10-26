from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_project_result_api_v2 import result_api_fixture

from live_clipper import web
from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import ProjectManager, open_project_repository
from live_clipper.utils import write_json
from live_clipper.web import LiveClipperRequestHandler, WebPaths, build_run_detail, build_runs_index, handle_api_request


@contextmanager
def _resource_server(directory, service_dir, *, setup='', restricted='', expect_ready=True):
    directory.mkdir(parents=True)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    for key, name in [('HOME', 'home'), ('TMPDIR', 'tmp'), ('XDG_CACHE_HOME', 'cache'), ('LIVE_CLIPPER_HOME', 'app-home')]:
        path = directory / name
        path.mkdir()
        env[key] = str(path)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from live_clipper.web import WebPaths, run_web_server
service_dir = Path(sys.argv[2])
exec(sys.argv[4])
run_web_server(port=int(sys.argv[3]), access_token='test-token', restricted_startup=sys.argv[5] or None,
    paths=WebPaths(service_dir=service_dir, config_path=Path('config.toml')))
'''
    process = subprocess.Popen([sys.executable, '-I', '-B', '-u', '-c', script, str(source), str(service_dir), str(port), setup, restricted],
        cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    events = {key: Event() for key in ('ready', 'download', 'validate')}
    lines = []

    def collect():
        for line in process.stdout:
            lines.append(line)
            for key, marker in [('ready', '[Web] Venus 控制台已启动:'), ('download', 'TEST-DOWNLOAD'), ('validate', 'TEST-VALIDATE')]:
                if marker in line:
                    events[key].set()

    reader = Thread(target=collect, daemon=True)
    reader.start()
    try:
        if expect_ready:
            assert events['ready'].wait(10), ''.join(lines)
        yield port, process, events
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        reader.join(5)
        process.stdout.close()
        (directory / 'backend.log').write_text(''.join(lines))
        assert not reader.is_alive()


def _resource_request(port, path, *, method='GET', body=None):
    request = Request(f'http://127.0.0.1:{port}{path}', method=method,
        headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def _filesystem_facts(root):
    facts = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        facts[str(path.relative_to(root))] = (
            path.is_dir(),
            stat.st_mode,
            stat.st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
    return facts


def test_restricted_startup_web_allows_only_shell_and_onboarding_without_writes(tmp_path, monkeypatch):
    home = tmp_path / "legacy-home"
    service_dir = home / "work" / "service"
    service_dir.mkdir(parents=True)
    config_path = home / "live-clipper.toml"
    config_path.write_text("[recording_source.default]\nsource_dir = '/Volumes/legacy'\n", encoding="utf-8")
    (service_dir / "jobs.json").write_text(
        '[{"id":"legacy-job","status":"running"}]\n',
        encoding="utf-8",
    )

    class RestrictedHandler(LiveClipperRequestHandler):
        paths = WebPaths(
            output_root=home / "output",
            state_dir=home / "work" / "automation_state",
            log_dir=home / "work" / "automation_logs",
            input_dir=home / "input",
            service_dir=service_dir,
            config_path=config_path,
        )
        access_token = "test-token"
        restricted_startup = "migration_required"

    from live_clipper.onboarding_coordinator import OnboardingCoordinator

    def forbidden(*args, **kwargs):
        pytest.fail('restricted onboarding accessed settings, resources, environment or recovery')

    for method in ('settings', '_resource_summary', '_environment_summary', '_repo'):
        monkeypatch.setattr(OnboardingCoordinator, method, forbidden)
    before = _filesystem_facts(home)
    server = ThreadingHTTPServer(("127.0.0.1", 0), RestrictedHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Authorization": "Bearer test-token"}
    try:
        shell = urlopen(f"{base}/studio", timeout=5)
        assert shell.status == 200 and b'<div id="root"></div>' in shell.read()
        onboarding_response = urlopen(Request(f"{base}/api/onboarding", headers=headers), timeout=5)
        onboarding_payload = json.loads(onboarding_response.read())
        assert onboarding_payload["entry"]["mode"] == "migration_required"
        assert onboarding_payload["session"] is None
        migration_response = urlopen(Request(f"{base}/api/migration", headers=headers), timeout=5)
        migration_payload = json.loads(migration_response.read())
        assert migration_response.status == 200
        assert migration_payload["entry"] in {"inspect", "review"}
        assert migration_payload["source"]["detected"] is True

        for method, route, data in [
            ("GET", "/api/studio", None),
            ("GET", "/api/config", None),
            ("POST", "/api/service/start", b"{}"),
            ("POST", "/api/onboarding/start", b"{}"),
        ]:
            request = Request(f"{base}{route}", headers=headers, method=method, data=data)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            with pytest.raises(HTTPError) as blocked:
                urlopen(request, timeout=5)
            payload = json.loads(blocked.value.read())
            assert blocked.value.code == 409
            assert payload["error_code"] == "migration_required"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert _filesystem_facts(home) == before


def test_run_web_server_skips_job_recovery_in_restricted_startup(tmp_path, monkeypatch):
    calls = []

    class FakeServer:
        def __init__(self, address, handler):
            calls.append((address, handler.restricted_startup))

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(web, "_LocalHTTPServer", FakeServer)
    monkeypatch.setattr(web.jobs, "sweep_interrupted", lambda service_dir: pytest.fail("restricted startup must not sweep jobs"))
    web.run_web_server(
        port=43210,
        paths=WebPaths(service_dir=tmp_path / "service", config_path=tmp_path / "live-clipper.toml"),
        access_token="test-token",
        restricted_startup="diagnostic_required",
    )

    assert calls == [(('127.0.0.1', 43210), "diagnostic_required")]


def test_normal_startup_recovers_real_interruption_after_partial_recovery_failure(tmp_path):
    from live_clipper.project_storage import ProjectRepository

    service_dir = tmp_path / 'service'
    model = 'mlx-community/whisper-large-v3-turbo'
    setup = '''from threading import Event
from live_clipper import asr_models, transcribe
entry = dict(asr_models.model_entry('mlx-community/whisper-large-v3-turbo'))
entry['files'] = [{'path': 'config.json', 'bytes': 1, 'sha256': 'unused-before-interruption'}]
asr_models.REGISTRY = [entry]
def record_call(marker):
    with (service_dir / 'external-calls.txt').open('a') as stream:
        stream.write(marker + '\\n')
    print(marker, flush=True)
def download(**kwargs):
    record_call('TEST-DOWNLOAD')
    Event().wait()
def external_asr(*args):
    if args[-1].asr.model == 'completed':
        return {'segments': [{'start': 0, 'end': 1, 'text': '测试'}]}
    record_call('TEST-VALIDATE')
    Event().wait()
asr_models.hf_hub_download = download
transcribe.transcribe_audio = external_asr
'''
    cloud = {'name': '云端检查', 'kind': 'cloud_asr', 'config': {
        'model': 'blocked', 'endpoint': 'https://example.test/v1', 'purposes': ['asr']}}
    check = {'proposal': cloud, 'credential': 'test-only', 'purposes': ['asr'], 'request_id': 'interrupted-check'}
    responses = []
    with _resource_server(tmp_path / 'first', service_dir, setup=setup) as (port, process, events):
        completed = {**check, 'request_id': 'completed-check', 'proposal': {**cloud, 'config': {**cloud['config'], 'model': 'completed'}}}
        assert _resource_request(port, '/api/resources/validate', method='POST', body=completed)[0] == 200
        status, payload = _resource_request(port, '/api/resources', method='POST', body={'request_id': 'local', 'proposal': {
            'name': '本机模型', 'kind': 'local_asr', 'config': {'model': model, 'model_source': 'huggingface', 'purposes': ['asr']}}})
        assert status == 201
        identifier = payload['resource']['resource_id']
        status, payload = _resource_request(port, f'/api/resources/{identifier}/prepare', method='POST', body={'expected_revision': 1})
        assert status == 202
        job_id = payload['job']['id']

        def validate():
            try:
                responses.append(_resource_request(port, '/api/resources/validate', method='POST', body=check))
            except OSError:
                responses.append('connection-terminated')

        request_thread = Thread(target=validate)
        request_thread.start()
        try:
            assert events['download'].wait(5) and events['validate'].wait(5)
            with ProjectRepository(service_dir) as repo:
                assert repo.connection.execute("SELECT count(*) FROM resource_tasks WHERE state='running'").fetchone()[0] == 2
                finished_before = repo.connection.execute("SELECT * FROM resource_tasks WHERE state='finished'").fetchall()
            process.kill()
            assert process.wait(5) != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(5)
            request_thread.join(5)
        assert not request_thread.is_alive() and responses == ['connection-terminated']
    before_calls = (service_dir / 'external-calls.txt').read_bytes()
    with ProjectRepository(service_dir) as repo:
        repo.connection.execute("CREATE TRIGGER recovery_failure BEFORE UPDATE OF state ON resource_tasks WHEN NEW.state='interrupted' BEGIN SELECT RAISE(ABORT,'controlled recovery failure'); END")
    with _resource_server(tmp_path / 'failed-recovery', service_dir, setup=setup, expect_ready=False) as (port, process, events):
        assert process.wait(10) != 0 and not events['ready'].is_set()
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0
    assert json.loads((service_dir / 'jobs.json').read_text())['jobs'][0]['status'] == 'interrupted'
    with ProjectRepository(service_dir) as repo:
        assert repo.connection.execute("SELECT count(*) FROM resource_tasks WHERE state='running'").fetchone()[0] == 2
        repo.connection.execute('DROP TRIGGER recovery_failure')
    with _resource_server(tmp_path / 'recovered', service_dir, setup=setup) as (port, _, _events):
        status, payload = _resource_request(port, '/api/resources/validate', method='POST', body=check)
        assert status == 409 and payload['error']['code'] == 'validation_result_unknown'
        assert _resource_request(port, f'/api/resources/{identifier}/delete-preview')[1]['preview']['can_delete']
        with ProjectRepository(service_dir) as repo:
            assert repo.connection.execute("SELECT count(*) FROM resource_tasks WHERE state='interrupted'").fetchone()[0] == 2
            assert repo.connection.execute("SELECT * FROM resource_tasks WHERE state='finished'").fetchall() == finished_before
            tasks_after = repo.connection.execute('SELECT * FROM resource_tasks ORDER BY rowid').fetchall()
            operations_after = repo.connection.execute('SELECT * FROM resource_operations ORDER BY rowid').fetchall()
        jobs_after = (service_dir / 'jobs.json').read_bytes()
    with _resource_server(tmp_path / 'repeated', service_dir, setup=setup):
        with ProjectRepository(service_dir) as repo:
            assert repo.connection.execute('SELECT * FROM resource_tasks ORDER BY rowid').fetchall() == tasks_after
            assert repo.connection.execute('SELECT * FROM resource_operations ORDER BY rowid').fetchall() == operations_after
        assert (service_dir / 'jobs.json').read_bytes() == jobs_after
    assert (service_dir / 'external-calls.txt').read_bytes() == before_calls
    assert json.loads(jobs_after)['jobs'][0]['id'] == job_id


@pytest.mark.parametrize('restricted', ['', 'diagnostic_required'])
def test_startup_without_database_preserves_mode_specific_job_recovery(tmp_path, restricted):
    service_dir = tmp_path / 'service'
    jobs_path = service_dir / 'jobs.json'
    write_json(jobs_path, {'jobs': [{'id': 'non-resource', 'kind': 'ai_review', 'status': 'running'},
        {'id': 'finished', 'status': 'succeeded', 'result': {'ok': True}}]})
    before = jobs_path.read_bytes()
    with _resource_server(tmp_path / 'startup', service_dir, restricted=restricted):
        assert not (service_dir / 'venus.sqlite3').exists()
        if restricted:
            assert jobs_path.read_bytes() == before
        else:
            records = json.loads(jobs_path.read_text())['jobs']
            assert records[0]['status'] == 'interrupted'
            assert records[1] == {'id': 'finished', 'status': 'succeeded', 'result': {'ok': True}}


def test_build_runs_index_merges_files_state_and_log(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    run_dir = output_root / "recording"
    log_path = log_dir / "recording.log"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", {"candidates": [{"id": "c1"}]})
    write_json(run_dir / "review_brief.json", {"candidates": [{"id": "c1"}]})
    write_json(state_dir / "recording.json", {"pid": 4321, "log_path": str(log_path)})
    log_path.parent.mkdir(parents=True)
    log_path.write_text("first\nsecond\n", encoding="utf-8")
    monkeypatch.setattr("live_clipper.web._pid_is_running", lambda pid: True)

    index = build_runs_index(WebPaths(output_root=output_root, state_dir=state_dir, log_dir=log_dir, service_dir=tmp_path / "service"))

    assert index["ok"] is True
    assert index["runs"][0]["run_id"] == "recording"
    assert index["runs"][0]["running"] is True
    assert index["runs"][0]["requires_review"] is True
    assert index["runs"][0]["candidate_count"] == 1
    assert index["runs"][0]["selected_count"] == 0
    assert index["runs"][0]["clip_count"] == 0
    assert index["runs"][0]["log_path"] == str(log_path)
    assert index["runs"][0]["phase"] == "needs_review_selection"


def test_build_run_detail_includes_steps_files_actions_and_log_tail(tmp_path):
    output_root = tmp_path / "output"
    log_dir = tmp_path / "logs"
    run_dir = output_root / "recording"
    log_path = log_dir / "recording.log"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", {"candidates": [{"id": "c1"}, {"id": "c2"}]})
    write_json(run_dir / "review_brief.json", {"candidates": [{"id": "c1"}, {"id": "c2"}]})
    log_path.parent.mkdir(parents=True)
    log_path.write_text("\n".join(f"line {index}" for index in range(5)), encoding="utf-8")

    detail = build_run_detail(
        "recording",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=log_dir),
        log_lines=2,
    )

    assert detail["ok"] is True
    assert detail["run"]["run_id"] == "recording"
    assert detail["steps"][3]["label"] == "AI 分析"
    assert detail["steps"][3]["done"] is True
    assert detail["steps"][5]["label"] == "审阅 Agent 选择"
    assert detail["steps"][5]["state"] == "waiting"
    assert detail["actions"]["can_render"] is False
    assert detail["actions"]["can_cleanup"] is False
    assert detail["log"]["tail"] == "line 3\nline 4"


def test_build_run_detail_lists_generated_clips_and_cleanup_targets(tmp_path):
    output_root = tmp_path / "output"
    input_dir = tmp_path / "input"
    run_dir = output_root / "recording"
    local_source = input_dir / "recording.mkv"
    original_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    original_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    original_source.write_bytes(b"nas video")
    (run_dir / "audio.wav").parent.mkdir(parents=True)
    (run_dir / "audio.wav").write_bytes(b"audio")
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "clip_01.mp4").write_bytes(b"clip one")
    write_json(run_dir / "run_metadata.json", {
        "source_name": "recording.mkv",
        "pipeline": {
            "local_source_path": str(local_source),
            "original_source_path": str(original_source),
        },
    })
    write_json(run_dir / "selected_clips.json", {"candidates": [{"id": "clip_01"}]})

    detail = build_run_detail(
        "recording",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs", input_dir=input_dir),
    )

    assert detail["clips"][0]["name"] == "clip_01.mp4"
    assert detail["clips"][0]["url"] == "/media/runs/recording/clips/clip_01.mp4"
    local_source_target = next(target for target in detail["cleanup"]["targets"] if target["kind"] == "local_source_video")
    assert local_source_target["deletable"] is True
    assert detail["actions"]["can_delete_local_source"] is True


def test_service_run_media_and_cleanup_use_saved_run_paths(tmp_path):
    service_dir = tmp_path / "service"
    workspace_dir = tmp_path / "workspace" / "runs" / "business-run"
    input_dir = workspace_dir / "input"
    run_dir = workspace_dir / "output"
    local_source = input_dir / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True)
    clip = clips_dir / "clip_01.mp4"
    clip.write_bytes(b"clip one")
    write_json(
        run_dir / "run_metadata.json",
        {
            "pipeline": {
                "local_source_path": str(local_source),
                "original_source_path": str(tmp_path / "nas" / "recording.mkv"),
            }
        },
    )
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                    {
                        "run_id": "business-run",
                        "run_dir": str(run_dir),
                        "input_dir": str(input_dir),
                        "local_source_path": str(local_source),
                        "log_path": str(service_dir / "runs" / "business-run.log"),
                        "phase": "rendered",
                    }
            ]
        },
    )
    paths = WebPaths(
        output_root=tmp_path / "legacy-output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "legacy-input",
        service_dir=service_dir,
        config_path=tmp_path / "missing.toml",
    )

    detail = build_run_detail("business-run", paths)

    assert detail["clips"][0]["url"] == "/media/runs/business-run/clips/clip_01.mp4"
    assert next(
        target for target in detail["cleanup"]["targets"] if target["kind"] == "local_source_video"
    )["path"] == str(local_source)
    assert web._media_clip_path("/media/runs/business-run/clips/clip_01.mp4", paths) == clip
    assert web._media_clip_path("/media/runs/business-run/clips/../secret.mp4", paths) is None


def test_handle_api_request_returns_json_payloads(tmp_path):
    output_root = tmp_path / "output"
    run_dir = output_root / "recording"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})

    status_code, headers, body = handle_api_request(
        "GET",
        "/api/runs",
        WebPaths(
            output_root=output_root,
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "logs",
            service_dir=tmp_path / "service",
        ),
    )

    assert status_code == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert body["ok"] is True
    assert body["runs"][0]["run_id"] == "recording"


def test_runs_api_filters_full_snapshot_before_pagination_and_reports_counts(tmp_path):
    service_dir = tmp_path / "service"
    historical = [
        {
            "run_id": f"rendered-{index}",
            "phase": "rendered",
            "run_dir": str(tmp_path / "output" / f"rendered-{index}"),
            "updated_at": f"2026-08-{index + 1:02d}T00:00:00+00:00",
        }
        for index in range(20)
    ]
    queued = [
        {
            "run_id": f"queued-{index}",
            "phase": "queued",
            "run_dir": str(tmp_path / "output" / f"queued-{index}"),
        }
        for index in range(57)
    ]
    queued.insert(
        20,
        {
            "run_id": "processing-hidden-after-first-page",
            "phase": "processing",
            "run_dir": str(tmp_path / "output" / "processing-hidden-after-first-page"),
        },
    )
    queued.insert(
        21,
        {
            "run_id": "failed-hidden-after-first-page",
            "phase": "failed",
            "run_dir": str(tmp_path / "output" / "failed-hidden-after-first-page"),
        },
    )
    write_json(service_dir / "runs.json", {"runs": historical + queued})
    paths = WebPaths(service_dir=service_dir, output_root=tmp_path / "output")

    first_status, _headers, first = handle_api_request(
        "GET", "/api/runs?phase=queued&offset=0&limit=20", paths
    )
    last_status, _headers, last = handle_api_request(
        "GET", "/api/runs?phase=queued&offset=40&limit=20", paths
    )
    processing_status, _headers, processing = handle_api_request(
        "GET", "/api/runs?phase=processing", paths
    )

    assert first_status == last_status == processing_status == 200
    assert first["total"] == 57
    assert first["count"] == 20
    assert first["has_more"] is True
    assert first["phase_counts"] == {
        "all": 79,
        "queued": 57,
        "processing": 1,
        "needs_review": 0,
        "rendered": 20,
        "failed": 1,
        "other": 0,
    }
    assert [run["queue_position"] for run in first["runs"]] == list(range(1, 21))
    assert [run["queue_position"] for run in last["runs"]] == list(range(41, 58))
    assert last["count"] == 17
    assert last["has_more"] is False
    assert processing["total"] == 1
    assert processing["runs"][0]["run_id"] == "processing-hidden-after-first-page"


def test_runs_api_uses_one_phase_group_contract_for_legacy_phases(tmp_path):
    service_dir = tmp_path / "service"
    phases = [
        "queued",
        "processing",
        "rendering",
        "running",
        "ready_to_render",
        "needs_review",
        "needs_review_selection",
        "rendered",
        "cleanup_ready",
        "failed",
        "failed_needs_review",
        "waiting_or_manual",
        "missing",
        "unknown",
        "future_phase",
    ]
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {"run_id": phase, "phase": phase, "run_dir": str(tmp_path / "output" / phase)}
                for phase in phases
            ]
        },
    )
    paths = WebPaths(service_dir=service_dir)

    status, _headers, all_runs = handle_api_request("GET", "/api/runs?limit=100", paths)
    review_status, _headers, review = handle_api_request("GET", "/api/runs?phase=needs_review", paths)
    failed_status, _headers, failed = handle_api_request("GET", "/api/runs?phase=failed", paths)

    assert status == review_status == failed_status == 200
    assert all_runs["phase_counts"] == {
        "all": 15,
        "queued": 1,
        "processing": 4,
        "needs_review": 2,
        "rendered": 2,
        "failed": 2,
        "other": 4,
    }
    assert {run["phase"] for run in review["runs"]} == {"needs_review", "needs_review_selection"}
    assert {run["phase"] for run in failed["runs"]} == {"failed", "failed_needs_review"}
    assert {run["run_id"] for run in all_runs["runs"]} == set(phases)


def test_runs_api_rejects_invalid_query_parameters_with_stable_error(tmp_path):
    paths = WebPaths(service_dir=tmp_path / "service")

    for query in [
        "phase=not-a-phase",
        "offset=-1",
        "offset=abc",
        "limit=0",
        "limit=101",
        "limit=1.5",
    ]:
        status, _headers, body = handle_api_request("GET", f"/api/runs?{query}", paths)

        assert status == 400
        assert body["ok"] is False
        assert body["error_code"] == "invalid_query_parameter"


def test_runs_api_applies_the_same_contract_to_legacy_output_fallback(tmp_path):
    output_root = tmp_path / "output"
    for index in range(25):
        run_dir = output_root / f"legacy-{index}"
        write_json(run_dir / "run_metadata.json", {"source_name": f"legacy-{index}.mkv"})
        write_json(run_dir / "review_brief.json", {"candidates": [{"id": f"clip-{index}"}]})

    status, _headers, payload = handle_api_request(
        "GET",
        "/api/runs?phase=needs_review&offset=20&limit=20",
        WebPaths(output_root=output_root, service_dir=tmp_path / "missing-service"),
    )

    assert status == 200
    assert payload["total"] == 25
    assert payload["count"] == 5
    assert payload["has_more"] is False
    assert payload["phase_counts"]["all"] == 25
    assert payload["phase_counts"]["needs_review"] == 25
    assert {run["phase"] for run in payload["runs"]} == {"needs_review_selection"}


def test_scan_and_retry_configuration_errors_use_http_409(tmp_path, monkeypatch):
    paths = WebPaths(
        output_root=tmp_path / "output",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "missing.toml",
    )
    run_dir = tmp_path / "workspace" / "runs" / "run-failed" / "output"
    write_json(paths.service_dir / "runs.json", {"runs": [{
        "run_id": "run-failed",
        "phase": "failed",
        "run_dir": str(run_dir),
        "input_dir": str(run_dir.parent / "input"),
        "source_path": str(tmp_path / "source.mkv"),
        "local_source_path": str(run_dir.parent / "input" / "source.mkv"),
        "last_error": "old error",
    }]})
    monkeypatch.setattr(web, "_settings_for_paths", lambda request_paths: web.Settings())

    scan_status, _headers, scan = handle_api_request("POST", "/api/service/scan-now", paths)
    retry_status, _headers, retry = handle_api_request("POST", "/api/runs/run-failed/retry", paths)

    assert scan_status == 409
    assert retry_status == 409
    assert scan["error_code"] == retry["error_code"] == "migration_required"
    assert "诊断或迁移" in scan["message"]


def test_empty_selection_disables_render_and_cleanup_and_returns_http_409(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "workspace" / "runs" / "run-empty" / "output"
    write_json(run_dir / "review_brief.json", {"candidates": []})
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "selected_clips.json", [])
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {
                    "run_id": "run-empty",
                    "phase": "needs_review",
                    "run_dir": str(run_dir),
                    "input_dir": str(run_dir.parent / "input"),
                    "source_path": str(tmp_path / "source.mkv"),
                    "local_source_path": None,
                }
            ]
        },
    )
    paths = WebPaths(service_dir=service_dir, config_path=tmp_path / "missing.toml")

    detail = build_run_detail("run-empty", paths)
    status, _headers, payload = handle_api_request("POST", "/api/runs/run-empty/render", paths)

    assert detail["actions"]["can_render"] is False
    assert detail["actions"]["can_cleanup_preview"] is False
    assert detail["actions"]["can_cleanup"] is False
    assert detail["actions"]["can_ai_review"] is False
    assert status == 409
    assert payload["error_code"] == "selection_empty"


def test_static_assets_disable_cache_and_serve_hashed_react_js(tmp_path):
    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(output_root=tmp_path / "output", state_dir=tmp_path / "state", log_dir=tmp_path / "logs")

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        index_response = urlopen(f"{base_url}/", timeout=5)
        html = index_response.read().decode("utf-8")
        assert index_response.headers["Cache-Control"] == "no-store"
        script_match = re.search(
            r'<script[^>]+src="(/static/react/assets/index-[A-Za-z0-9_-]+\.js)"[^>]*></script>',
            html,
        )
        assert script_match

        app_response = urlopen(f"{base_url}{script_match.group(1)}", timeout=5)
        assert app_response.status == 200
        assert "javascript" in app_response.headers["Content-Type"]
        assert app_response.headers["Cache-Control"] == "no-store"
        bundle = app_response.read()
        assert b"/api/studio" in bundle
        assert b"/api/projects" in bundle
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_handle_api_request_deletes_single_generated_clip(tmp_path):
    output_root = tmp_path / "output"
    run_dir = output_root / "recording"
    clip_path = run_dir / "clips" / "clip_01.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"clip")
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})

    status_code, _headers, body = handle_api_request(
        "POST",
        "/api/runs/recording/clips/clip_01.mp4/delete",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs"),
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["deleted"] == str(clip_path)
    assert not clip_path.exists()


def test_handle_api_request_deletes_only_local_source_copy(tmp_path):
    output_root = tmp_path / "output"
    input_dir = tmp_path / "input"
    run_dir = output_root / "recording"
    local_source = input_dir / "recording.mkv"
    nas_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    nas_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    nas_source.write_bytes(b"nas video")
    (run_dir / "clips").mkdir(parents=True)
    (run_dir / "clips" / "clip_01.mp4").write_bytes(b"clip")
    write_json(run_dir / "selected_clips.json", {"candidates": [{"id": "clip_01"}]})
    write_json(run_dir / "run_metadata.json", {
        "source_name": "recording.mkv",
        "pipeline": {
            "local_source_path": str(local_source),
            "original_source_path": str(nas_source),
        },
    })

    status_code, _headers, body = handle_api_request(
        "POST",
        "/api/runs/recording/delete-local-source",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs", input_dir=input_dir),
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["deleted"] == str(local_source)
    assert not local_source.exists()
    assert nas_source.exists()


def test_project_routes_use_sqlite_and_global_scan_is_blocked(tmp_path):
    service_dir = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    repository = open_project_repository(service_dir)
    project = ProjectManager(repository, Settings()).create_project(
        name="项目",
        config=default_project_config(source, output),
        activation_state="inactive",
        request_id="create-web-project",
    )
    repository.close()
    paths = WebPaths(service_dir=service_dir, config_path=tmp_path / "missing.toml")

    status, _headers, projects = handle_api_request("GET", "/api/projects", paths)
    run_status, _headers, missing_run = handle_api_request("GET", "/api/runs/not-found", paths)
    scan_status, _headers, scan = handle_api_request("POST", "/api/service/scan-now", paths)

    assert status == 200
    assert projects["projects"][0]["project_id"] == project.project_id
    assert run_status == 404 and missing_run["error"]["code"] == "run_not_found"
    assert scan_status == 409 and scan["error"]["code"] == "project_scope_required"
    assert not (service_dir / "runs.json").exists()


def test_spa_history_route_returns_react_index(tmp_path):
    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=tmp_path / "service")

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        root = urlopen(f"{base_url}/", timeout=5).read()
        deep = urlopen(f"{base_url}/projects/project-1/runs/run-1", timeout=5)
        assert deep.status == 200
        assert deep.headers["Content-Type"] == "text/html; charset=utf-8"
        assert deep.read() == root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_recent_project_creation_completes_initial_scan(tmp_path, monkeypatch):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    config = default_project_config(source, output)
    config["source"].update(first_scan_mode="recent", lookback_days=3)
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "fake")

    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=tmp_path / "service", config_path=tmp_path / "missing.toml")

    from resource_test_support import assign_test_resources
    with open_project_repository(TestHandler.paths.service_dir) as repository:
        config = assign_test_resources(repository, config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = Request(
        f"http://127.0.0.1:{server.server_address[1]}/api/projects",
        data=json.dumps(
            {
                "request_id": "http-recent-create",
                "activation_state": "active",
                "project": {"name": "recent", "description": "", "config": config},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=5)
        payload = json.loads(response.read())
        assert response.status == 201
        assert payload["initial_scan"]["status"] == "success"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    repository = open_project_repository(tmp_path / "service")
    scan = repository.list_scan_events(payload["project"]["project_id"])[0]
    assert scan.trigger_source == "manual"
    assert repository.get_runtime(payload["project"]["project_id"]).first_scan_state == "completed"
    repository.close()


def test_http_output_media_range_head_auth_and_416(tmp_path):
    repository, _api, _project, _run, _path, media = result_api_fixture(
        tmp_path,
        output_container="mov,mp4,m4a,3gp,3g2,mj2",
    )
    repository.close()
    config_path = tmp_path / "live-clipper.toml"
    config_path.write_text(
        f'[paths]\nwork_dir = "{tmp_path / "work"}"\noutput_root = "{tmp_path / "output"}"\n',
        encoding="utf-8",
    )

    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=tmp_path / "service", config_path=config_path)
        access_token = "test-token"

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/outputs/output-1/media"
    try:
        ranged = urlopen(Request(url, headers={"Authorization": "Bearer test-token", "Range": "bytes=2-5"}), timeout=5)
        assert ranged.status == 206 and ranged.read() == media[2:6]
        assert ranged.headers["Content-Type"] == "video/mp4"
        assert ranged.headers["Content-Range"] == f"bytes 2-5/{len(media)}"

        head = urlopen(Request(url, headers={"Authorization": "Bearer test-token"}, method="HEAD"), timeout=5)
        assert head.status == 200 and head.read() == b"" and int(head.headers["Content-Length"]) == len(media)
        assert head.headers["Content-Type"] == "video/mp4"

        with pytest.raises(HTTPError) as invalid:
            urlopen(Request(url, headers={"Authorization": "Bearer test-token", "Range": "bytes=99-100"}), timeout=5)
        assert invalid.value.code == 416
        assert invalid.value.headers["Content-Range"] == f"bytes */{len(media)}"

        with pytest.raises(HTTPError) as unauthorized:
            urlopen(url, timeout=5)
        assert unauthorized.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_http_server_declares_platform_queue_before_listening():
    assert web._LocalHTTPServer.__dict__['request_queue_size'] == socket.SOMAXCONN
    assert 'request_queue_size' not in ThreadingHTTPServer.__dict__
    with web._LocalHTTPServer(('127.0.0.1', 0), LiveClipperRequestHandler) as server:
        assert server.request_queue_size == socket.SOMAXCONN
        with socket.create_connection(server.server_address, timeout=1) as connection:
            assert connection.getpeername() == server.server_address


def test_app_accepts_three_bursts_of_twelve_new_connections(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    for key, name in [('HOME', 'home'), ('TMPDIR', 'tmp'), ('XDG_CACHE_HOME', 'cache'), ('LIVE_CLIPPER_HOME', 'app-home')]:
        directory = tmp_path / name
        directory.mkdir()
        env[key] = str(directory)
    env['LIVE_CLIPPER_WEB_TOKEN'] = 'http-test-token'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    command = [sys.executable, '-I', '-B', '-u', '-c',
               'import sys; sys.path.insert(0, sys.argv[1]); from live_clipper.cli import run_app; '
               'run_app(host="127.0.0.1", port=int(sys.argv[2]))', str(source), str(port)]
    process = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = Event()
    lines = []

    def collect_output():
        for line in process.stdout:
            lines.append(line)
            if '[Web] Venus 控制台已启动:' in line:
                ready.set()

    reader = Thread(target=collect_output, daemon=True)
    reader.start()
    report = {'python': sys.version, 'command': command, 'pid': process.pid, 'port': port, 'rounds': []}
    paths = ['/api/migration', '/api/onboarding', '/api/projects', '/api/project-form-options', '/api/service', '/api/resources'] * 2
    try:
        assert ready.wait(timeout=30), ''.join(lines)
        for index in range(3):
            barrier = Barrier(12)

            def request(path, barrier=barrier):
                item = {'path': path, 'method': 'GET', 'events': []}
                with socket.socket() as connection:
                    connection.settimeout(20)
                    try:
                        barrier.wait(timeout=5)
                        item['start_ns'] = time.monotonic_ns()
                        item['stage'] = 'connect'
                        connection.connect(('127.0.0.1', port))
                        item['events'].append(['connected', time.monotonic_ns()])
                        item['stage'] = 'send'
                        connection.sendall(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer http-test-token\r\nConnection: close\r\n\r\n'.encode())
                        item['events'].append(['sent', time.monotonic_ns()])
                        item['stage'] = 'response'
                        data = bytearray()
                        while chunk := connection.recv(65536):
                            data.extend(chunk)
                        headers, body = bytes(data).split(b'\r\n\r\n', 1)
                        item['status'] = int(headers.split(b'\r\n')[0].split()[1])
                        item['json_valid'] = isinstance(json.loads(body), dict)
                        item['events'].append(['response', time.monotonic_ns()])
                    except Exception as error:
                        item.update(error=type(error).__name__, message=str(error), traceback=traceback.format_exc())
                    finally:
                        item['end_ns'] = time.monotonic_ns()
                return item

            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(request, paths))
            report['rounds'].append({'index': index, 'results': results, 'backend_poll': process.poll()})
        assert all(row['backend_poll'] is None for row in report['rounds'])
        failures = [item for row in report['rounds'] for item in row['results']
                    if item.get('status') != 200 or not item.get('json_valid')]
        assert not failures, json.dumps(failures, indent=2)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            report['exit_code'] = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            report['exit_code'] = process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
        with socket.socket() as probe:
            probe.settimeout(1)
            report['port_closed'] = probe.connect_ex(('127.0.0.1', port)) != 0
        (tmp_path / 'http-bursts.json').write_text(json.dumps(report, indent=2) + '\n')
        (tmp_path / 'backend.log').write_text(''.join(lines))
        assert not reader.is_alive()
        assert report['exit_code'] == 0 and report['port_closed']
