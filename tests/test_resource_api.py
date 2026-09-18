from live_clipper.config import Settings
from live_clipper.project_storage import ProjectRepository
from live_clipper.resource_api import ResourceAPI


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
