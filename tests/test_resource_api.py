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
