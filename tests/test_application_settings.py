from live_clipper.config import write_default_config
from live_clipper.config_editor import application_config, save_application_config


def test_application_write_preserves_resource_migration_input_and_rejects_conflicts(tmp_path):
    import tomllib
    from copy import deepcopy

    path = tmp_path / 'live-clipper.toml'
    write_default_config(path)
    original = tomllib.loads(path.read_text())
    current = application_config(path)
    draft = deepcopy(current['config'])
    draft['review_automation_model']['max_candidates'] = 45
    result = save_application_config(path, {'config': draft, 'expected_revision': current['revision']})
    assert result['ok']
    saved = tomllib.loads(path.read_text())
    assert saved['llm'] == original['llm']
    assert saved['asr'] == original['asr']
    assert saved['review_automation']['model']['max_candidates'] == 45
    assert not save_application_config(path, {'config': draft, 'expected_revision': current['revision']})['ok']
    rejected = deepcopy(result['config'])
    rejected['llm'] = {'model': 'must-not-be-written'}
    before = path.read_bytes()
    assert not save_application_config(path, {'config': rejected, 'expected_revision': result['revision']})['ok']
    assert before == path.read_bytes()
