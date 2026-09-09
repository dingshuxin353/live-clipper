from live_clipper.config import load_settings
from live_clipper.config_editor import application_config
from live_clipper.web import WebPaths, handle_api_request


def test_settings_reads_and_retired_writes_preserve_nondefault_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'live-clipper.toml'
    original = b'''# Preserve this comment and formatting, too.
[paths]
work_dir = 'processing/custom'
glossary_path = 'historic-terms.json'
[scheduler]
timezone = 'Asia/Tokyo'
tick_seconds = 47
[service]
stuck_after_minutes = 77
[review_automation]
timeout_minutes = 83
[review_automation.model]
max_candidates = 59
'''
    path.write_bytes(original)
    before = load_settings(path)
    paths = WebPaths(config_path=path, service_dir=tmp_path / 'service')
    for _ in range(2):
        result = application_config(path)
        assert result == {'ok': True, 'storage': {'work_dir': str(tmp_path / 'processing/custom')}}
        assert handle_api_request('GET', '/api/config', paths)[0] == 200
        for method, route in [('POST', '/api/config'), ('GET', '/api/settings')]:
            status, _, payload = handle_api_request(method, route, paths, body={'config': {'scheduler': {'tick_seconds': 5}}})
            assert status == 410
            assert not payload['ok']
        assert path.read_bytes() == original
    after = load_settings(path)
    assert (after.scheduler, after.paths, after.service, after.review_automation) == (before.scheduler, before.paths, before.service, before.review_automation)
    assert not (tmp_path / 'processing').exists()
    assert not (tmp_path / '.application-config.lock').exists()


def test_invalid_config_fails_without_leaking_contents_or_creating_files(tmp_path):
    path = tmp_path / 'bad.toml'
    path.write_text('secret = "unfinished')
    before = path.read_bytes()
    assert application_config(path) == {'ok': False, 'message': '数据位置未获取，请检查服务后重试'}
    assert path.read_bytes() == before
