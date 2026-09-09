from __future__ import annotations

import json

from test_project_result_api_v2 import result_api_fixture


def test_repair_context_is_issue_bound_to_the_original_revision_without_key(tmp_path):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    frozen = run.parameter_snapshot['resources']['review']
    identifier = frozen['resource_id']
    issue = repository.discover_issue(issue_code='ai_resource_unavailable', category='resource', scope_type='run',
        project_id=project.project_id, run_id=run.run_id, issue_group_key='resource:ai', root_cause_ref=identifier, recovery_capability='continue_run')
    from live_clipper.resource_store import ResourceStore
    store = ResourceStore(repository)
    current = store.get(identifier)
    changed = {'name': current['name'], 'kind': current['kind'], 'config': {**current['config'], 'model': 'new-project-model'}}
    proof = store.record_validation(changed, credential='unit-only-key', results={p: {'state': 'ready'} for p in changed['config']['purposes']})
    store.save(changed, resource_id=identifier, expected_revision=1, request_id='change-current', validation_id=proof)
    assert api.issue_dto(issue)['repair_resource_id'] == identifier
    status, response = api.handle('GET', f'/api/resources/{identifier}/repair-context?issue_id={issue.issue_id}')
    assert status == 200
    assert response['repair_context']['revision'] == frozen['revision']
    assert response['repair_context']['model'] == frozen['config']['model']
    assert 'unit-only-key' not in json.dumps(response)
    status, _ = api.handle('GET', f'/api/resources/unrelated/repair-context?issue_id={issue.issue_id}')
    assert status == 409


def test_global_connection_write_and_probe_routes_are_retired(tmp_path):
    repository, api, _project, _run, _path, _media = result_api_fixture(tmp_path)
    before = '\n'.join(repository.connection.iterdump())
    for method, route in [('PATCH', 'connection'), ('POST', 'connection-test')]:
        status, response = api.handle(method, f'/api/resources/legacy.analysis.default/{route}', body={'api_key': 'must-not-persist'})
        assert status == 410 and response['error']['code'] == 'resource_route_retired'
    assert '\n'.join(repository.connection.iterdump()) == before
