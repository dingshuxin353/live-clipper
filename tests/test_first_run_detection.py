from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from live_clipper.config import DEFAULT_CONFIG_TEMPLATE
from live_clipper.first_run_detection import detect_first_run_environment, inspect_startup
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import ProjectError, open_project_repository
from live_clipper.project_storage import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, ProjectRepository, database_path


def _fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(item.relative_to(root)): (
            item.stat().st_size,
            item.stat().st_mtime_ns,
            hashlib.sha256(item.read_bytes()).hexdigest(),
        )
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def test_empty_template_and_env_are_new_and_detection_is_read_only(tmp_path):
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    service_dir = tmp_path / "work" / "service"
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(config_path=config_path, env_path=env_path, service_dir=service_dir)
    decision = inspect_startup(config_path=config_path, env_path=env_path, service_dir=service_dir)

    assert not detection.has_legacy_evidence
    assert detection.data_mode == "absent" and detection.project_count == 0
    assert decision.entry == "onboarding" and decision.onboarding == "new"
    assert _fingerprint(tmp_path) == before
    assert not database_path(service_dir).exists()


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        ("metadata", "legacy_metadata"),
        ("marker_completed", "legacy_onboarding_marker"),
        ("marker_skipped", "legacy_onboarding_marker"),
        ("global_source", "legacy_global_source_configured"),
    ],
)
def test_legacy_evidence_routes_to_migration_without_writes(tmp_path, fixture, expected_code):
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    env_path.write_text("SENTINEL_KEY=do-not-read-or-copy\n", encoding="utf-8")
    if fixture == "metadata":
        (service_dir / "runs.json").write_text('{"runs": []}', encoding="utf-8")
    elif fixture.startswith("marker"):
        key = "completed_at" if fixture.endswith("completed") else "skipped_at"
        (service_dir / "onboarding.json").write_text(json.dumps({key: "2026-01-01T00:00:00Z"}), encoding="utf-8")
    else:
        config_path.write_text(
            DEFAULT_CONFIG_TEMPLATE.replace('source_dir = ""\nsince_hours = 36', 'source_dir = "/Volumes/archive"\nsince_hours = 36'),
            encoding="utf-8",
        )
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(config_path=config_path, env_path=env_path, service_dir=service_dir)
    decision = inspect_startup(config_path=config_path, env_path=env_path, service_dir=service_dir)

    assert expected_code in detection.evidence_codes
    assert decision.entry == "migration_required"
    assert _fingerprint(tmp_path) == before
    assert "SENTINEL_KEY" not in repr(detection) + repr(decision)


def test_legacy_only_preflight_blocks_repository_creation(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    before = _fingerprint(tmp_path)
    with pytest.raises(ProjectError) as error:
        open_project_repository(service_dir)
    assert error.value.code == "migration_required"
    assert _fingerprint(tmp_path) == before
    assert not database_path(service_dir).exists()


def test_projects_and_first_run_facts_are_read_with_no_database_writes(tmp_path):
    service_dir = tmp_path / "service"
    repository = open_project_repository(service_dir)
    repository.begin_first_run_session()
    repository.close()
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )

    assert detection.has_project_database and detection.data_mode == "projects"
    assert detection.has_first_run_session and detection.project_count == 0
    assert decision.entry == "onboarding" and decision.onboarding == "resume"
    assert _fingerprint(tmp_path) == before


def test_existing_project_without_session_routes_to_workbench(tmp_path):
    service_dir = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    repository = open_project_repository(service_dir)
    project = repository.create_project("已有项目", default_project_config(source, output))
    repository.close()

    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert project.project_id
    assert decision.entry == "workbench" and decision.onboarding is None


def test_unexplained_legacy_v2_database_is_diagnostic_and_not_upgraded(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    connection = sqlite3.connect(database_path(service_dir))
    connection.executescript(
        """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE system_state(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
INSERT INTO schema_migrations VALUES (1, 'v1', '2026-08-29T00:00:00Z');
INSERT INTO schema_migrations VALUES (2, 'v2', '2026-08-29T00:00:01Z');
INSERT INTO system_state VALUES ('data_mode', 'legacy', '2026-08-29T00:00:01Z');
"""
    )
    connection.close()
    before = _fingerprint(tmp_path)

    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert decision.entry == "diagnostic_required"
    with pytest.raises(ProjectError) as error:
        open_project_repository(service_dir)
    assert error.value.code == "diagnostic_required"
    assert _fingerprint(tmp_path) == before
    check = sqlite3.connect(database_path(service_dir))
    assert check.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]


def test_pre_v4_nonempty_legacy_import_is_diagnostic_without_upgrade(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    connection = sqlite3.connect(database_path(service_dir), isolation_level=None)
    connection.create_function("migration_fault", 1, lambda _phase: 0)
    connection.executescript(
        "BEGIN;"
        + SCHEMA_V1
        + "INSERT INTO schema_migrations VALUES (1, 'v1', '2026-09-01T00:00:00Z');"
        + "INSERT INTO system_state VALUES ('data_mode', 'legacy', '2026-09-01T00:00:00Z');"
        + SCHEMA_V2
        + "INSERT INTO schema_migrations VALUES (2, 'v2', '2026-09-01T00:00:01Z');"
        + SCHEMA_V3
        + "INSERT INTO schema_migrations VALUES (3, 'v3', '2026-09-01T00:00:02Z');"
        + "INSERT INTO legacy_imports VALUES ('import-1', 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', '{}', 'backup', 'completed', '{}', NULL, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');"
        + "COMMIT;"
    )
    connection.close()
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert "legacy_import_state_requires_diagnostic" in detection.evidence_codes
    assert decision.entry == "diagnostic_required"
    assert decision.reason_code == "legacy_import_state_requires_diagnostic"
    assert _fingerprint(tmp_path) == before


def test_durable_active_migration_is_detected_read_only_and_routes_to_resume(tmp_path):
    service_dir = tmp_path / "service"
    repository = ProjectRepository(service_dir)
    repository.create_migration_session(
        migration_id="migration-1",
        source_fingerprint="a" * 64,
        plan_version=3,
        plan_hash="b" * 64,
        source_manifest=[],
        choices={},
        request_id="request-1",
        request_hash="c" * 64,
        backup_path="/backup",
    )
    repository.close()
    (service_dir / "runs.json").write_text('{"runs": []}', encoding="utf-8")
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )

    assert detection.migration_session_count == 1
    assert decision.entry == "migration_required" and decision.reason_code == "migration_resume"
    assert _fingerprint(tmp_path) == before


def test_strict_empty_index_resumes_project_first_run_without_writes(tmp_path):
    service_dir = tmp_path / 'service'
    with open_project_repository(service_dir) as repository:
        session = repository.begin_first_run_session()
        repository.update_first_run_draft(session.revision, {'project': {'name': '原草稿'}}, current_step='ai')
    (service_dir / 'service.json').write_text('{"runtime_mode":"projects"}')
    (service_dir / 'runs.json').write_text(' { "runs" : [ ] } \n')
    before = _fingerprint(tmp_path)
    decision = inspect_startup(config_path=tmp_path / 'missing.toml', env_path=tmp_path / 'missing.env', service_dir=service_dir)
    assert decision.entry == 'onboarding' and decision.onboarding == 'resume'
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize('content', ['{"runs":[{}]}', '{', '[]', '{}', '{"runs":{}}', '{"runs":null}',
                                     '{"runs":[],"unknown":1}', '{"runs":[1],"runs":[]}', '{"runs":[],"runs":[]}'])
def test_uncertain_or_nonempty_index_keeps_conflict(tmp_path, content):
    service_dir = tmp_path / 'service'
    with open_project_repository(service_dir) as repo:
        repo.begin_first_run_session()
    (service_dir / 'service.json').write_text('{"runtime_mode":"projects"}')
    (service_dir / 'runs.json').write_text(content)
    before = _fingerprint(tmp_path)
    decision = inspect_startup(config_path=tmp_path / 'config', env_path=tmp_path / 'env', service_dir=service_dir)
    assert decision.reason_code == 'legacy_projects_conflict'
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize('fault', ['directory', 'permission', 'disappears', 'changes'])
def test_uncertain_read_never_becomes_empty_index(tmp_path, monkeypatch, fault):
    from live_clipper import first_run_detection as detector

    service_dir = tmp_path / 'service'
    with open_project_repository(service_dir) as repo:
        repo.begin_first_run_session()
    (service_dir / 'service.json').write_text('{"runtime_mode":"projects"}')
    target = service_dir / 'runs.json'
    if fault == 'directory':
        target.mkdir()
    else:
        target.write_text('{"runs":[]}')
    if fault == 'permission':
        original_open = detector.os.open
        def denied(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError('injected read denial')
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(detector.os, 'open', denied)
    if fault in {'disappears', 'changes'}:
        original_loads = detector.json.loads
        def mutate(text, *args, **kwargs):
            if text == '{"runs":[]}':
                if fault == 'disappears':
                    target.unlink()
                else:
                    target.write_text('{"runs":[1]}')
            return original_loads(text, *args, **kwargs)
        monkeypatch.setattr(detector.json, 'loads', mutate)
    decision = inspect_startup(config_path=tmp_path / 'config', env_path=tmp_path / 'env', service_dir=service_dir)
    assert decision.reason_code == 'legacy_projects_conflict'


@pytest.mark.parametrize('independent', ['onboarding.json', 'global_config', 'missing_runtime', 'invalid_runtime'])
def test_empty_index_does_not_override_other_evidence(tmp_path, independent):
    service_dir = tmp_path / 'service'
    with open_project_repository(service_dir) as repo:
        repo.begin_first_run_session()
    (service_dir / 'runs.json').write_text('{"runs":[]}')
    marker = service_dir / 'service.json'
    marker.write_text('{"runtime_mode":"projects"}')
    config = tmp_path / 'config'
    if independent == 'onboarding.json':
        (service_dir / independent).write_text('{}')
    elif independent == 'global_config':
        config.write_text('[recording_source.default]\nsource_dir="/old/source"\n')
    elif independent == 'missing_runtime':
        marker.unlink()
    else:
        marker.write_text('not JSON')
    before = _fingerprint(tmp_path)
    assert inspect_startup(config_path=config, env_path=tmp_path / 'env', service_dir=service_dir).entry == 'diagnostic_required'
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize(('state', 'project_exists', 'expected'), [
    ('paused', False, ('onboarding', 'paused', None)),
    ('activation_pending', True, ('onboarding', 'activation_pending', None)),
    ('completed', True, ('workbench', None, None)),
    ('completed', False, ('diagnostic_required', None, 'completed_project_missing')),
    (None, True, ('workbench', None, None)),
])
def test_empty_index_preserves_existing_startup_state(tmp_path, state, project_exists, expected):
    service_dir = tmp_path / 'service'
    with open_project_repository(service_dir) as repo:
        session = repo.begin_first_run_session() if state else None
        project_id = repo.create_project('已有项目', default_project_config(tmp_path/'source', tmp_path/'output')).project_id if project_exists else 'missing'
        if state:
            if state == 'paused':
                repo.pause_first_run(session.revision)
            else:
                repo.connection.execute("UPDATE first_run_sessions SET state=?, current_step='complete', project_request_id='request', project_request_hash='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', first_project_id=?, completed_at=?",
                                        (state, project_id, '2026-09-10T00:00:00Z' if state == 'completed' else None))
                repo.connection.commit()
    (service_dir / 'service.json').write_text('{"runtime_mode":"projects"}')
    (service_dir / 'runs.json').write_text('{"runs":[]}')
    before = _fingerprint(tmp_path)
    result = inspect_startup(config_path=tmp_path/'config', env_path=tmp_path/'env', service_dir=service_dir)
    assert (result.entry, result.onboarding, result.reason_code) == expected
    assert _fingerprint(tmp_path) == before


def test_real_app_startup_converts_saved_choice_and_resumes_again(tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys
    import threading
    import time
    import urllib.request

    from live_clipper.resource_store import ResourceStore

    home = tmp_path / 'app-home'
    home.mkdir()
    service_dir = home / 'work/service'
    config = home / 'live-clipper.toml'
    config.write_text(f'[paths]\nworkspace_root = "{home / "workspace"}"\n[llm]\nmodel="saved-model"\napi_base="https://saved.test/v1"\n')
    (home / '.env').write_text('CHEAP_MODEL_API_KEY=isolated-test-credential\n')
    with open_project_repository(service_dir) as repo:
        repo.begin_first_run_session()
        saved = repo.update_first_run_draft(1, {'ai': {'model': 'saved-model', 'api_base': 'https://saved.test/v1'}, 'project': {'name': '原来的项目'}}, current_step='ai')
    (service_dir / 'service.json').write_text('{"runtime_mode":"projects"}')
    index = service_dir / 'runs.json'
    index.write_text('{"runs":[]}')
    original_index = index.read_bytes()
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'LIVE_CLIPPER_HOME': str(home),
           'HF_HUB_OFFLINE': '1', 'PYTHONNOUSERSITE': '1'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    snapshots = []
    for attempt in range(2):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        with (tmp_path / f'startup-{attempt}.log').open('w') as log:
            process = subprocess.Popen([str(Path(sys.executable).with_name('live-clipper')), 'app', '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=home, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, 'App exited; see isolated startup log'
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/api/onboarding', timeout=1) as response:
                            snapshot = json.load(response)
                        break
                    except OSError:
                        assert time.monotonic() < deadline, 'App readiness timed out'
                        threading.Event().wait(0.05)
                assert snapshot['entry']['onboarding'] == 'resume', snapshot['entry']
                assert snapshot['session']['current_step'] == saved.current_step
                assert snapshot['session']['draft']['project'] == saved.draft['project']
                snapshots.append(snapshot['session'])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0
        assert index.read_bytes() == original_index
    assert snapshots[0] == snapshots[1]
    with ProjectRepository(service_dir) as repo:
        session = repo.get_first_run_session()
        resource = ResourceStore(repo).get(session.draft['ai']['resource_id'])
        assert not resource['ready']
        assert resource['config']['model'] == 'saved-model'
        assert repo.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()[0] == 'completed'
        assert repo.connection.execute('SELECT count(*) FROM first_run_sessions').fetchone()[0] == 1
        assert repo.connection.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
        assert repo.connection.execute('SELECT count(*) FROM migration_sessions').fetchone()[0] == 0
