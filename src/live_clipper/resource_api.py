from __future__ import annotations

from typing import Any

from .cheap_model_client import CheapModelServiceError, discover_models
from .config import Settings
from .project_storage import ProjectRepository
from .resource_providers import PROVIDERS, REGIONS
from .resource_store import ResourceError, ResourceStore, normalize_proposal
from .resource_validation import validate_resource

MESSAGES = {
    'validation_result_unknown': '上次验证结果尚未确认，请查询原操作，不要重复发起付费请求',
    'resource_not_found': '资源不存在', 'resource_deleted': '资源已删除',
    'revision_conflict': '资源已在其他位置修改，请查看最新配置后比较',
    'request_conflict': '该操作的内容已改变，请重新提交',
    'validation_changed': '配置已更改，请重新验证', 'validation_required': '请先验证新配置，当前生效版本保持不变',
    'referenced_capability_required': '项目仍依赖该用途，请补齐验证或另建资源后调整项目分配',
    'required_connection_fields': '请填写服务地址、模型标识和密钥',
    'resource_in_use': '资源仍有引用或运行占用', 'failed_runs_confirmation_required': '请确认删除对失败记录恢复的影响',
    'original_configuration_unknown': '原处理配置无法确认，请使用当前项目配置重新跑一次',
    'same_identity_confirmation_required': '请确认新凭据仍属于原供应商的同一账号和业务空间',
    'workspace_required': '请填写密钥所属的业务空间 ID', 'region_required': '请选择密钥所属地域',
    'custom_endpoint_requires_custom_provider': '修改预设地址时，请选择自定义兼容服务',
}


class ResourceAPI:
    def __init__(self, repository: ProjectRepository, settings: Settings, config_path=None):
        self.repository = repository
        self.store = ResourceStore(repository)
        self.settings = settings
        self.config_path = config_path

    def dispatch(self, method: str, parts: list[str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return self._dispatch(method, parts, body)
        except (ResourceError, CheapModelServiceError) as exc:
            message = MESSAGES.get(exc.code, '资源操作未完成，请检查字段或准备状态')
            return 409, {'ok': False, 'error': {'code': exc.code, 'message': message}, 'message': message, 'detail': getattr(exc, 'detail', None)}
        except (ValueError, TypeError, KeyError):
            return 422, {'ok': False, 'error': {'code': 'invalid_resource', 'message': '资源字段不完整或无效'}}

    def _dispatch(self, method: str, parts: list[str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tail = parts[2:]
        state = self.repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
        if method != 'GET' and tail != ['migration', 'retry'] and state and state[0] != 'completed':
            raise ResourceError('migration_pending')
        if not tail and method == 'GET':
            state = self.repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
            cleanups = [{'request_id': row[0], 'resource_id': row[1]} for row in self.store.db.execute("SELECT request_id,json_extract(result_json,'$.resource_id') FROM resource_operations WHERE json_extract(result_json,'$.deleted')=1 AND json_extract(result_json,'$.cleanup_state') <> 'completed'")]
            return 200, {'ok': True, 'cleanups': cleanups, 'resources': [self.detail(r['resource_id']) for r in self.store.list()], 'migration': state[0] if state else 'pending'}
        if tail == ['migration', 'retry'] and method == 'POST':
            from .config import load_settings
            from .resource_migration import migrate_resources

            if self.config_path is None:
                raise ResourceError('migration_source_unknown')
            settings = load_settings(self.config_path, env_path=self.config_path.parent / '.env')
            migrate_resources(self.repository, settings)
            return 200, {'ok': True}
        if tail == ['providers'] and method == 'GET':
            return 200, {'ok': True, 'providers': [{'id': key, **value} for key, value in PROVIDERS.items()], 'regions': REGIONS}
        if len(tail) == 2 and tail[0] == 'operations' and method == 'GET':
            return 200, {'ok': True, 'result': self.store.operation_result(tail[1])}
        if len(tail) == 2 and tail[0] == 'cleanup' and method == 'POST':
            return 200, {'ok': True, 'result': self.store.cleanup(tail[1])}
        if len(tail) == 2 and tail[1] == 'prepare' and method == 'POST':
            from . import asr_models, jobs

            self._strict(body, {'expected_revision'})
            with self.repository.transaction():
                resource = self.store.get(tail[0], body['expected_revision'])
                if resource['deleted']:
                    raise ResourceError('revision_conflict')
                if resource['kind'] != 'local_asr':
                    raise ResourceError('incompatible_resource')
                model = resource['config'].get('model')
                asr_models.model_entry(model)
                if asr_models.local_path_for(model) is None:
                    asr_models.download_capacity(model)
                source = resource['config'].get('model_source', 'modelscope')
                service_dir = self.repository.service_dir
                proposal = {key: resource[key] for key in ('name', 'kind', 'config')}
                token = self.store.credential(tail[0], resource['revision'])
                active = jobs.active_job_for(service_dir, model, asr_models.DOWNLOAD_JOB_KIND)
                if active:
                    return 202, {'ok': True, 'job': active}
                import uuid

                from .project_domain import normalize_utc

                task_id = uuid.uuid4().hex
                self.store.db.execute('INSERT INTO resource_tasks VALUES(?,?,?,?,?,NULL)', (task_id, resource['resource_id'], model, 'running', normalize_utc()))
                def prepare():
                    try:
                        asr_models.download_model(model, source, token=token)
                        with ProjectRepository(service_dir) as repo:
                            store = ResourceStore(repo)
                            evidence = validate_resource(store, proposal, resource_id=resource['resource_id'], revision=resource['revision'], purposes=['asr'])
                            if evidence['results']['asr']['state'] != 'ready':
                                return {'ok': False, 'message': '模型已下载，但加载验证未通过；文件已保留'}
                            store.accept_local_preparation(resource['resource_id'], resource['revision'], evidence['validation_id'])
                            return {'ok': True, 'resource_id': resource['resource_id'], 'revision': resource['revision']}
                    except Exception:
                        return {'ok': False, 'message': '模型准备未完成，请检查资源修订、网络、磁盘或运行环境；已下载文件保留'}
                    finally:
                        with ProjectRepository(service_dir) as repo:
                            with repo.transaction():
                                repo.connection.execute("UPDATE resource_tasks SET state='finished',finished_at=? WHERE task_id=?", (normalize_utc(), task_id))
                job = jobs.start_job(service_dir, kind=asr_models.DOWNLOAD_JOB_KIND, run_id=model, fn=prepare)
            return 202, {'ok': True, 'job': job}
        if tail == ['preview'] and method == 'POST':
            self._strict(body, {'proposal'})
            return 200, {'ok': True, 'proposal': normalize_proposal(body['proposal'])}
        if tail == ['models'] and method == 'POST':
            self._strict(body, {'proposal', 'credential', 'resource_id', 'revision'})
            proposal = normalize_proposal(body['proposal'])
            credential = self.store._effective_credential(proposal, body.get('credential'), body.get('resource_id'), body.get('revision'))
            if proposal['kind'] not in {'ai', 'cloud_asr'} or not credential or not proposal['config'].get('endpoint'):
                raise ResourceError('required_connection_fields')
            return 200, {'ok': True, 'models': discover_models(proposal['config']['endpoint'], credential)}
        if tail == ['validate'] and method == 'POST':
            self._strict(body, {'proposal', 'credential', 'resource_id', 'revision', 'purposes', 'request_id'})
            if not body.get('request_id'):
                raise ResourceError('request_id_required')
            return 200, {'ok': True, **validate_resource(self.store, **body)}
        if not tail and method == 'POST':
            self._strict(body, {'proposal', 'request_id', 'credential', 'validation_id'})
            return 201, {'ok': True, 'resource': self.store.save(**body)}
        if len(tail) == 1 and method == 'GET':
            return 200, {'ok': True, 'resource': self.detail(tail[0])}
        if len(tail) == 1 and method == 'PATCH':
            self._strict(body, {'proposal', 'request_id', 'credential', 'validation_id', 'expected_revision'})
            return 200, {'ok': True, 'resource': self.store.save(resource_id=tail[0], **body)}
        if len(tail) == 2 and tail[1] == 'delete-preview' and method == 'GET':
            return 200, {'ok': True, 'preview': self.store.delete_preview(tail[0])}
        if len(tail) == 1 and method == 'DELETE':
            self._strict(body, {'request_id', 'expected_revision', 'acknowledge_failed', 'clean_model'})
            return 200, {'ok': True, 'result': self.store.delete(tail[0], **body)}
        if len(tail) == 3 and tail[1] == 'revisions' and method == 'GET':
            return 200, {'ok': True, 'resource': {**self.store.get(tail[0], int(tail[2])), **self.store.references(tail[0], int(tail[2]))}}
        if len(tail) == 2 and tail[1] == 'repair' and method == 'POST':
            self._strict(body, {'revision', 'credential', 'validation_id', 'request_id', 'confirm_same_account'})
            return 200, {'ok': True, **self.store.repair(tail[0], **body)}
        return 404, {'ok': False, 'error': {'code': 'route_not_found', 'message': '资源操作不存在'}}

    def detail(self, resource_id: str) -> dict[str, Any]:
        from . import asr_models, jobs

        resource = self.store.get(resource_id)
        if resource['kind'] == 'local_asr':
            model = resource['config'].get('model')
            resource['model_files'] = next((m for m in asr_models.list_models(self.repository.service_dir) if m['id'] == model), None)
            resource['preparation'] = jobs.active_job_for(self.repository.service_dir, model, asr_models.DOWNLOAD_JOB_KIND)
        return {**resource, **self.store.references(resource_id)}

    @staticmethod
    def _strict(body: dict[str, Any], allowed: set[str]) -> None:
        if not isinstance(body, dict) or set(body) - allowed:
            raise ResourceError('invalid_fields')
