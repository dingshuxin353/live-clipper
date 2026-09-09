from concurrent.futures import ThreadPoolExecutor

import pytest

from live_clipper.config import Settings
from live_clipper.project_storage import ProjectRepository
from live_clipper.resource_execution import settings_for_snapshot
from live_clipper.resource_store import ResourceError, ResourceStore


def test_frozen_analysis_and_independent_review_do_not_read_current_defaults(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        def create(kind, model, endpoint, key, purposes):
            config = {'model': model, 'purposes': purposes}
            if endpoint:
                config['endpoint'] = endpoint
            value = {'name': model, 'kind': kind, 'config': config}
            evidence = store.record_validation(value, credential=key, results={p: {'state': 'ready'} for p in purposes})
            return store.save(value, request_id=model, credential=key, validation_id=evidence)['resource_id']
        asr = create('cloud_asr', 'asr', 'https://speech.test/v1', 'speech-key', ['asr'])
        a = create('ai', 'model-a', 'https://a.test/v1', 'a-key', ['analysis', 'review'])
        b = create('ai', 'model-b', 'https://b.test/v1', 'b-key', ['analysis', 'review'])
        snapshot = {'resource_contract': 1, 'resources': {'asr': store.freeze(asr, 'asr'), 'analysis': store.freeze(a, 'analysis'), 'review': store.freeze(b, 'review')},
                    'execution_policy': {'review_timeout_minutes': 60, 'review_prompt_template': 'default_clip_review', 'review_max_candidates': 40, 'review_retry_attempts': 2}}
    base = Settings(cheap_model_api_key='unrelated', cheap_model_name='unrelated')
    def resolve(purpose):
        with ProjectRepository(tmp_path) as repo:
            return settings_for_snapshot(repo, base, snapshot, purpose=purpose)
    with ThreadPoolExecutor() as pool:
        analysis, review = list(pool.map(resolve, ['analysis', 'review']))
    assert (analysis.cheap_model_name, analysis.cheap_model_api_base, analysis.cheap_model_api_key) == ('model-a', 'https://a.test/v1', 'a-key')
    assert (review.cheap_model_name, review.cheap_model_api_base, review.cheap_model_api_key) == ('model-b', 'https://b.test/v1', 'b-key')
    assert analysis.asr_api_key == review.asr_api_key == 'speech-key'
    assert base.cheap_model_api_key == 'unrelated'
    with ProjectRepository(tmp_path) as repo:
        with pytest.raises(ResourceError, match='original_configuration_unknown'):
            settings_for_snapshot(repo, base, {'resources': {'analysis_ref': 'legacy.analysis.default'}})


def test_request_uses_exact_resource_identity_and_no_cross_call_environment(tmp_path):
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from live_clipper.cheap_model_client import CheapModelClient

    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers['Authorization'], payload['model']))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'content': '{"ok":true}'}}]}).encode())
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = dict(os.environ)
    try:
        endpoint = f'http://127.0.0.1:{server.server_port}'
        def request(identity):
            client = CheapModelClient(Settings(cheap_model_api_base=f'{endpoint}/{identity}', cheap_model_name=identity, cheap_model_api_key=f'key-{identity}'), request_attempts=1)
            return client.complete_json('test', {'test': True})
        with ThreadPoolExecutor() as pool:
            assert list(pool.map(request, ['A', 'B'])) == [{'ok': True}, {'ok': True}]
        assert sorted(received) == [('/A/chat/completions', 'Bearer key-A', 'A'), ('/B/chat/completions', 'Bearer key-B', 'B')]
        assert dict(os.environ) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_frozen_glossary_rejects_content_replacement_before_processing(tmp_path):
    from resource_test_support import ready_project_config

    from live_clipper.config import PathsConfig
    from live_clipper.project_resources import resolve_parameter_snapshot

    glossary = tmp_path / 'terms.json'
    glossary.write_text('{"terms": []}')
    settings = Settings(paths=PathsConfig(glossary_path=glossary))
    with ProjectRepository(tmp_path / 'service') as repo:
        config = ready_project_config(repo, tmp_path / 'source', tmp_path / 'output')
        from live_clipper.project_domain import project_config_v2
        frozen = resolve_parameter_snapshot(project_config_v2(config), settings, repository=repo)
        assert settings_for_snapshot(repo, settings, frozen).paths.glossary_path == glossary
        frozen["execution_policy"]["correct_transcript"] = True
        glossary.write_text('{"terms": ["changed"]}')
        with pytest.raises(ResourceError, match='original_glossary_changed'):
            settings_for_snapshot(repo, settings, frozen)
