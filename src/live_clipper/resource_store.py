"""Named resources share the project database's write boundary; secrets never enter it."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .onboarding_resources import normalize_api_base
from .project_domain import normalize_utc
from .project_storage import ProjectRepository

KINDS = {'local_asr': {'asr'}, 'cloud_asr': {'asr'}, 'ai': {'analysis', 'review'}, 'local_agent': {'review'}}
SCHEMA = """
CREATE TABLE resources (
 resource_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
 current_revision INTEGER NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE resource_revisions (
 resource_id TEXT NOT NULL REFERENCES resources(resource_id), revision INTEGER NOT NULL,
 config_json TEXT NOT NULL, binding_ref TEXT, validation_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(resource_id, revision)
);
CREATE TRIGGER resource_revision_immutable BEFORE UPDATE ON resource_revisions
BEGIN SELECT RAISE(ABORT, 'resource revisions are immutable'); END;
CREATE TABLE resource_validations (
 validation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, results_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE resource_operations (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE resource_repairs (
 repair_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, revision INTEGER NOT NULL,
 binding_ref TEXT, validation_json TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(resource_id, revision) REFERENCES resource_revisions(resource_id, revision)
);
CREATE TABLE resource_health (
 resource_id TEXT NOT NULL, revision INTEGER NOT NULL, results_json TEXT NOT NULL,
 PRIMARY KEY(resource_id, revision), FOREIGN KEY(resource_id,revision) REFERENCES resource_revisions(resource_id,revision)
);
CREATE TABLE resource_tasks (
 task_id TEXT PRIMARY KEY, resource_id TEXT, model TEXT, state TEXT NOT NULL,
 created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TRIGGER resource_project_reference BEFORE INSERT ON project_config_revisions
WHEN EXISTS(SELECT 1 FROM system_state WHERE key='named_resources_migration' AND value='completed')
BEGIN
 SELECT CASE WHEN EXISTS (
   SELECT 1 FROM json_each(NEW.config_json,'$.resources') ref
   WHERE ref.key IN ('asr_ref','analysis_ref','review_ref') AND ref.value <> ''
   AND NOT (ref.key='review_ref' AND ref.value='reuse_analysis')
   AND NOT EXISTS (
     SELECT 1 FROM resources r JOIN resource_revisions v ON r.resource_id=v.resource_id AND r.current_revision=v.revision,
       json_each(v.config_json,'$.purposes') p
     WHERE r.resource_id=ref.value AND r.deleted_at IS NULL AND p.value=replace(ref.key,'_ref','')
   )
 ) THEN RAISE(ABORT,'incompatible_resource') END;
END;
CREATE TRIGGER resource_run_reference BEFORE INSERT ON runs
WHEN NEW.trigger_source <> 'legacy_import' AND EXISTS(SELECT 1 FROM system_state WHERE key='named_resources_migration' AND value='completed')
BEGIN
 SELECT CASE WHEN COALESCE(json_extract(NEW.parameter_snapshot_json,'$.resource_contract'),0) <> 1 OR (SELECT count(*) FROM json_each(NEW.parameter_snapshot_json,'$.resources') WHERE key IN ('asr','analysis','review')) <> 3
 THEN RAISE(ABORT,'original_configuration_unknown') END;
 SELECT CASE WHEN EXISTS (
   SELECT 1 FROM json_each(NEW.parameter_snapshot_json,'$.resources') frozen
   WHERE frozen.key IN ('asr','analysis','review') AND NOT EXISTS (
     SELECT 1 FROM resources r JOIN resource_revisions v ON r.resource_id=v.resource_id
     WHERE r.resource_id=json_extract(frozen.value,'$.resource_id')
       AND v.revision=json_extract(frozen.value,'$.revision') AND r.deleted_at IS NULL
   )
 ) THEN RAISE(ABORT,'resource_unavailable') END;
END;
CREATE TRIGGER resource_run_requeue BEFORE UPDATE OF status ON runs
WHEN NEW.status IN ('queued','processing') AND OLD.status NOT IN ('queued','processing') AND NEW.current_stage <> 'render' AND NEW.trigger_source <> 'legacy_import' AND EXISTS(SELECT 1 FROM system_state WHERE key='named_resources_migration' AND value='completed')
BEGIN
 SELECT CASE WHEN COALESCE(json_extract(NEW.parameter_snapshot_json,'$.resource_contract'),0) <> 1 OR (SELECT count(*) FROM json_each(NEW.parameter_snapshot_json,'$.resources') WHERE key IN ('asr','analysis','review')) <> 3
 THEN RAISE(ABORT,'original_configuration_unknown') END;
 SELECT CASE WHEN EXISTS (
   SELECT 1 FROM json_each(NEW.parameter_snapshot_json,'$.resources') frozen
   WHERE frozen.key IN ('asr','analysis','review') AND NOT EXISTS (
     SELECT 1 FROM resources r JOIN resource_revisions v ON r.resource_id=v.resource_id
     WHERE r.resource_id=json_extract(frozen.value,'$.resource_id')
       AND v.revision=json_extract(frozen.value,'$.revision') AND r.deleted_at IS NULL
   )
 ) THEN RAISE(ABORT,'resource_unavailable') END;
END;

"""


class ResourceError(ValueError):
    def __init__(self, code: str, *, detail: Any = None):
        self.code = code
        self.detail = detail
        super().__init__(code)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def normalize_proposal(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {'name', 'kind', 'config'}:
        raise ResourceError('invalid_resource')
    name, kind, config = value['name'], value['kind'], value['config']
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ResourceError('invalid_name')
    if kind not in KINDS or not isinstance(config, dict):
        raise ResourceError('invalid_kind')
    common = {'model', 'purposes'}
    allowed = {
        'ai': common | {'endpoint', 'provider', 'region', 'workspace', 'request_profile', 'timeout_seconds', 'request_attempts', 'retry_delay_seconds', 'temperature', 'max_tokens'},
        'cloud_asr': common | {'endpoint', 'language'},
        'local_asr': common | {'language', 'model_source'},
        'local_agent': {'purposes', 'command_timeout_minutes', 'include_review_package_inline'},
    }
    if set(config) - allowed[kind]:
        raise ResourceError('invalid_fields')
    config = dict(config)
    purposes = config.get('purposes', sorted(KINDS[kind]))
    if not isinstance(purposes, list) or not purposes or any(p not in KINDS[kind] for p in purposes):
        raise ResourceError('invalid_purposes')
    config['purposes'] = sorted(set(purposes))
    for key in ('model', 'provider', 'region', 'workspace', 'language', 'model_source'):
        if key in config and (not isinstance(config[key], str) or len(config[key]) > 512 or any(ord(c) < 32 for c in config[key])):
            raise ResourceError('invalid_fields')
    if kind in {'ai', 'cloud_asr'}:
        endpoint = config.get('endpoint', '')
        if not isinstance(endpoint, str):
            raise ResourceError('invalid_endpoint')
        if endpoint:
            try:
                config['endpoint'] = normalize_api_base(endpoint, allow_loopback=True)
            except ValueError:
                raise ResourceError('invalid_endpoint') from None
    if kind == 'ai':
        from .resource_providers import PROVIDERS, preset_endpoint

        provider = config.get('provider', '')
        config['provider'] = provider
        if provider and provider not in PROVIDERS:
            raise ResourceError('invalid_provider')
        if provider and provider != 'custom':
            try:
                target = preset_endpoint(provider, region=config.get('region', ''), workspace=config.get('workspace', ''))
            except ValueError as exc:
                if str(exc) in {'region_required', 'workspace_required'} and not config.get('endpoint'):
                    target = ''
                else:
                    raise ResourceError(str(exc)) from None
            if config.get('endpoint') and config['endpoint'] != target:
                raise ResourceError('custom_endpoint_requires_custom_provider')
            config['endpoint'] = target
        expected_profile = 'kimi-k2.6-default' if provider == 'kimi' and config.get('model', '').startswith('kimi-k2.6') else 'chat-completions-v1'
        if 'request_profile' in config and config['request_profile'] != expected_profile:
            raise ResourceError('invalid_request_profile')
        config['request_profile'] = expected_profile
    limits = {'timeout_seconds': (30, 3600), 'request_attempts': (1, 10), 'retry_delay_seconds': (0, 60), 'temperature': (0, 2), 'max_tokens': (512, 32000), 'command_timeout_minutes': (1, 240)}
    for key, (minimum, maximum) in limits.items():
        if key in config:
            number = config[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not minimum <= number <= maximum:
                raise ResourceError('invalid_parameters')
            if key not in {'temperature', 'retry_delay_seconds'} and not isinstance(number, int):
                raise ResourceError('invalid_parameters')
    if 'include_review_package_inline' in config and not isinstance(config['include_review_package_inline'], bool):
        raise ResourceError('invalid_parameters')
    if kind == 'local_asr' and config.get('model_source', 'modelscope') not in {'modelscope', 'huggingface'}:
        raise ResourceError('invalid_source')
    return {'name': name.strip(), 'kind': kind, 'config': config}


class ResourceStore:
    def __init__(self, repository: ProjectRepository):
        self.repository = repository
        self.db = repository.connection

    def _row(self, resource_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self.db.execute('''SELECT r.resource_id,r.name,r.kind,r.current_revision,r.created_at,r.deleted_at,
          v.revision,v.config_json,v.binding_ref,v.validation_json
          FROM resources r JOIN resource_revisions v ON r.resource_id=v.resource_id
          WHERE r.resource_id=? AND v.revision=COALESCE(?,r.current_revision)''', (resource_id, revision)).fetchone()
        if row is None:
            raise ResourceError('resource_not_found')
        result = dict(zip(('resource_id','name','kind','current_revision','created_at','deleted_at','revision','config_json','binding_ref','validation_json'), row, strict=True))
        repair = self.db.execute('''SELECT binding_ref,validation_json FROM resource_repairs
          WHERE resource_id=? AND revision=? ORDER BY rowid DESC LIMIT 1''', (resource_id, result['revision'])).fetchone()
        if repair:
            result['binding_ref'], result['validation_json'] = repair
        return result

    def get(self, resource_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self._row(resource_id, revision)
        config, validation = json.loads(row['config_json']), json.loads(row['validation_json'])
        health = self.db.execute('SELECT results_json FROM resource_health WHERE resource_id=? AND revision=?', (resource_id, row['revision'])).fetchone()
        if health:
            validation.update(json.loads(health[0]))
        if row['kind'] == 'local_asr' and validation.get('asr', {}).get('state') == 'ready':
            from . import asr_models

            model = config.get('model', '')
            path = asr_models.install_dir(model) if model in asr_models.registry_ids() else Path(model)
            if not path.is_absolute() or not path.is_dir():
                validation['asr'] = {'state': 'needs_repair', 'code': 'model_not_installed'}
        if row['kind'] in {'ai', 'cloud_asr'} and row['binding_ref'] and not self._secret_path(row['binding_ref']).is_file():
            validation = {p: {'state': 'needs_repair', 'code': 'credential_unavailable'} for p in config['purposes']}
        states = [validation.get(p, {}).get('state', 'pending') for p in config['purposes']]
        state = 'needs_repair' if 'needs_repair' in states else ('ready' if all(s == 'ready' for s in states) else 'pending')
        return {'resource_id': resource_id, 'name': row['name'], 'kind': row['kind'], 'revision': row['revision'],
                'config': config, 'validation': validation, 'state': state, 'ready': state == 'ready',
                'has_credential': bool(row['binding_ref']) and not bool(row['deleted_at']),
                'created_at': row['created_at'], 'deleted': bool(row['deleted_at'])}

    def list(self) -> list[dict[str, Any]]:
        return [self.get(row[0]) for row in self.db.execute('SELECT resource_id FROM resources WHERE deleted_at IS NULL ORDER BY rowid DESC').fetchall()]

    def _secret_path(self, binding: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', binding):
            raise ResourceError('invalid_binding')
        return self.repository.service_dir / 'resource-credentials' / f'{binding}.env'

    def _write_credential(self, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 32 for c in value):
            raise ResourceError('invalid_credential')
        binding = uuid.uuid4().hex
        path = self._secret_path(binding)
        if path.parent.is_symlink():
            raise ResourceError('invalid_credential_directory')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            # dotenv double-quoted values preserve both quotes and backslashes.
            stream.write('RESOURCE_CREDENTIAL=' + json.dumps(value, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return binding

    def credential(self, resource_id: str, revision: int) -> str | None:
        row = self._row(resource_id, revision)
        if row['deleted_at'] or not row['binding_ref']:
            return None
        path = self._secret_path(row['binding_ref'])
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        return dotenv_values(path, interpolate=False).get('RESOURCE_CREDENTIAL')

    def _effective_credential(self, proposal: dict[str, Any], credential: str | None, resource_id: str | None, revision: int | None) -> str | None:
        if credential:
            return credential
        if resource_id:
            previous = self.get(resource_id, revision)
            if previous['kind'] == proposal['kind'] and all(previous['config'].get(k) == proposal['config'].get(k) for k in ('endpoint', 'provider', 'region', 'workspace')):
                return self.credential(resource_id, previous['revision'])
        return None

    def record_validation(self, proposal: dict[str, Any], *, credential: str | None, results: dict[str, Any], resource_id: str | None = None, revision: int | None = None) -> str:
        """Internal-only: callers must execute the purpose validator before issuing evidence."""
        proposal = normalize_proposal(proposal)
        credential = self._effective_credential(proposal, credential, resource_id, revision)
        if set(results) - set(proposal['config']['purposes']):
            raise ResourceError('invalid_purposes')
        safe = {}
        for purpose, result in results.items():
            if result.get('state') not in {'ready', 'needs_repair'}:
                raise ResourceError('invalid_validation')
            code = str(result.get('code', ''))
            if not re.fullmatch(r'[a-z0-9_]{0,80}', code):
                raise ResourceError('invalid_validation')
            safe[purpose] = {'state': result['state'], 'code': code, 'checked_at': normalize_utc()}
        identity = fingerprint([proposal['kind'], proposal['config'], credential])
        identifier = uuid.uuid4().hex
        with self.repository.transaction():
            self.db.execute('INSERT INTO resource_validations VALUES(?,?,?,?)', (identifier, identity, encoded(safe), normalize_utc()))
            if resource_id:
                current = self.get(resource_id, revision)
                if current['config'] == proposal['config'] and self.credential(resource_id, current['revision']) == credential:
                    failed = {p: v for p, v in safe.items() if v['state'] == 'needs_repair'}
                    if failed:
                        self.db.execute('INSERT INTO resource_health VALUES(?,?,?) ON CONFLICT(resource_id,revision) DO UPDATE SET results_json=json_patch(resource_health.results_json,excluded.results_json)', (resource_id, current['revision'], encoded(failed)))
        return identifier

    def _operation(self, request_id: str, digest: str) -> dict[str, Any] | None:
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,120}', request_id):
            raise ResourceError('invalid_request_id')
        row = self.db.execute('SELECT fingerprint,result_json FROM resource_operations WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row[0] != digest:
                raise ResourceError('request_conflict')
            return json.loads(row[1])
        return None

    def operation_result(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute('SELECT result_json FROM resource_operations WHERE request_id=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_operation(self, request_id: str, digest: str, result: dict[str, Any]) -> None:
        self.db.execute('INSERT INTO resource_operations VALUES(?,?,?,?)', (request_id, digest, encoded(result), normalize_utc()))

    def save(self, proposal: dict[str, Any], *, request_id: str, credential: str | None = None, resource_id: str | None = None, expected_revision: int | None = None, validation_id: str | None = None) -> dict[str, Any]:
        proposal = normalize_proposal(proposal)
        digest = fingerprint(['save', proposal, credential, resource_id, expected_revision, validation_id])
        with self.repository.transaction():
            prior = self._operation(request_id, digest)
            if prior is not None:
                return prior
            old = self.get(resource_id) if resource_id else None
            if old and old['deleted']:
                raise ResourceError('resource_deleted')
            if old and expected_revision != old['revision']:
                raise ResourceError('revision_conflict', detail=old)
            if old and old['kind'] != proposal['kind']:
                raise ResourceError('resource_kind_immutable')
            effective = self._effective_credential(proposal, credential, resource_id, expected_revision)
            validation = {}
            unchanged = old and old['config'] == proposal['config'] and effective == self.credential(resource_id, old['revision'])
            if validation_id:
                evidence = self.db.execute('SELECT fingerprint,results_json FROM resource_validations WHERE validation_id=?', (validation_id,)).fetchone()
                if evidence is None or evidence[0] != fingerprint([proposal['kind'], proposal['config'], effective]):
                    raise ResourceError('validation_changed')
                validation = json.loads(evidence[1])
            elif unchanged:
                validation = old['validation']
            committed_validation = json.loads(self._row(resource_id)['validation_json']) if old else {}
            if old and any(item['state'] == 'ready' for item in committed_validation.values()) and (not unchanged or validation_id):
                if any(validation.get(p, {}).get('state') != 'ready' for p in proposal['config']['purposes']):
                    raise ResourceError('validation_required')
                required = {purpose for project in self.references(resource_id)['projects'] for purpose in project['purposes']}
                if any(validation.get(p, {}).get('state') != 'ready' for p in required):
                    raise ResourceError('referenced_capability_required')
            binding = None
            if effective:
                if old and effective == self.credential(resource_id, old['revision']):
                    binding = self._row(resource_id)['binding_ref']
                else:
                    binding = self._write_credential(effective)
            revision = old['revision'] + 1 if old else 1
            identifier = resource_id or uuid.uuid4().hex
            now = normalize_utc()
            if old:
                self.db.execute('UPDATE resources SET name=?,current_revision=? WHERE resource_id=?', (proposal['name'], revision, identifier))
            else:
                self.db.execute('INSERT INTO resources VALUES(?,?,?,?,?,NULL)', (identifier, proposal['name'], proposal['kind'], 1, now))
            self.db.execute('INSERT INTO resource_revisions VALUES(?,?,?,?,?,?)', (identifier, revision, encoded(proposal['config']), binding, encoded(validation), now))
            result = self.get(identifier)
            self._save_operation(request_id, digest, result)
            return result

    def references(self, resource_id: str, resource_revision: int | None = None) -> dict[str, Any]:
        projects, active_runs, failed_runs = [], [], []
        for project in self.repository.list_projects():
            revision = self.repository.get_config_revision(project.project_id)
            refs = revision.config['resources']
            purposes = [p for p in ('asr', 'analysis', 'review') if refs.get(p + '_ref') == resource_id or (p == 'review' and refs.get('review_ref') == 'reuse_analysis' and refs.get('analysis_ref') == resource_id)]
            if purposes and (resource_revision is None or self.get(resource_id)['revision'] == resource_revision):
                projects.append({'project_id': project.project_id, 'name': project.name, 'purposes': purposes})
        for run in self.repository.list_runs():
            refs = run.parameter_snapshot.get('resources', {})
            if any(refs.get(p + '_ref') == resource_id and (resource_revision is None or refs.get(p, {}).get('revision') == resource_revision) for p in ('asr', 'analysis', 'review')):
                item = {'run_id': run.run_id, 'project_id': run.project_id, 'status': run.status}
                if run.status in {'queued', 'processing', 'awaiting_review'}:
                    active_runs.append(item)
                elif run.status == 'failed':
                    failed_runs.append(item)
        return {'projects': projects, 'active_runs': active_runs, 'failed_runs': failed_runs}

    def delete_preview(self, resource_id: str) -> dict[str, Any]:
        resource = self.get(resource_id)
        refs = self.references(resource_id)
        tasks = self.db.execute("SELECT task_id FROM resource_tasks WHERE state='running' AND (resource_id=? OR model=?)", (resource_id, resource['config'].get('model'))).fetchall()
        model_files = {'can_clean': False, 'bytes': 0, 'shared': False, 'installed': False}
        if resource['kind'] == 'local_asr':
            from . import asr_models, jobs

            model = resource['config'].get('model')
            if model in asr_models.registry_ids():
                active = jobs.active_job_for(self.repository.service_dir, model, asr_models.DOWNLOAD_JOB_KIND)
                if active:
                    tasks.append((active['id'],))
                path = asr_models.install_dir(model)
                shared = self.db.execute("""SELECT 1 FROM resource_revisions v JOIN resources r ON r.resource_id=v.resource_id
                  WHERE r.deleted_at IS NULL AND r.resource_id<>? AND r.kind='local_asr' AND json_extract(v.config_json,'$.model')=? LIMIT 1""", (resource_id, model)).fetchone()
                managed = path.parent == asr_models.models_root() and not path.is_symlink() and not path.parent.is_symlink()
                for owned_path, manifest_name in ((path, '_install.json'), (asr_models.partial_dir(model), '_download.json')):
                    if owned_path.exists():
                        manifest = asr_models._safe_read_json(owned_path / manifest_name)
                        managed = managed and not owned_path.is_symlink() and bool(manifest and manifest.get('model_id') == model)
                installed = managed and path.is_dir()
                size = sum(p.stat().st_size for root in (path, asr_models.partial_dir(model)) if managed and root.is_dir() for p in root.rglob('*') if p.is_file() and not p.is_symlink())
                model_files = {'can_clean': bool(managed and not shared and not tasks and not refs['active_runs']), 'bytes': size,
                               'shared': bool(shared), 'installed': installed}
        return {**refs, 'tasks': [row[0] for row in tasks], 'model_files': model_files, 'resource': resource,
                'can_delete': not refs['projects'] and not refs['active_runs'] and not tasks}

    def delete(self, resource_id: str, *, request_id: str, expected_revision: int, acknowledge_failed: bool = False, clean_model: bool = False) -> dict[str, Any]:
        if not isinstance(acknowledge_failed, bool) or not isinstance(clean_model, bool):
            raise ResourceError('invalid_fields')
        digest = fingerprint(['delete', resource_id, expected_revision, acknowledge_failed, clean_model])
        with self.repository.transaction():
            previous = self._operation(request_id, digest)
            if previous is not None:
                return previous
            preview = self.delete_preview(resource_id)
            if preview['resource']['revision'] != expected_revision:
                raise ResourceError('revision_conflict')
            if preview['resource']['deleted']:
                raise ResourceError('resource_deleted')
            if not preview['can_delete']:
                raise ResourceError('resource_in_use', detail=preview)
            if preview['failed_runs'] and acknowledge_failed is not True:
                raise ResourceError('failed_runs_confirmation_required', detail=preview)
            if clean_model and not preview['model_files']['can_clean']:
                raise ResourceError('model_cleanup_unavailable')
            self.db.execute('UPDATE resources SET deleted_at=? WHERE resource_id=?', (normalize_utc(), resource_id))
            result = {'resource_id': resource_id, 'deleted': True, 'files_retained': True, 'clean_model': clean_model,
                      'cleanup_state': 'pending', 'cleanup_errors': []}
            self._save_operation(request_id, digest, result)
        return self.cleanup(request_id)

    def cleanup(self, request_id: str) -> dict[str, Any]:
        import shutil

        with self.repository.transaction():
            result = self.operation_result(request_id)
            if not result or not result.get('deleted'):
                raise ResourceError('cleanup_not_found')
            if result.get('cleanup_state') == 'completed':
                return result
            identifier = result['resource_id']
            preview = self.delete_preview(identifier)
            errors = []
            if result['clean_model']:
                if not preview['can_delete'] or not preview['model_files']['can_clean']:
                    errors.append('model_cleanup_in_use')
                else:
                    from . import asr_models

                    model = preview['resource']['config']['model']
                    try:
                        for path in (asr_models.install_dir(model), asr_models.partial_dir(model)):
                            if path.is_symlink() or path.parent.is_symlink() or path.parent != asr_models.models_root():
                                raise OSError('unmanaged_model')
                            if path.exists():
                                shutil.rmtree(path)
                        result['files_retained'] = False
                    except OSError:
                        errors.append('model_cleanup_failed')
            bindings = self.db.execute("SELECT binding_ref FROM resource_revisions WHERE resource_id=? UNION SELECT binding_ref FROM resource_repairs WHERE resource_id=?", (identifier, identifier)).fetchall()
            for (binding,) in bindings:
                if not binding:
                    continue
                shared = self.db.execute("""SELECT 1 FROM resources r WHERE r.deleted_at IS NULL AND (
                  EXISTS(SELECT 1 FROM resource_revisions v WHERE v.resource_id=r.resource_id AND v.binding_ref=?) OR
                  EXISTS(SELECT 1 FROM resource_repairs p WHERE p.resource_id=r.resource_id AND p.binding_ref=?)) LIMIT 1""", (binding, binding)).fetchone()
                if not shared:
                    try:
                        path = self._secret_path(binding)
                        if path.parent.is_symlink():
                            raise OSError('unmanaged_binding')
                        path.unlink(missing_ok=True)
                    except OSError:
                        errors.append('credential_cleanup_failed')
            result.update(cleanup_state='partial' if errors else 'completed', cleanup_errors=errors)
            self.db.execute('UPDATE resource_operations SET result_json=? WHERE request_id=?', (encoded(result), request_id))
            return result

    def repair(self, resource_id: str, revision: int, *, credential: str | None = None, validation_id: str, request_id: str, confirm_same_account: bool) -> dict[str, Any]:
        digest = fingerprint(['repair', resource_id, revision, credential, validation_id, confirm_same_account])
        with self.repository.transaction():
            previous = self._operation(request_id, digest)
            if previous is not None:
                return previous
            resource = self.get(resource_id, revision)
            if resource['kind'] not in {'ai', 'cloud_asr', 'local_agent'}:
                raise ResourceError('resource_not_repairable')
            if resource['deleted']:
                raise ResourceError('resource_deleted')
            if confirm_same_account is not True:
                raise ResourceError('same_identity_confirmation_required')
            evidence = self.db.execute('SELECT fingerprint,results_json FROM resource_validations WHERE validation_id=?', (validation_id,)).fetchone()
            if not evidence or evidence[0] != fingerprint([resource['kind'], resource['config'], credential]):
                raise ResourceError('validation_changed')
            validation = json.loads(evidence[1])
            if any(validation.get(p, {}).get('state') != 'ready' for p in resource['config']['purposes']):
                raise ResourceError('validation_required')
            binding = None if resource['kind'] == 'local_agent' else self._write_credential(credential)
            repair_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO resource_repairs VALUES(?,?,?,?,?,?)', (repair_id, resource_id, revision, binding, encoded(validation), normalize_utc()))
            self.db.execute('DELETE FROM resource_health WHERE resource_id=? AND revision=?', (resource_id, revision))
            for run in self.repository.list_runs():
                frozen = run.parameter_snapshot.get('resources', {})
                if any(isinstance(item, dict) and item.get('resource_id') == resource_id and item.get('revision') == revision for item in frozen.values()):
                    self.repository.append_stage_event(run.run_id, stage=run.current_stage, event_type='resource_binding_repaired',
                        detail={'resource_id': resource_id, 'revision': revision, 'repair_id': repair_id, 'binding_ref': binding})
            result = {'resource': self.get(resource_id, revision), 'repair_id': repair_id, 'binding_ref': binding}
            self._save_operation(request_id, digest, result)
            return result

    def accept_local_preparation(self, resource_id: str, revision: int, validation_id: str) -> None:
        with self.repository.transaction():
            resource = self.get(resource_id, revision)
            if resource['deleted'] or resource['kind'] != 'local_asr':
                raise ResourceError('resource_unavailable')
            evidence = self.db.execute('SELECT fingerprint,results_json FROM resource_validations WHERE validation_id=?', (validation_id,)).fetchone()
            if not evidence or evidence[0] != fingerprint([resource['kind'], resource['config'], self.credential(resource_id, revision)]):
                raise ResourceError('validation_changed')
            results = json.loads(evidence[1])
            if results.get('asr', {}).get('state') != 'ready':
                raise ResourceError('validation_required')
            self.db.execute('INSERT INTO resource_health VALUES(?,?,?) ON CONFLICT(resource_id,revision) DO UPDATE SET results_json=excluded.results_json', (resource_id, revision, encoded(results)))

    def freeze(self, resource_id: str, purpose: str, revision: int | None = None) -> dict[str, Any]:
        resource = self.get(resource_id, revision)
        if resource['deleted']:
            raise ResourceError('resource_deleted')
        if purpose not in resource['config']['purposes']:
            raise ResourceError('incompatible_resource')
        if resource['validation'].get(purpose, {}).get('state') != 'ready':
            raise ResourceError('resource_unavailable')
        if resource['kind'] == 'local_asr':
            from . import asr_models

            model = resource['config'].get('model', '')
            if model in asr_models.registry_ids() and asr_models.local_path_for(model) is None:
                raise ResourceError('model_not_installed')
        row = self._row(resource_id, resource['revision'])
        if resource['kind'] in {'ai', 'cloud_asr'} and not self.credential(resource_id, resource['revision']):
            raise ResourceError('credential_unavailable')
        return {'resource_id': resource_id, 'revision': resource['revision'], 'name': resource['name'],
                'kind': resource['kind'], 'purpose': purpose, 'config': resource['config'], 'binding_ref': row['binding_ref']}

    def resolved_binding(self, frozen: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        try:
            resource_id, revision, purpose = frozen['resource_id'], frozen['revision'], frozen['purpose']
            current = self.freeze(resource_id, purpose, revision)
        except (KeyError, TypeError):
            raise ResourceError('original_configuration_unknown') from None
        if any(current[key] != frozen.get(key) for key in ('resource_id', 'revision', 'kind', 'purpose', 'config')):
            raise ResourceError('original_configuration_unknown')
        return current, self.credential(resource_id, revision)
