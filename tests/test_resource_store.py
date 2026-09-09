import pytest

from live_clipper.project_storage import ProjectRepository
from live_clipper.resource_store import ResourceError, ResourceStore


def proposal(model='model-a', endpoint='https://example.test/v1'):
    return {'name': '分析', 'kind': 'ai', 'config': {
        'model': model, 'endpoint': endpoint, 'provider': 'custom',
        'purposes': ['analysis', 'review'],
    }}


def test_revision_commit_and_credentials_are_isolated(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        a = store.save(proposal(), request_id='add-a', credential='private-a')
        assert not a['ready']
        assert store.save(proposal(), request_id='add-a', credential='private-a') == a
        b = store.save(proposal('model-b'), request_id='add-b', credential='private-b')
        assert a['resource_id'] != b['resource_id']
        assert 'private-a' not in repr(store.list())
        assert store.credential(a['resource_id'], 1) == 'private-a'
        assert store.credential(b['resource_id'], 1) == 'private-b'
        changed = store.save(proposal('new-model'), request_id='edit-a', resource_id=a['resource_id'], expected_revision=1)
        assert changed['revision'] == 2
        assert store.get(a['resource_id'], 1)['config']['model'] == 'model-a'
        with pytest.raises(ResourceError, match='revision_conflict'):
            store.save(proposal(), request_id='stale', resource_id=a['resource_id'], expected_revision=1)
    with ProjectRepository(tmp_path) as repo:
        assert ResourceStore(repo).get(a['resource_id'])['revision'] == 2
    assert b'private-a' not in (tmp_path / 'venus.sqlite3').read_bytes()
    for path in (tmp_path / 'resource-credentials').glob('*.env'):
        assert path.stat().st_mode & 0o077 == 0


def test_target_change_requires_explicit_credential_and_request_conflicts(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        a = store.save(proposal(), request_id='a', credential='private-a')
        with pytest.raises(ResourceError, match='request_conflict'):
            store.save(proposal('different'), request_id='a', credential='private-a')
        changed = store.save(proposal(endpoint='https://different.test/v1'), request_id='b', resource_id=a['resource_id'], expected_revision=1)
        assert changed['has_credential'] is False
        assert store.credential(a['resource_id'], 1) == 'private-a'
        assert store.credential(a['resource_id'], 2) is None
        with pytest.raises(ResourceError):
            store.save({**proposal(), 'config': {**proposal()['config'], 'api_key': 'unsafe'}}, request_id='bad')


def test_validation_is_bound_to_content_and_cannot_destroy_ready_revision(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        evidence = store.record_validation(proposal(), credential='a', results={'analysis': {'state': 'ready'}, 'review': {'state': 'ready'}})
        a = store.save(proposal(), request_id='a', credential='a', validation_id=evidence)
        assert a['ready']
        with pytest.raises(ResourceError, match='validation_changed'):
            store.save(proposal('changed'), request_id='b', credential='a', validation_id=evidence)
        with pytest.raises(ResourceError, match='validation_required'):
            store.save(proposal('changed'), request_id='c', resource_id=a['resource_id'], expected_revision=1)
        renamed = store.save({**proposal(), 'name': '新名称'}, request_id='rename', resource_id=a['resource_id'], expected_revision=1)
        assert renamed['ready']
        assert renamed['config'] == a['config']


def test_delete_tombstone_and_idempotency(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        a = store.save(proposal(), request_id='a', credential='a')
        assert store.delete_preview(a['resource_id'])['can_delete']
        result = store.delete(a['resource_id'], request_id='delete', expected_revision=1)
        assert result['deleted']
        assert store.list() == []
        assert store.delete(a['resource_id'], request_id='delete', expected_revision=1) == result
        assert store.get(a['resource_id'], 1)['deleted']
        assert store.credential(a['resource_id'], 1) is None
        b = store.save(proposal(), request_id='new')
        assert b['resource_id'] != a['resource_id']
        with pytest.raises(ResourceError, match='resource_deleted'):
            store.save(proposal(), request_id='revive', resource_id=a['resource_id'], expected_revision=1)


def test_repair_old_revision_preserves_current_model_and_records_binding(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        evidence = store.record_validation(proposal(), credential='a', results={'analysis': {'state': 'ready'}, 'review': {'state': 'ready'}})
        a = store.save(proposal(), request_id='a', credential='a', validation_id=evidence)
        original = store.freeze(a['resource_id'], 'analysis')
        changed = proposal('new-model')
        new_evidence = store.record_validation(changed, credential='a', results={'analysis': {'state': 'ready'}, 'review': {'state': 'ready'}})
        store.save(changed, resource_id=a['resource_id'], expected_revision=1, request_id='edit', validation_id=new_evidence)
        repair_evidence = store.record_validation(proposal(), credential='repaired', results={'analysis': {'state': 'ready'}, 'review': {'state': 'ready'}})
        repaired = store.repair(a['resource_id'], 1, credential='repaired', validation_id=repair_evidence, request_id='repair', confirm_same_account=True)
        binding, key = store.resolved_binding(original)
        assert key == 'repaired'
        assert binding['config']['model'] == 'model-a'
        assert binding['binding_ref'] == repaired['binding_ref']
        assert store.get(a['resource_id'])['config']['model'] == 'new-model'
        assert store.credential(a['resource_id'], 2) == 'a'
        assert 'repaired' not in repr(original)


def test_migration_is_atomic_restartable_and_does_not_validate_or_change_activation(tmp_path):
    from live_clipper.config import ReviewAutomationConfig, Settings
    from live_clipper.project_domain import default_project_config, project_config_v2
    from live_clipper.resource_migration import migrate_resources

    with ProjectRepository(tmp_path) as repo:
        config = project_config_v2(default_project_config(tmp_path / 'source', tmp_path / 'output'))
        config['resources'].update(asr_ref='legacy.asr.default', analysis_ref='legacy.analysis.default', review_ref='legacy.analysis.default')
        project = repo.create_project('existing', config, activation_state='paused')
        settings = Settings(cheap_model_api_key='old-private-key', review_automation=ReviewAutomationConfig(mode='model'))

        def crash(stage):
            if stage == 'after_projects':
                raise RuntimeError('interrupted')

        with pytest.raises(RuntimeError, match='interrupted'):
            migrate_resources(repo, settings, fault=crash)
        assert ResourceStore(repo).list() == []
        assert repo.get_config_revision(project.project_id).revision == 1
        migrate_resources(repo, settings)
        refs = repo.get_config_revision(project.project_id).config['resources']
        assert refs['review_ref'] == 'reuse_analysis'
        assert refs['analysis_ref'] != 'legacy.analysis.default'
        assert repo.get_project(project.project_id).activation_state == 'paused'
        assert not any(r['ready'] for r in ResourceStore(repo).list())
        migrate_resources(repo, settings)
        assert len(ResourceStore(repo).list()) == 2
        assert repo.get_config_revision(project.project_id).revision == 2
        assert repo.get_config_revision(project.project_id, 1).config == config
        assert (tmp_path / 'resource-migration-backup.sqlite3').is_file()


@pytest.mark.parametrize('mode,provider,expected', [('model', 'codex_cli', 'reuse_analysis'), ('local_agent', 'codex_cli', ''), ('local_agent', 'claude_code', 'agent')])
def test_migration_uses_effective_agent_mode(tmp_path, mode, provider, expected):
    from live_clipper.config import ReviewAutomationConfig, ReviewAutomationLocalAgentConfig, Settings
    from live_clipper.project_domain import default_project_config, project_config_v2
    from live_clipper.resource_migration import migrate_resources

    with ProjectRepository(tmp_path) as repo:
        config = project_config_v2(default_project_config(tmp_path/'source', tmp_path/'output'))
        config['resources'].update(asr_ref='legacy.asr.default', analysis_ref='legacy.analysis.default', review_ref='legacy.analysis.default')
        project = repo.create_project('existing', config)
        settings = Settings(review_automation=ReviewAutomationConfig(mode=mode, local_agent=ReviewAutomationLocalAgentConfig(provider=provider)))
        migrate_resources(repo, settings)
        ref = repo.get_config_revision(project.project_id).config['resources']['review_ref']
        if expected == 'agent':
            assert ResourceStore(repo).get(ref)['kind'] == 'local_agent'
        else:
            assert ref == expected


def test_successful_probe_can_promote_unchanged_pending_revision(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        pending = store.save(proposal(), request_id='pending', credential='key')
        evidence = store.record_validation(proposal(), credential='key', resource_id=pending['resource_id'], revision=1,
            results={'analysis': {'state': 'ready'}, 'review': {'state': 'ready'}})
        saved = store.save(proposal(), request_id='ready', resource_id=pending['resource_id'], expected_revision=1, validation_id=evidence)
        assert saved['ready'] and saved['revision'] == 2
        assert not store.get(saved['resource_id'], 1)['ready']


def test_delete_preview_is_not_authority_after_a_new_project_reference(tmp_path):
    from live_clipper.config import Settings
    from live_clipper.project_domain import default_project_config, project_config_v2
    from live_clipper.resource_migration import migrate_resources

    with ProjectRepository(tmp_path) as repo:
        migrate_resources(repo, Settings())
        store = ResourceStore(repo)
        resource = store.save(proposal(), request_id='resource')
        assert store.delete_preview(resource['resource_id'])['can_delete']
        config = project_config_v2(default_project_config(tmp_path/'source', tmp_path/'output'))
        config['resources']['analysis_ref'] = resource['resource_id']
        repo.create_project('reference added later', config, activation_state='inactive')
        with pytest.raises(ResourceError, match='resource_in_use'):
            store.delete(resource['resource_id'], request_id='delete', expected_revision=1)
        assert not store.get(resource['resource_id'])['deleted']


def test_validation_operation_replay_never_calls_provider_twice(tmp_path, monkeypatch):
    from live_clipper import resource_validation

    calls = []
    monkeypatch.setattr(resource_validation, 'validate_analysis', lambda settings: calls.append(settings.cheap_model_name))
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        kwargs = {'credential': 'key', 'purposes': ['analysis'], 'request_id': 'explicit-test'}
        result = resource_validation.validate_resource(store, proposal(), **kwargs)
        assert resource_validation.validate_resource(store, proposal(), **kwargs) == result
        assert calls == ['model-a']
        with pytest.raises(ResourceError, match='request_conflict'):
            resource_validation.validate_resource(store, proposal('another-model'), **kwargs)


def test_partial_credential_cleanup_is_durable_and_retryable(tmp_path, monkeypatch):
    from pathlib import Path

    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        resource = store.save(proposal(), request_id='add', credential='private')
        original = Path.unlink
        def fail_credential(path, *args, **kwargs):
            if path.suffix == '.env':
                raise PermissionError('unit-test permission failure')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', fail_credential)
        deleted = store.delete(resource['resource_id'], request_id='delete', expected_revision=1)
        assert deleted['deleted'] and deleted['cleanup_state'] == 'partial'
        assert store.list() == []
    monkeypatch.setattr(Path, 'unlink', original)
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        assert store.operation_result('delete')['cleanup_state'] == 'partial'
        assert store.cleanup('delete')['cleanup_state'] == 'completed'
        assert list((tmp_path/'resource-credentials').glob('*.env')) == []


def test_migration_preserves_named_project_and_converts_only_saved_onboarding_choice(tmp_path):
    from resource_test_support import assign_test_resources

    from live_clipper.config import Settings
    from live_clipper.project_domain import default_project_config
    from live_clipper.resource_migration import migrate_resources

    with ProjectRepository(tmp_path / 'named') as repo:
        config = assign_test_resources(repo, default_project_config(tmp_path/'source', tmp_path/'output'))
        project = repo.create_project('named', config)
        migrate_resources(repo, Settings())
        assert repo.get_config_revision(project.project_id).config == config
        assert repo.get_config_revision(project.project_id).revision == 1
        assert len(ResourceStore(repo).list()) == 2
    with ProjectRepository(tmp_path / 'onboarding') as repo:
        repo.connection.execute("INSERT OR REPLACE INTO system_state VALUES('data_mode','projects','2026-01-01T00:00:00Z')")
        repo.connection.commit()
        repo.begin_first_run_session()
        repo.update_first_run_draft(expected_revision=1, current_step='ai', patch={'ai': {'model': 'saved-model', 'api_base': 'https://saved.test/v1'}})
        settings = Settings(cheap_model_api_key='saved-key', cheap_model_name='saved-model', cheap_model_api_base='https://saved.test/v1')
        migrate_resources(repo, settings)
        resources = ResourceStore(repo).list()
        assert len(resources) == 1 and resources[0]['kind'] == 'ai' and not resources[0]['ready']
        assert repo.get_first_run_session().draft['ai'] == {'resource_id': resources[0]['resource_id']}
        assert repo.get_first_run_session().current_step == 'ai'


def test_failed_health_probe_cannot_remove_the_last_ready_revision_guard(tmp_path):
    with ProjectRepository(tmp_path) as repo:
        store = ResourceStore(repo)
        proof = store.record_validation(proposal(), credential='key', results={p: {'state': 'ready'} for p in ['analysis', 'review']})
        resource = store.save(proposal(), request_id='ready', credential='key', validation_id=proof)
        failed = store.record_validation(proposal(), credential='key', resource_id=resource['resource_id'], revision=1,
            results={p: {'state': 'needs_repair', 'code': 'result_unknown'} for p in ['analysis', 'review']})
        assert not store.get(resource['resource_id'])['ready']
        with pytest.raises(ResourceError, match='validation_required'):
            store.save(proposal('untested-model'), resource_id=resource['resource_id'], expected_revision=1, request_id='replace')
        with pytest.raises(ResourceError, match='validation_required'):
            store.save(proposal(), resource_id=resource['resource_id'], expected_revision=1, request_id='failed-proof', validation_id=failed)
        assert store.get(resource['resource_id'])['revision'] == 1


def test_migration_backup_is_standalone_without_live_wal_or_connection(tmp_path):
    import shutil
    import sqlite3
    from contextlib import closing

    from live_clipper.config import Settings
    from live_clipper.resource_migration import migrate_resources

    with ProjectRepository(tmp_path / 'service') as repo:
        repo.connection.execute("INSERT INTO system_state VALUES('backup-sentinel','before-migration','2026-09-07T00:00:00Z')")
        repo.connection.commit()
        def stop(stage):
            if stage == 'after_backup':
                raise RuntimeError('interrupted after durable backup')
        with pytest.raises(RuntimeError, match='durable backup'):
            migrate_resources(repo, Settings(), fault=stop)
        copy = tmp_path / 'standalone.sqlite3'
        shutil.copyfile(repo.service_dir / 'resource-migration-backup.sqlite3', copy)
        with closing(sqlite3.connect(copy.as_uri() + '?immutable=1', uri=True)) as backup:
            assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert backup.execute("SELECT value FROM system_state WHERE key='backup-sentinel'").fetchone()[0] == 'before-migration'
        assert list(repo.service_dir.glob('.resource-backup-*')) == []
