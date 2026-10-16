from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from resource_test_support import onboarding_resource_patch

from live_clipper import asr_models, onboarding_resources, service
from live_clipper.config import Settings
from live_clipper.first_run_detection import inspect_startup
from live_clipper.onboarding_coordinator import OnboardingCoordinator, OnboardingError
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import open_project_repository
from live_clipper.project_storage import database_path


def _coordinator(tmp_path: Path, *, settings: Settings | None = None) -> tuple[OnboardingCoordinator, Path]:
    source = tmp_path / "recordings"
    source.mkdir()
    configured = settings or Settings(cheap_model_api_key="ai-secret", cheap_model_name="ai-model", asr_model="local-model")
    return (
        OnboardingCoordinator(
            service_dir=tmp_path / "work" / "service",
            config_path=tmp_path / "live-clipper.toml",
            env_path=tmp_path / ".env",
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            settings_loader=lambda: configured,
        ),
        source,
    )


def test_environment_sqlite_check_stays_blocked_when_read_budget_expires(tmp_path, monkeypatch):
    from test_startup_snapshot import _assert_readers_closed, _track_readers

    coordinator, _source = _coordinator(tmp_path)
    coordinator.service_dir.mkdir(parents=True)
    writer = sqlite3.connect(database_path(coordinator.service_dir), isolation_level=None)
    try:
        writer.execute('CREATE TABLE protected(value)')
        writer.execute("INSERT INTO protected VALUES ('retained')")
        writer.execute('BEGIN EXCLUSIVE')
        readers = _track_readers(monkeypatch)
        started = time.monotonic()
        result = coordinator._environment_summary()
        assert time.monotonic() - started < 2
        sqlite_check = next(item for item in result['checks'] if item['name'] == 'sqlite')
        assert sqlite_check['status'] == 'blocked'
        assert sqlite_check['problem'] == '无法读取本地数据库'
        assert writer.in_transaction
        assert writer.execute('SELECT value FROM protected').fetchall() == [('retained',)]
        _assert_readers_closed(readers)
    finally:
        writer.close()


def _project_patch(source: Path, output: Path) -> dict[str, object]:
    return {
        "name": "首项目",
        "source_directory": str(source),
        "output_directory": str(output),
        "trigger_mode": "manual",
    }


def test_unknown_fields_are_rejected_before_session_mutation(tmp_path: Path) -> None:
    coordinator, _source = _coordinator(tmp_path)
    with pytest.raises(OnboardingError) as error:
        coordinator.start({"unexpected": True})
    assert error.value.code == "validation_failed"


def test_project_draft_only_accepts_m1_fields_and_builds_v2(tmp_path: Path) -> None:
    coordinator, source = _coordinator(tmp_path)
    coordinator.start({})
    _status, patched = coordinator.patch_session(
        {
            "request_id": "draft",
            "expected_revision": 1,
            "current_step": "project",
            "patch": {**onboarding_resource_patch(coordinator), "project": _project_patch(source, tmp_path / "output")},
        }
    )
    assert patched["session"]["revision"] == 2
    with pytest.raises(OnboardingError) as error:
        coordinator.patch_session(
            {
                "request_id": "advanced",
                "expected_revision": 2,
                "current_step": "project",
                "patch": {**onboarding_resource_patch(coordinator), "project": {"review_strategy": "manual"}},
            }
        )
    assert error.value.code == "validation_failed"
    name, config, _raw = coordinator._project_from_draft(patched["session"]["draft"])
    assert name == "首项目"
    assert config["schema_version"] == 2
    assert config["processing"]["review_strategy"] == "ai_auto"
    assert config["resources"]["review_ref"] == "reuse_analysis"


def test_finish_failure_keeps_single_project_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, source = _coordinator(tmp_path)
    monkeypatch.setattr(asr_models, "local_path_for", lambda _model: tmp_path / "installed")
    coordinator.start({})
    _status, patched = coordinator.patch_session(
        {
            "request_id": "draft",
            "expected_revision": 1,
            "current_step": "project",
            "patch": {**onboarding_resource_patch(coordinator), "project": _project_patch(source, tmp_path / "output")},
        }
    )
    monkeypatch.setattr(onboarding_resources, "test_ai_service", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": False, "error_code": "service_not_ready", "message": "未启动"})
    status, pending = coordinator.finish({"request_id": "finish", "expected_revision": patched["session"]["revision"]})
    assert status == 202
    assert pending["session"]["state"] == "activation_pending"
    project_id = pending["project"]["project_id"]
    assert coordinator.snapshot()["session"]["first_project"]["project_id"] == project_id
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True, "ready": True})
    status, completed = coordinator.retry({"request_id": "finish", "expected_revision": pending["session"]["revision"]})
    assert status == 200
    assert completed["session"]["state"] == "completed"
    assert completed["session"]["first_project"]["project_id"] == project_id


def test_current_project_runtime_metadata_does_not_conflict_with_completed_projects(tmp_path: Path) -> None:
    service_dir = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    with open_project_repository(service_dir) as repository:
        repository.create_project("已有项目", default_project_config(source, output))
        state = service._service_state("running", 123, Settings(), service_dir=service_dir)
        service._write_service_state(service_dir, state)
        service.append_event(service_dir, "service_started", pid=123, embedded=True)

    assert database_path(service_dir).is_file()
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml",
        env_path=tmp_path / "missing.env",
        service_dir=service_dir,
    )
    assert decision.entry == "workbench"


@pytest.mark.parametrize('operation', ['patch_session', 'pause'])
def test_onboarding_write_and_replay_identity_commit_together(tmp_path, monkeypatch, operation):
    from live_clipper.project_storage import ProjectRepository

    coordinator, _ = _coordinator(tmp_path)
    coordinator.start({})
    body = {'request_id': 'stable-operation', 'expected_revision': 1}
    if operation == 'patch_session':
        body.update(current_step='asr', patch={})
    original = ProjectRepository.save_idempotency_key

    def fail_record(*args, **kwargs):
        raise RuntimeError('interrupted before replay identity')

    monkeypatch.setattr(ProjectRepository, 'save_idempotency_key', fail_record)
    with pytest.raises(RuntimeError):
        getattr(coordinator, operation)(body)
    with ProjectRepository(coordinator.service_dir) as repository:
        current = repository.get_first_run_session()
        assert current.revision == 1
        assert current.state == 'in_progress'
        assert current.current_step == 'welcome'
    monkeypatch.setattr(ProjectRepository, 'save_idempotency_key', original)
    _, saved = getattr(coordinator, operation)(body)
    _, replay = getattr(coordinator, operation)(body)
    assert replay['reused'] is True
    assert replay['session'] == saved['session']


@pytest.mark.parametrize('via_http', [False, True])
def test_snapshot_recovers_reserved_project_and_returns_only_post_recovery_state(tmp_path, monkeypatch, via_http):
    import json
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.request import urlopen

    from live_clipper.first_run_detection import StartupInspection
    from live_clipper.project_storage import ProjectRepository
    from live_clipper.web import LiveClipperRequestHandler, WebPaths

    coordinator, source = _coordinator(tmp_path)
    coordinator.start({})
    with coordinator._repo() as repo:
        session = repo.get_first_run_session()
        reserved = repo.reserve_first_project_request(session.revision, 'recover-project', 'a' * 64)
        project = repo.create_project('保留项目', default_project_config(source, tmp_path / 'output'))
        repo.save_idempotency_key('project.create', 'recover-project', request_hash='a' * 64,
                                 object_type='project', object_id=project.project_id)
    before = coordinator.inspect()
    assert before.facts.session.first_project_id is None
    summaries = []
    original_summary = StartupInspection.migration_summary

    def summary(inspection):
        summaries.append(inspection)
        return original_summary(inspection)

    monkeypatch.setattr(StartupInspection, 'migration_summary', summary)
    if via_http:
        class Handler(LiveClipperRequestHandler):
            paths = WebPaths(service_dir=coordinator.service_dir, config_path=coordinator.config_path,
                             input_dir=coordinator.input_dir, output_root=coordinator.output_root)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/onboarding', timeout=5) as response:
                assert response.status == 200
                payload = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            assert not thread.is_alive()
    else:
        payload = coordinator.snapshot(startup=before)
    assert payload['entry']['onboarding'] == 'activation_pending'
    assert payload['session']['state'] == 'activation_pending'
    assert payload['session']['revision'] == reserved.revision + 1
    assert payload['session']['first_project']['project_id'] == project.project_id
    assert payload['session']['pending_finish_request_id'] == 'recover-project'
    assert payload['migration'] is None
    assert len(summaries) == 1
    assert summaries[0].facts.session.first_project_id == project.project_id
    assert summaries[0].entry == payload['entry']
    repeated = coordinator.snapshot()
    assert repeated['entry'] == payload['entry'] and repeated['session'] == payload['session']
    with ProjectRepository(coordinator.service_dir) as repo:
        assert [item.project_id for item in repo.list_projects()] == [project.project_id]
        assert repo.get_first_run_session().revision == reserved.revision + 1
