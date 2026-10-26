import pytest

from live_clipper.config import Settings
from live_clipper.project_storage import ProjectRepository
from live_clipper.resource_api import ResourceAPI


def test_preparation_thread_start_failure_leaves_no_running_job_or_occupation(tmp_path, monkeypatch):
    import threading

    from live_clipper import asr_models, jobs
    from live_clipper.resource_store import ResourceStore
    from live_clipper.web import WebPaths, handle_api_request

    monkeypatch.setenv('LIVE_CLIPPER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(asr_models, 'download_capacity', lambda _: {})
    paths = WebPaths(service_dir=tmp_path / 'service', config_path=tmp_path / 'config.toml')
    model = sorted(asr_models.registry_ids())[0]
    with ProjectRepository(paths.service_dir) as repo:
        resource = ResourceStore(repo).save({'name': '隔离模型', 'kind': 'local_asr',
            'config': {'model': model, 'purposes': ['asr']}}, request_id='add')

    def cannot_start(*args, **kwargs):
        raise RuntimeError('private-token /private/startup')

    monkeypatch.setattr(threading.Thread, 'start', cannot_start)
    status, _, payload = handle_api_request('POST', f"/api/resources/{resource['resource_id']}/prepare", paths,
        body={'expected_revision': 1})
    assert status == 500 and not payload['ok']
    assert 'private-token' not in repr(payload) and '/private/startup' not in repr(payload)
    assert jobs.active_job_for(paths.service_dir, model, asr_models.DOWNLOAD_JOB_KIND) is None
    with ProjectRepository(paths.service_dir) as repo:
        assert repo.connection.execute("SELECT count(*) FROM resource_tasks WHERE state='running'").fetchone()[0] == 0
        assert ResourceStore(repo).delete_preview(resource['resource_id'])['can_delete']


@pytest.mark.parametrize('saved', [False, True])
def test_validation_concurrent_replay_preserves_one_execution_and_original_result(tmp_path, monkeypatch, saved):
    from threading import Event, Thread

    from live_clipper import transcribe
    from live_clipper.web import WebPaths, handle_api_request

    monkeypatch.setenv('LIVE_CLIPPER_HOME', str(tmp_path / 'home'))
    paths = WebPaths(service_dir=tmp_path / 'service', config_path=tmp_path / 'config.toml')
    proposal = {'name': '云端识别', 'kind': 'cloud_asr', 'config': {
        'model': 'test-model', 'endpoint': 'https://example.test/v1', 'purposes': ['asr']}}
    body = {'proposal': proposal, 'credential': 'private-key', 'purposes': ['asr'], 'request_id': 'check-once'}
    if saved:
        status, _, payload = handle_api_request('POST', '/api/resources', paths,
            body={'proposal': proposal, 'credential': 'private-key', 'request_id': 'save'})
        assert status == 201
        body.update(resource_id=payload['resource']['resource_id'], revision=1)
    entered, release = Event(), Event()
    calls, responses = [], []

    def external_asr(*args):
        calls.append('asr')
        entered.set()
        assert release.wait(5)
        return {'segments': [{'start': 0, 'end': 1, 'text': '测试'}]}

    monkeypatch.setattr(transcribe, 'transcribe_audio', external_asr)
    thread = Thread(target=lambda: responses.append(handle_api_request('POST', '/api/resources/validate', paths, body=body)))
    thread.start()
    try:
        assert entered.wait(5)
        with ProjectRepository(paths.service_dir) as repo:
            assert repo.connection.execute("SELECT count(*) FROM resource_tasks WHERE state='running'").fetchone()[0] == 1
        status, _, payload = handle_api_request('POST', '/api/resources/validate', paths, body=body)
        assert status == 409 and payload['error']['code'] == 'validation_result_unknown'
        changed = {**body, 'proposal': {**proposal, 'config': {**proposal['config'], 'model': 'changed'}}}
        status, _, payload = handle_api_request('POST', '/api/resources/validate', paths, body=changed)
        assert status == 409 and payload['error']['code'] == 'request_conflict'
        if saved:
            status, _, payload = handle_api_request('GET', f"/api/resources/{body['resource_id']}/delete-preview", paths)
            assert status == 200 and not payload['preview']['can_delete']
        assert calls == ['asr']
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    status, _, result = responses[0]
    assert status == 200 and result['results']['asr']['state'] == 'ready'
    assert handle_api_request('POST', '/api/resources/validate', paths, body=body)[2] == result
    operation = handle_api_request('GET', '/api/resources/operations/check-once', paths)[2]['result']
    assert operation == {key: value for key, value in result.items() if key != 'ok'}
    assert calls == ['asr']
    with ProjectRepository(paths.service_dir) as repo:
        assert repo.connection.execute("SELECT state FROM resource_tasks").fetchall() == [('finished',)]


@pytest.mark.parametrize('operation', ['prepare', 'validate'])
def test_registration_failure_prevents_external_work(tmp_path, monkeypatch, operation):
    from live_clipper import asr_models, transcribe
    from live_clipper.web import WebPaths, handle_api_request

    monkeypatch.setenv('LIVE_CLIPPER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(asr_models, 'download_capacity', lambda _: {})

    def forbidden(*args, **kwargs):
        pytest.fail('external work started without durable occupation')

    monkeypatch.setattr(asr_models, 'download_model', forbidden)
    monkeypatch.setattr(transcribe, 'transcribe_audio', forbidden)
    paths = WebPaths(service_dir=tmp_path / 'service', config_path=tmp_path / 'config.toml')
    proposal = {'name': '登记失败', 'kind': 'local_asr', 'config': {
        'model': sorted(asr_models.registry_ids())[0], 'purposes': ['asr']}}
    _, _, payload = handle_api_request('POST', '/api/resources', paths, body={'proposal': proposal, 'request_id': 'add'})
    identifier = payload['resource']['resource_id']
    with ProjectRepository(paths.service_dir) as repo:
        repo.connection.execute("CREATE TRIGGER cannot_register BEFORE INSERT ON resource_tasks BEGIN SELECT RAISE(ABORT,'registration failed'); END")
    route = f'/api/resources/{identifier}/prepare' if operation == 'prepare' else '/api/resources/validate'
    body = {'expected_revision': 1} if operation == 'prepare' else {
        'proposal': proposal, 'purposes': ['asr'], 'request_id': 'check', 'resource_id': identifier, 'revision': 1}
    status, _, payload = handle_api_request('POST', route, paths, body=body)
    assert status == 500 and not payload['ok']
    with ProjectRepository(paths.service_dir) as repo:
        assert repo.connection.execute('SELECT count(*) FROM resource_tasks').fetchone()[0] == 0
        assert repo.connection.execute("SELECT count(*) FROM resource_operations WHERE request_id='check'").fetchone()[0] == 0
    assert not (paths.service_dir / 'jobs.json').exists()


def test_validation_finalization_failure_is_reported_and_keeps_occupation(tmp_path, monkeypatch):
    from live_clipper import transcribe
    from live_clipper.web import WebPaths, handle_api_request

    paths = WebPaths(service_dir=tmp_path / 'service', config_path=tmp_path / 'config.toml')
    proposal = {'name': '云端识别', 'kind': 'cloud_asr', 'config': {
        'model': 'test-model', 'endpoint': 'https://example.test/v1', 'purposes': ['asr']}}
    _, _, payload = handle_api_request('POST', '/api/resources', paths,
        body={'proposal': proposal, 'credential': 'private-key', 'request_id': 'save'})
    identifier = payload['resource']['resource_id']
    with ProjectRepository(paths.service_dir) as repo:
        repo.connection.execute("CREATE TRIGGER cannot_finish BEFORE UPDATE OF state ON resource_tasks WHEN NEW.state='finished' BEGIN SELECT RAISE(ABORT,'private-key /private/database'); END")
    monkeypatch.setattr(transcribe, 'transcribe_audio', lambda *_: {'segments': [{'start': 0, 'end': 1, 'text': '测试'}]})
    body = {'proposal': proposal, 'credential': 'private-key', 'purposes': ['asr'], 'request_id': 'check',
        'resource_id': identifier, 'revision': 1}
    status, _, payload = handle_api_request('POST', '/api/resources/validate', paths, body=body)
    assert status == 500 and not payload['ok']
    assert 'private-key' not in repr(payload) and '/private/database' not in repr(payload)
    preview = handle_api_request('GET', f'/api/resources/{identifier}/delete-preview', paths)[2]['preview']
    assert not preview['can_delete'] and len(preview['tasks']) == 1
    operation = handle_api_request('GET', '/api/resources/operations/check', paths)[2]['result']
    assert operation['results']['asr']['state'] == 'ready'


def test_resource_api_cannot_accept_client_validation_or_disclose_credentials(tmp_path):
    with ProjectRepository(tmp_path) as repository:
        api = ResourceAPI(repository, Settings())
        proposal = {'name': '独立资源', 'kind': 'ai', 'config': {'endpoint': 'https://example.test/v1', 'model': 'a', 'purposes': ['analysis']}}
        body = {'proposal': proposal, 'credential': 'private', 'request_id': 'add'}
        status, payload = api.dispatch('POST', ['api', 'resources'], {**body, 'ready': True})
        assert status == 409
        status, payload = api.dispatch('POST', ['api', 'resources'], body)
        assert status == 201
        resource = payload['resource']
        assert resource['ready'] is False
        assert 'private' not in repr(payload)
        status, listing = api.dispatch('GET', ['api', 'resources'], {})
        assert listing['resources'][0]['resource_id'] == resource['resource_id']
        assert api.dispatch('GET', ['api', 'resources', 'operations', 'add'], {})[1]['result'] == resource


def test_model_preparation_preserves_known_failure_without_private_exception(tmp_path, monkeypatch, caplog):
    import errno

    from live_clipper import asr_models, jobs
    from live_clipper.resource_store import ResourceStore

    monkeypatch.setattr(asr_models, 'models_root', lambda: tmp_path / 'models')
    monkeypatch.setattr(asr_models, 'model_entry', lambda _: {})
    monkeypatch.setattr(asr_models, 'local_path_for', lambda _: None)
    with ProjectRepository(tmp_path / 'service') as repository:
        store = ResourceStore(repository)
        resource = store.save({'name': '隔离模型', 'kind': 'local_asr', 'config': {'model': 'test-model', 'purposes': ['asr']}}, request_id='local-model')
        api = ResourceAPI(repository, Settings())
        route = ['api', 'resources', resource['resource_id'], 'prepare']
        def no_space(_):
            raise asr_models.ModelPreparationError('insufficient_disk_space', 'private-size')
        monkeypatch.setattr(asr_models, 'download_capacity', no_space)
        status, result = api.dispatch('POST', route, {'expected_revision': 1})
        assert status == 409 and result['error']['code'] == 'insufficient_disk_space'
        monkeypatch.setattr(asr_models, 'download_capacity', lambda _: {})
        def denied(*_args, **_kwargs):
            raise PermissionError(errno.EACCES, 'private-key', '/private/path')
        monkeypatch.setattr(asr_models, 'download_model', denied)
        work = []
        monkeypatch.setattr(jobs, 'start_job', lambda _root, **kwargs: work.append(kwargs['fn']) or {'id': 'isolated-job'})
        status, result = api.dispatch('POST', route, {'expected_revision': 1})
        failure = work[0]()
        assert status == 202 and failure['code'] == 'model_directory_unwritable'
        assert failure['diagnostic_id'] in caplog.text
        assert 'private-key' not in repr(result) + caplog.text
        assert '/private/path' not in repr(result) + caplog.text
