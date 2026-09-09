"""Stored resources for isolated unit tests; these are not live-provider acceptance evidence."""
from copy import deepcopy

from live_clipper.project_domain import default_project_config
from live_clipper.resource_store import ResourceStore


def assign_test_resources(repository, config):
    store = ResourceStore(repository)
    result = deepcopy(config)
    for kind, field, purposes in [('cloud_asr', 'asr_ref', ['asr']), ('ai', 'analysis_ref', ['analysis', 'review'])]:
        name = 'unit-' + kind
        existing = next((r for r in store.list() if r['name'] == name), None)
        if existing is None:
            proposal = {'name': name, 'kind': kind, 'config': {'endpoint': 'https://unit.invalid/v1', 'model': name, 'purposes': purposes, **({'provider': 'custom'} if kind == 'ai' else {})}}
            evidence = store.record_validation(proposal, credential='unit-only-key', results={p: {'state': 'ready'} for p in purposes})
            existing = store.save(proposal, request_id=name, credential='unit-only-key', validation_id=evidence)
        result['resources'][field] = existing['resource_id']
    if 'review_ref' in result['resources']:
        result['resources']['review_ref'] = 'reuse_analysis'
    return result


def ready_project_config(repository, source, output):
    return assign_test_resources(repository, default_project_config(source, output))


def onboarding_resource_patch(coordinator):
    with coordinator._repo() as repo:
        config = assign_test_resources(repo, default_project_config('/source', '/output'))
        return {'asr': {'resource_id': config['resources']['asr_ref']}, 'ai': {'resource_id': config['resources']['analysis_ref']}}


def bind_cli_test_run(monkeypatch, tmp_path, settings, *, correction=True, refine=False, top_n=25):
    """Freeze explicit unit-test resources before invoking the real CLI resolver."""
    import json

    from live_clipper import asr_models
    from live_clipper.project_domain import project_config_v2
    from live_clipper.project_resources import resolve_parameter_snapshot
    from live_clipper.project_storage import ProjectRepository

    model_dir = tmp_path / 'unit-model'
    model_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(asr_models, 'install_dir', lambda _model: model_dir)
    monkeypatch.setattr(asr_models, 'local_path_for', lambda _model: model_dir)
    service_dir = tmp_path / 'cli-service'
    with ProjectRepository(service_dir) as repo:
        store = ResourceStore(repo)
        config = project_config_v2(default_project_config(tmp_path, tmp_path/'output'))
        definitions = [
            ('asr_ref', 'cloud_asr' if settings.asr_backend == 'openai' else 'local_asr',
             {'model': settings.asr_model, 'language': settings.asr_language, 'purposes': ['asr'], **({'endpoint': settings.asr_api_base} if settings.asr_backend == 'openai' else {})}, settings.asr_api_key),
            ('analysis_ref', 'ai', {'model': settings.cheap_model_name, 'endpoint': settings.cheap_model_api_base, 'provider': 'custom', 'purposes': ['analysis', 'review']}, settings.cheap_model_api_key),
        ]
        for field, kind, value, credential in definitions:
            proposal = {'name': field, 'kind': kind, 'config': value}
            evidence = store.record_validation(proposal, credential=credential, results={p: {'state': 'ready'} for p in value['purposes']})
            resource = store.save(proposal, request_id=field, credential=credential, validation_id=evidence)
            config['resources'][field] = resource['resource_id']
        config['resources']['review_ref'] = 'reuse_analysis'
        project = repo.create_project('CLI unit run', config)
        snapshot = resolve_parameter_snapshot(config, settings, repository=repo)
        snapshot['execution_policy'].update(correct_transcript=correction, refine=refine, refine_top_n=top_n)
        run = repo.create_normal_run(project_id=project.project_id, content_id='unit-video', trigger_source='manual', first_seen_path=str(tmp_path/'source.mp4'), latest_seen_path=str(tmp_path/'source.mp4'), parameter_snapshot=snapshot).run
    monkeypatch.setenv('LIVE_CLIPPER_PROJECT_RUN', json.dumps([str(service_dir), run.run_id]))
