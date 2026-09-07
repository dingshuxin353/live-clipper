"""One recoverable transaction converts effective settings and current project references."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing

from .config import Settings
from .project_domain import normalize_utc, project_config_v2, stable_json
from .project_storage import ProjectRepository
from .resource_store import ResourceStore, encoded, fingerprint

STATE_KEY = 'named_resources_migration'


def migrate_resources(repository: ProjectRepository, settings: Settings, *, fault: Callable[[str], None] | None = None) -> None:
    repository.service_dir.mkdir(parents=True, exist_ok=True)
    with (repository.service_dir / '.resource-migration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _migrate_locked(repository, settings, fault=fault)


def _migrate_locked(repository: ProjectRepository, settings: Settings, *, fault: Callable[[str], None] | None) -> None:
    db = repository.connection
    state = db.execute('SELECT value FROM system_state WHERE key=?', (STATE_KEY,)).fetchone()
    if state and state[0] == 'completed':
        return
    now = normalize_utc()
    if state is None:
        with repository.transaction():
            db.execute('INSERT OR IGNORE INTO system_state VALUES(?,?,?)', (STATE_KEY, 'backing_up', now))
    backup_path = repository.service_dir / 'resource-migration-backup.sqlite3'
    if not backup_path.exists():
        from pathlib import Path

        descriptor, name = tempfile.mkstemp(dir=repository.service_dir, prefix='.resource-backup-')
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as backup:
                db.backup(backup)
                backup.execute("PRAGMA journal_mode=DELETE")
                if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('resource_backup_invalid')
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.link(temporary, backup_path)
            directory = os.open(repository.service_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    if fault:
        fault('after_backup')
    store = ResourceStore(repository)
    with repository.transaction():
        current = db.execute('SELECT value FROM system_state WHERE key=?', (STATE_KEY,)).fetchone()
        if current[0] == 'completed':
            return
        db.execute('UPDATE system_state SET value=? WHERE key=?', ('migrating', STATE_KEY))
        projects = repository.list_projects()
        session = repository.get_first_run_session()
        draft = session.draft if session else {}
        old_asr, old_ai = draft.get('asr', {}), draft.get('ai', {})
        draft_asr_matches = not old_asr.get('resource_id') and (
            old_asr.get('mode') == 'local' and old_asr.get('local_model_id') == settings.asr.model
            or old_asr.get('mode') == 'cloud' and old_asr.get('model') == settings.asr.model
            and old_asr.get('api_base') == settings.asr.api_base and bool(settings.asr_api_key)
        )
        draft_ai_matches = not old_ai.get('resource_id') and bool(settings.cheap_model_api_key) and old_ai.get('model') == settings.llm.model and old_ai.get('api_base') == settings.llm.api_base
        legacy_projects = []
        for project in projects:
            revision = repository.get_config_revision(project.project_id)
            refs = revision.config['resources']
            if any(value and value not in {'reuse_analysis'} and not db.execute('SELECT 1 FROM resources WHERE resource_id=?', (value,)).fetchone() for value in refs.values()):
                legacy_projects.append(project)
        # Only effective old assignments are migrated; templates and new resource IDs are preserved.
        if legacy_projects or draft_asr_matches or draft_ai_matches:
            legacy_refs = [repository.get_config_revision(p.project_id).config['resources'] for p in legacy_projects]
            needs_asr = draft_asr_matches or any(r['asr_ref'] == 'legacy.asr.default' for r in legacy_refs)
            needs_analysis = draft_ai_matches or any('legacy.analysis.default' in r.values() for r in legacy_refs)
            llm = settings.llm
            review = settings.review_automation
            analysis_config = {'endpoint': llm.api_base, 'model': llm.model, 'provider': 'custom',
                               'purposes': ['analysis', 'review'], 'timeout_seconds': llm.timeout_seconds,
                               'request_attempts': llm.request_attempts, 'retry_delay_seconds': llm.retry_delay_seconds,
                               'temperature': review.model.temperature, 'max_tokens': review.model.max_tokens}
            asr_config = {'model': settings.asr.model, 'language': settings.asr.language, 'purposes': ['asr']}
            if settings.asr.backend == 'openai':
                asr_kind = 'cloud_asr'
                asr_config['endpoint'] = settings.asr.api_base
                asr_credential = settings.asr_api_key
            elif settings.asr.backend == 'mlx_whisper':
                asr_kind = 'local_asr'
                asr_config['model_source'] = settings.asr.model_source
                asr_credential = settings.hf_token
            elif needs_asr:
                raise ValueError('legacy_asr_backend_unknown')

            def add(kind, name, config, credential):
                # Stable migration identity uses actual settings and credential identity, never display name.
                identifier = fingerprint(['settings-migration-v1', kind, config, credential])[:32]
                if not db.execute('SELECT 1 FROM resources WHERE resource_id=?', (identifier,)).fetchone():
                    binding = store._write_credential(credential) if credential else None
                    db.execute('INSERT INTO resources VALUES(?,?,?,?,?,NULL)', (identifier, name, kind, 1, now))
                    db.execute('INSERT INTO resource_revisions VALUES(?,?,?,?,?,?)', (identifier, 1, encoded(config), binding, '{}', now))
                return identifier

            asr_id = add(asr_kind, '原语音识别配置', asr_config, asr_credential) if needs_asr else ''
            analysis_id = add('ai', '原内容分析配置', analysis_config, settings.cheap_model_api_key) if needs_analysis else ''
            review_id = 'reuse_analysis'
            if settings.legacy_review_removed:
                review_id = ''
            elif any(r.get('review_ref', r['analysis_ref']) == 'legacy.analysis.default' for r in legacy_refs) and review.mode == 'local_agent':
                if review.local_agent.provider == 'claude_code':
                    review_id = add('local_agent', 'Claude Code 审阅', {'purposes': ['review'], 'command_timeout_minutes': review.local_agent.command_timeout_minutes, 'include_review_package_inline': review.local_agent.include_review_package_inline}, None)
                else:
                    review_id = ''
            elif any(r.get('review_ref', r['analysis_ref']) == 'legacy.analysis.default' for r in legacy_refs) and review.model.model and review.model.model != llm.model:
                review_id = add('ai', '原独立审阅配置', {**analysis_config, 'model': review.model.model}, settings.cheap_model_api_key)
            if fault:
                fault('after_resources')
            for project in legacy_projects:
                revision = repository.get_config_revision(project.project_id)
                config = revision.config if revision.schema_version == 2 else project_config_v2(revision.config)
                refs = config['resources']
                # Unknown old references stay unassigned. Historical revisions and Run snapshots stay untouched.
                mapped_asr = asr_id if refs['asr_ref'] == 'legacy.asr.default' else refs['asr_ref'] if db.execute('SELECT 1 FROM resources WHERE resource_id=?', (refs['asr_ref'],)).fetchone() else ''
                mapped_analysis = analysis_id if refs['analysis_ref'] == 'legacy.analysis.default' else refs['analysis_ref'] if db.execute('SELECT 1 FROM resources WHERE resource_id=?', (refs['analysis_ref'],)).fetchone() else ''
                mapped_review = review_id if refs.get('review_ref') == 'legacy.analysis.default' else refs.get('review_ref', '') if refs.get('review_ref') == 'reuse_analysis' or db.execute('SELECT 1 FROM resources WHERE resource_id=?', (refs.get('review_ref', ''),)).fetchone() else ''
                config = {**config, 'resources': {**refs, 'asr_ref': mapped_asr, 'analysis_ref': mapped_analysis, 'review_ref': mapped_review}}
                db.execute('INSERT INTO project_config_revisions VALUES(?,?,?,?,?)', (project.project_id, revision.revision + 1, stable_json(config), 2, now))
                db.execute('UPDATE projects SET current_config_revision=?,updated_at=? WHERE project_id=?', (revision.revision + 1, now, project.project_id))
                db.execute("UPDATE project_runtime SET readiness_state='blocked',failure_code='resource_validation_required',failure_summary='处理资源待验证；历史记录已保留' WHERE project_id=?", (project.project_id,))
            if session and (draft_asr_matches or draft_ai_matches):
                converted = json.loads(encoded(draft))
                if draft_asr_matches:
                    converted['asr'] = {'resource_id': asr_id}
                if draft_ai_matches:
                    converted['ai'] = {'resource_id': analysis_id}
                db.execute('UPDATE first_run_sessions SET draft_json=?,revision=revision+1,updated_at=? WHERE session_id=?', (encoded(converted), now, session.session_id))
            if fault:
                fault('after_projects')
        db.execute('UPDATE system_state SET value=?,updated_at=? WHERE key=?', ('completed', now, STATE_KEY))
