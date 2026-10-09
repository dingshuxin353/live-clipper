from __future__ import annotations

from contextlib import contextmanager
from threading import Event, current_thread

import pytest
from test_migration_coordinator import _legacy_home, _validated

from live_clipper.migration_coordinator import MigrationError
from live_clipper.project_storage import ProjectRepository
from live_clipper.resource_store import ResourceStore


def execute(coordinator):
    plan = _validated(coordinator)
    return coordinator.execute({
        'request_id': 'complete-conversion', 'source_fingerprint': plan['source_fingerprint'],
        'plan_hash': plan['plan_hash'], 'choices': plan['choices'],
    })[1]['session']


def finish(coordinator, session):
    coordinator._futures[(str(coordinator.service_dir), session['migration_id'])].result(timeout=10)
    return coordinator.snapshot()


def test_migration_remains_incomplete_until_resource_conversion_commits(tmp_path, monkeypatch):
    coordinator, service_dir = _legacy_home(tmp_path)
    reached, release = Event(), Event()
    original = ResourceStore.__init__

    def pause(self, repository):
        original(self, repository)
        if current_thread().name.startswith('venus-migration'):
            reached.set()
            assert release.wait(10)

    monkeypatch.setattr(ResourceStore, '__init__', pause)
    session = execute(coordinator)
    try:
        assert reached.wait(10)
        snapshot = coordinator.snapshot()
        assert snapshot['entry'] == 'executing'
        assert not snapshot['session']['state'].startswith('completed_')
        assert snapshot['report'] is None
        with migration_http(coordinator) as request:
            status, payload = request('/api/onboarding')
            assert status == 200 and payload['migration']['entry'] == 'executing'
            assert payload['migration']['report'] is None
            assert request('/api/projects', {})[0] == 409
        with ProjectRepository(service_dir) as observer:
            assert observer.get_data_mode() == 'legacy'
            assert observer.list_projects() == []
        with pytest.raises(MigrationError):
            coordinator.acknowledge({'request_id': 'early-enter',
                                     'migration_id': session['migration_id'],
                                     'expected_revision': snapshot['session']['revision']})
    finally:
        release.set()
        completed = finish(coordinator, session)
    assert completed['entry'] == 'completed'
    with ProjectRepository(service_dir) as observer:
        assert observer.get_data_mode() == 'projects'
        assert observer.connection.execute(
            "SELECT value FROM system_state WHERE key='named_resources_migration'"
        ).fetchone()[0] == 'completed'


def test_resource_creation_failure_rolls_back_whole_migration_and_owned_credentials(tmp_path, monkeypatch):
    coordinator, service_dir = _legacy_home(tmp_path)
    from live_clipper.config import load_settings

    coordinator.env_path.write_text('CHEAP_MODEL_API_KEY=isolated-test-key\n')
    coordinator.settings_loader = lambda: load_settings(coordinator.config_path, env_path=coordinator.env_path)
    original = ResourceStore._write_credential
    attempted = []

    def fail_after_file(self, value, **kwargs):
        original(self, value, **kwargs)
        attempted.append(True)
        raise OSError('injected disk failure after credential persistence')

    monkeypatch.setattr(ResourceStore, '_write_credential', fail_after_file)
    session = execute(coordinator)
    snapshot = finish(coordinator, session)
    assert attempted == [True]
    assert snapshot['session']['state'] == 'failed_rolled_back'
    assert snapshot['report'] is None
    with ProjectRepository(service_dir) as observer:
        assert observer.get_data_mode() == 'legacy'
        assert observer.list_projects() == []
        assert ResourceStore(observer).list() == []
    assert list((service_dir / 'resource-credentials').glob('*.env')) == []


def released_half_complete(tmp_path, *, acknowledged=False, asr_backend='mlx_whisper'):
    """Execute the released storage transaction, without its later resource conversion."""
    from live_clipper.project_domain import legacy_id
    from live_clipper.project_migration import build_migration_plan, create_migration_backup

    coordinator, service_dir = _legacy_home(tmp_path)
    coordinator.config_path.write_text(coordinator.config_path.read_text().replace('[asr]\n', f'[asr]\nbackend = "{asr_backend}"\n'))
    choices = _validated(coordinator)['choices']
    inspection = coordinator._inspection()
    plan = build_migration_plan(inspection, choices=choices, backup_root=coordinator.backup_root)
    migration_id = legacy_id(plan.source_fingerprint, 'migration')
    project_id = legacy_id(plan.source_fingerprint, 'project:default')
    backup = create_migration_backup(inspection, backup_root=coordinator.backup_root, migration_id=migration_id)
    with ProjectRepository(service_dir) as repo:
        session = repo.create_migration_session(migration_id=migration_id, source_fingerprint=plan.source_fingerprint,
            plan_version=plan.plan_version, plan_hash=plan.plan_hash, source_manifest=[e.to_dict() for e in inspection.source_manifest],
            choices=dict(plan.choices), request_id='released-execute', request_hash='a'*64, backup_path=str(backup.path))
        session = repo.update_migration_stage(migration_id, session.revision, state='backing_up', stage='copy', backup_status='completed')
        session = repo.update_migration_stage(migration_id, session.revision, state='migrating', stage='project')
        session = repo.update_migration_stage(migration_id, session.revision, state='validating', stage='database')
        report = {'plan_version': plan.plan_version, 'plan_hash': plan.plan_hash,
                  'discovery': {**plan.discovery_summary, 'timezone': plan.project_preview['timezone'],
                                **{key: plan.project_preview[key] for key in ('trigger_mode', 'schedule_mode', 'daily_time', 'interval_minutes')}},
                  'imported': plan.history_summary['counts']['importable'],
                  'compatibility': plan.history_summary['counts']['compatibility'],
                  'quarantined': plan.history_summary['counts']['quarantined'], 'safe_results': 0,
                  'quarantine_reason_codes': [],
                  'project': {'project_id': project_id, 'name': '迁移项目'}, 'backup_created': True,
                  'history_total': len(plan.history_summary['entries']), 'blocker_codes': ['resource_validation_required'],
                  'readiness': 'attention', 'blocker_count': 1}
        session = repo.apply_migration_transaction(migration_id, session.revision, source_fingerprint=plan.source_fingerprint,
            plan_hash=plan.plan_hash, project_id=project_id, project_name='迁移项目', config=coordinator._config(plan),
            history_entries=plan.history_summary['entries'], safe_results=[], blocker_codes=['resource_validation_required'], report=report)
        if acknowledged:
            session = repo.acknowledge_migration_session(migration_id, session.revision)
    return coordinator, session


@pytest.mark.parametrize('acknowledged', [False, True])
def test_released_half_complete_resumes_only_resources_preserving_later_data(tmp_path, acknowledged):
    from live_clipper.first_run_detection import inspect_startup
    from live_clipper.project_domain import default_project_config
    from live_clipper.project_service import ProjectError, ProjectManager
    from live_clipper.resource_api import ResourceAPI

    coordinator, original = released_half_complete(tmp_path, acknowledged=acknowledged)
    with ProjectRepository(coordinator.service_dir) as repo:
        project = repo.get_project(original.project_id)
        history = repo.list_runs(original.project_id)
        later = repo.create_project('later user project', default_project_config(tmp_path/'in', tmp_path/'out'))
        manager = ProjectManager(repo, coordinator.settings_loader())
        with pytest.raises(ProjectError) as blocked:
            manager.enable_project(original.project_id)
        assert blocked.value.code == 'migration_required'
        response = ResourceAPI(repo, coordinator.settings_loader()).dispatch('POST', ['api', 'resources', 'migration', 'retry'], {})
        assert response[0] == 409
    assert inspect_startup(config_path=coordinator.config_path, env_path=coordinator.env_path,
                           service_dir=coordinator.service_dir).entry == 'migration_required'
    assert coordinator.snapshot()['entry'] == 'incomplete'
    assert coordinator.snapshot()['report'] is None
    assert len(coordinator.history(original.migration_id)[1]['history']) == len(history)
    with pytest.raises(MigrationError, match='migration_pending'):
        coordinator.acknowledge({'request_id': 'early', 'migration_id': original.migration_id, 'expected_revision': original.revision})
    body = {'request_id': 'continue-old', 'migration_id': original.migration_id, 'expected_revision': original.revision}
    accepted = coordinator.retry(body)[1]['session']
    result = finish(coordinator, accepted)
    assert result['entry'] == 'completed'
    assert result['session']['migration_id'] == original.migration_id
    assert result['session']['state'] == 'completed_attention'
    assert result['report']['acknowledged_at'] is None
    assert coordinator.retry(body)[1]['session']['revision'] == result['session']['revision']
    with ProjectRepository(coordinator.service_dir) as repo:
        assert repo.list_runs(original.project_id) == history
        assert repo.get_project(later.project_id) == later
        assert repo.get_project(original.project_id).activation_state == project.activation_state
        assert len(repo.list_projects()) == 2
        assert len(repo.list_migration_sessions()) == 1
        assert not repo.resource_migration_pending()
        refs = repo.get_config_revision(original.project_id).config['resources']
        assert refs['asr_ref'] != 'legacy.asr.default'
        assert refs['analysis_ref'] != 'legacy.analysis.default'


@pytest.mark.parametrize('conflict,code', [('source', 'migration_source_changed'), ('backup', 'migration_backup_invalid'),
                                        ('reference', 'migration_resource_conflict'), ('credentials', 'migration_credential_source_unknown')])
def test_old_half_complete_rejects_changed_recovery_identity(tmp_path, conflict, code):
    coordinator, session = released_half_complete(tmp_path, acknowledged=True)
    if conflict == 'source':
        coordinator.config_path.write_text(coordinator.config_path.read_text() + '\n# changed\n')
    elif conflict == 'backup':
        (coordinator.backup_root / session.migration_id / 'manifest.json').write_text('{}')
    elif conflict == 'credentials':
        coordinator.env_path.write_text('CHEAP_MODEL_API_KEY=new-unproven-key\n')
    else:
        with ProjectRepository(coordinator.service_dir) as repo:
            revision = repo.get_config_revision(session.project_id)
            config = revision.config
            config['resources']['analysis_ref'] = 'unrecognized-resource'
            repo.add_config_revision(session.project_id, config, expected_revision=revision.revision)
    with ProjectRepository(coordinator.service_dir) as repo:
        before = repo.list_projects(), repo.list_runs()
    with pytest.raises(MigrationError, match=code):
        coordinator.retry({'request_id': 'unsafe', 'migration_id': session.migration_id, 'expected_revision': session.revision})
    assert coordinator.snapshot()['entry'] == 'incomplete'
    assert coordinator.snapshot()['session']['failure']['code'] == code
    with ProjectRepository(coordinator.service_dir) as repo:
        assert (repo.list_projects(), repo.list_runs()) == before


@pytest.mark.parametrize('stage', ['after_resources', 'after_projects'])
def test_resource_failure_after_rows_or_references_can_retry_same_identity(tmp_path, stage):
    coordinator, service_dir = _legacy_home(tmp_path)
    coordinator.env_path.write_text('CHEAP_MODEL_API_KEY=isolated-test-key\n')
    def fail(point):
        if point == stage:
            raise OSError('isolated conversion fault')
    coordinator.fault_injection = fail
    session = execute(coordinator)
    failed = finish(coordinator, session)['session']
    assert failed['state'] == 'failed_rolled_back'
    assert not list((service_dir / 'resource-credentials').glob('*.env'))
    with ProjectRepository(service_dir) as repo:
        assert not repo.list_projects() and not repo.list_runs() and not ResourceStore(repo).list()
    coordinator.fault_injection = None
    accepted = coordinator.retry({'request_id': 'retry-atomic', 'migration_id': session['migration_id'],
                                  'expected_revision': failed['revision']})[1]['session']
    assert finish(coordinator, accepted)['entry'] == 'completed'
    with ProjectRepository(service_dir) as repo:
        assert len(repo.list_projects()) == 1 and len(repo.list_runs()) == 1
        assert len(list((service_dir / 'resource-credentials').glob('*.env'))) == 1


def _migration_process(root, boundary, pipe):
    from pathlib import Path

    from live_clipper.migration_coordinator import MigrationCoordinator

    root = Path(root)
    if boundary == 'committed':
        from contextlib import contextmanager

        original_transaction = ProjectRepository.transaction
        @contextmanager
        def observe_commit(repository, **kwargs):
            with original_transaction(repository, **kwargs) as connection:
                yield connection
            if not repository.connection.in_transaction:
                rows = repository.list_migration_sessions()
                if rows and rows[0].state.startswith('completed_') and not repository.resource_migration_pending():
                    pipe.send({'session': coordinator._session_payload(rows[0])})
                    pipe.recv()
        ProjectRepository.transaction = observe_commit
    coordinator = MigrationCoordinator(service_dir=root/'work/service', config_path=root/'live-clipper.toml',
        env_path=root/'.env', input_dir=root/'input', output_root=root/'output')
    def checkpoint(stage):
        if stage == boundary:
            pipe.send(coordinator.snapshot())
            pipe.recv()
    coordinator.fault_injection = checkpoint
    session = execute(coordinator)
    coordinator._futures[(str(coordinator.service_dir), session['migration_id'])].result(timeout=30)


@pytest.mark.parametrize('boundary', ['after_resources', 'after_projects', 'committed'])
def test_process_kill_and_restart_preserve_exactly_one_migration_result(tmp_path, boundary):
    import multiprocessing

    coordinator, service_dir = _legacy_home(tmp_path)
    coordinator.env_path.write_text('CHEAP_MODEL_API_KEY=isolated-process-key\n')
    media, media_digest = add_safe_result(coordinator)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_migration_process, args=(str(tmp_path), boundary, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), 'child never reached real transaction boundary'
        held = parent.recv()
        session = held['session']
        before = coordinator._read_session()
        assert coordinator.recover_interrupted() == before  # Live foreign executor remains owner.
        assert len(list((service_dir/'resource-credentials').glob('*.env'))) == 1
        process.kill()
        process.join(10)
        assert not process.is_alive()
        recovered = coordinator.recover_interrupted()
        if boundary == 'committed':
            assert recovered.state == 'completed_attention'
        else:
            assert recovered.state == 'failed_rolled_back'
            with ProjectRepository(service_dir) as repo:
                assert not repo.list_projects() and not ResourceStore(repo).list()
            accepted = coordinator.retry({'request_id': 'after-kill', 'migration_id': session['migration_id'],
                                          'expected_revision': recovered.revision})[1]['session']
            assert finish(coordinator, accepted)['entry'] == 'completed'
        import hashlib

        snapshot = coordinator.snapshot()
        assert hashlib.sha256(media.read_bytes()).hexdigest() == media_digest
        assert snapshot['entry'] == 'completed'
        assert snapshot['session']['migration_id'] == session['migration_id']
        with ProjectRepository(service_dir) as repo:
            assert len(repo.list_projects()) == 1 and len(repo.list_runs()) == 1
            assert len(repo.list_migration_sessions()) == 1
            outputs = repo.list_run_outputs(repo.list_runs()[0].run_id)
            assert len(outputs) == 1
            evidence = service_dir.parent/'projects'/repo.list_projects()[0].project_id/'runs'/repo.list_runs()[0].run_id/'outputs'/outputs[0].output_id/'media_integrity.json'
            assert evidence.is_file()
            credentials = {row[0] for row in repo.connection.execute('SELECT binding_ref FROM resource_revisions WHERE binding_ref IS NOT NULL')}
            assert {file.stem for file in (service_dir/'resource-credentials').glob('*.env')} == credentials
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()


def test_half_complete_http_and_background_guards_then_wal_completion(tmp_path):

    from live_clipper import service
    from live_clipper.cli import ENV_TEMPLATE
    from live_clipper.project_runtime import tick_project_runtime
    from live_clipper.project_scheduler import tick_project_schedules

    coordinator, session = released_half_complete(tmp_path, acknowledged=True)
    coordinator.env_path.write_text(ENV_TEMPLATE)
    settings = coordinator.settings_loader()
    with ProjectRepository(coordinator.service_dir) as observer:
        before = observer.list_runs(), observer.list_projects()
        assert service.run_service_tick(settings, service_dir=coordinator.service_dir)['error_code'] == 'migration_pending'
        assert tick_project_runtime(settings, service_dir=coordinator.service_dir)['error_code'] == 'migration_pending'
        assert tick_project_schedules(settings, service_dir=coordinator.service_dir)['error_code'] == 'migration_pending'
        assert (observer.list_runs(), observer.list_projects()) == before
        with migration_http(coordinator) as request:
            assert request('/api/onboarding')[1]['migration']['entry'] == 'incomplete'
            assert request(f'/api/migration/{session.migration_id}/history')[0] == 200
            assert request('/api/migration/acknowledge', {'request_id': 'bad-enter', 'migration_id': session.migration_id,
                           'expected_revision': session.revision})[0] == 409
            for route in ['/api/projects', f'/api/projects/{session.project_id}/enable', '/api/resources/migration/retry', '/api/service/start']:
                assert request(route, {})[0] == 409
            status, accepted = request('/api/migration/retry', {'request_id': 'http-resume', 'migration_id': session.migration_id,
                                      'expected_revision': session.revision})
            assert status == 202
            assert finish(coordinator, accepted['session'])['entry'] == 'completed'
            # This writer stays open: all HTTP results must see the committed WAL.
            current = request('/api/onboarding')[1]
            assert current['migration']['entry'] == 'completed'
            assert current['entry']['mode'] == 'workbench'
            complete = request('/api/migration')[1]['session']
            assert request('/api/migration/acknowledge', {'request_id': 'enter', 'migration_id': session.migration_id,
                           'expected_revision': complete['revision']})[0] == 200


def add_safe_result(coordinator):
    import hashlib
    import json
    import subprocess

    media = coordinator.output_root / 'legacy.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x120:r=15:d=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(media)], check=True, capture_output=True)
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    runs = coordinator.service_dir / 'runs.json'
    payload = json.loads(runs.read_text())
    payload['runs'][0].update(result_path=str(media), result_sha256=digest)
    runs.write_text(json.dumps(payload))
    return media, digest


def test_rollback_cleans_owned_evidence_and_keeps_original_media_and_backup(tmp_path):
    import hashlib

    coordinator, service_dir = _legacy_home(tmp_path)
    media, digest = add_safe_result(coordinator)
    def fail(stage):
        if stage == 'after_projects':
            raise OSError('failed after references')
    coordinator.fault_injection = fail
    session = execute(coordinator)
    assert finish(coordinator, session)['session']['state'] == 'failed_rolled_back'
    assert not list((service_dir.parent/'projects').rglob('media_integrity.*'))
    assert hashlib.sha256(media.read_bytes()).hexdigest() == digest
    assert (coordinator.backup_root/session['migration_id']/'manifest.json').is_file()


@contextmanager
def migration_http(coordinator):
    import json
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from live_clipper.web import LiveClipperRequestHandler, WebPaths

    root = coordinator.service_dir.parent
    handler = type('ObservedMigrationHTTP', (LiveClipperRequestHandler,), {
        'paths': WebPaths(service_dir=coordinator.service_dir, config_path=coordinator.config_path,
                         input_dir=coordinator.input_dir, output_root=coordinator.output_root,
                         state_dir=root/'state', log_dir=root/'logs'),
        'access_token': 'test-token', 'restricted_startup': 'migration_required',
    })
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    def request(route, body=None):
        req = Request(f'http://127.0.0.1:{server.server_address[1]}{route}',
                      headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'},
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_prior_empty_resource_conversion_does_not_skip_newly_imported_assignments(tmp_path):
    from live_clipper.resource_migration import migrate_resources

    coordinator, service_dir = _legacy_home(tmp_path)
    with ProjectRepository(service_dir) as repository:
        migrate_resources(repository, coordinator.settings_loader())
    accepted = execute(coordinator)
    assert finish(coordinator, accepted)['entry'] == 'completed'
    with ProjectRepository(service_dir) as repository:
        project = repository.list_projects()[0]
        refs = repository.get_config_revision(project.project_id).config['resources']
        assert refs['asr_ref'] and refs['asr_ref'] != 'legacy.asr.default'
        assert refs['analysis_ref'] and refs['analysis_ref'] != 'legacy.analysis.default'


@pytest.mark.parametrize('environment', ['empty', 'template', 'comments', 'unrelated'])
def test_old_half_complete_without_effective_credentials_can_resume(tmp_path, environment):
    from live_clipper.cli import ENV_TEMPLATE

    coordinator, original = released_half_complete(tmp_path, acknowledged=True)
    contents = {'empty': '', 'template': ENV_TEMPLATE, 'comments': '# no credentials\n\n',
                'unrelated': 'EDITOR=vim\nUNUSED_SETTING=value\n'}[environment]
    coordinator.env_path.write_text(contents)
    settings = coordinator.settings_loader()
    assert not any((settings.asr_api_key, settings.cheap_model_api_key, settings.hf_token))
    with ProjectRepository(coordinator.service_dir) as repository:
        history = repository.list_runs()
        project = repository.get_project(original.project_id)
    accepted = coordinator.retry({'request_id': 'continue-without-credentials', 'migration_id': original.migration_id,
                                  'expected_revision': original.revision})[1]['session']
    completed = finish(coordinator, accepted)
    assert completed['entry'] == 'completed'
    assert completed['session']['state'] == 'completed_attention'
    assert completed['session']['project_id'] == original.project_id
    assert coordinator.env_path.read_text() == contents
    with ProjectRepository(coordinator.service_dir) as repository:
        assert repository.list_runs() == history
        assert len(repository.list_projects()) == 1
        assert repository.get_project(original.project_id).activation_state == project.activation_state
        assert not repository.connection.execute('SELECT 1 FROM resource_revisions WHERE binding_ref IS NOT NULL').fetchone()
    assert not list((coordinator.service_dir/'resource-credentials').glob('*.env'))


@pytest.mark.parametrize('key,backend', [('ASR_API_KEY', 'openai'), ('CHEAP_MODEL_API_KEY', 'mlx_whisper'), ('HF_TOKEN', 'mlx_whisper')])
def test_unregistered_effective_credential_still_blocks_old_resume(tmp_path, key, backend):
    coordinator, original = released_half_complete(tmp_path, acknowledged=True, asr_backend=backend)
    contents = f'{key}=isolated-unregistered-key\n'
    coordinator.env_path.write_text(contents)
    with ProjectRepository(coordinator.service_dir) as repository:
        history, projects = repository.list_runs(), repository.list_projects()
    with pytest.raises(MigrationError, match='migration_credential_source_unknown'):
        coordinator.retry({'request_id': 'unknown-credential', 'migration_id': original.migration_id,
                           'expected_revision': original.revision})
    assert coordinator.env_path.read_text() == contents
    assert coordinator.snapshot()['entry'] == 'incomplete'
    with ProjectRepository(coordinator.service_dir) as repository:
        assert (repository.list_runs(), repository.list_projects()) == (history, projects)
        assert not ResourceStore(repository).list()


@pytest.mark.parametrize('key,backend', [('ASR_API_KEY', 'mlx_whisper'), ('HF_TOKEN', 'openai')])
def test_credential_unused_by_this_conversion_is_not_bound(tmp_path, key, backend):
    coordinator, original = released_half_complete(tmp_path, acknowledged=True, asr_backend=backend)
    coordinator.env_path.write_text(f'{key}=unused-isolated-key\n')
    accepted = coordinator.retry({'request_id': 'unused-credential', 'migration_id': original.migration_id,
                                  'expected_revision': original.revision})[1]['session']
    assert finish(coordinator, accepted)['session']['state'] == 'completed_attention'
    with ProjectRepository(coordinator.service_dir) as repository:
        assert not repository.connection.execute('SELECT 1 FROM resource_revisions WHERE binding_ref IS NOT NULL').fetchone()
