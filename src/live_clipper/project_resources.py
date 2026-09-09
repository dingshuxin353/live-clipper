from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .project_domain import assert_secret_free
from .project_storage import ProjectRepository
from .resource_store import ResourceError, ResourceStore


@dataclass(frozen=True)
class ResourceOption:
    resource_id: str
    display_name: str
    resource_type: str
    ready: bool
    problem: str | None = None
    version: str | None = None
    purposes: tuple[str, ...] = ()
    ready_purposes: tuple[str, ...] = ()


class ResourceUnavailableError(ValueError):
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id
        super().__init__(f'resource unavailable: {resource_id}')


def resource_options(repository: ProjectRepository) -> tuple[ResourceOption, ...]:
    return tuple(ResourceOption(
        resource_id=r['resource_id'], display_name=r['name'],
        resource_type='asr' if r['kind'] in {'local_asr', 'cloud_asr'} else ('review' if r['kind'] == 'local_agent' else 'analysis'),
        ready=r['ready'], problem=None if r['ready'] else '资源尚未通过用途验证', version=r['config'].get('model', 'Claude Code'),
        purposes=tuple(r['config']['purposes']), ready_purposes=tuple(p for p, v in r['validation'].items() if v['state'] == 'ready'),
    ) for r in ResourceStore(repository).list())


def resource_map(repository: ProjectRepository) -> dict[str, ResourceOption]:
    return {resource.resource_id: resource for resource in resource_options(repository)}


def effective_references(config: dict[str, Any]) -> dict[str, str]:
    refs = config['resources']
    return {p: refs.get('analysis_ref', '') if p == 'review' and refs.get('review_ref') == 'reuse_analysis' else refs.get(p + '_ref', '') for p in ('asr', 'analysis', 'review')}


def resource_repair_context(repository: ProjectRepository, resource_id: str, *, issue_id: str, revision: int | None = None) -> dict[str, Any]:
    resource = ResourceStore(repository).get(resource_id, revision)
    return {'resource_id': resource_id, 'display_name': resource['name'], 'resource_type': resource['kind'],
            'api_base': resource['config'].get('endpoint'), 'model': resource['config'].get('model'),
            'revision': resource['revision'], 'credential_state': 'configured' if resource['has_credential'] else 'missing',
            'repair_capability': 'inline_connection', 'settings_url': f'/resources/{resource_id}', 'issue_id': issue_id}


def resolve_parameter_snapshot(config: dict[str, Any], settings: Settings, *, repository: ProjectRepository) -> dict[str, Any]:
    state = repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
    if state and state[0] != 'completed':
        raise ResourceUnavailableError('migration_pending')
    store = ResourceStore(repository)
    refs = effective_references(config)
    resources = {}
    for purpose, identifier in refs.items():
        try:
            resources[purpose] = store.freeze(identifier, purpose)
        except ResourceError:
            raise ResourceUnavailableError(identifier) from None
        resources[purpose]['model'] = resources[purpose]['config'].get('model')
        resources[purpose]['provider'] = resources[purpose]['config'].get('provider')
        resources[purpose + '_ref'] = identifier
    resources.update(arbitration_mode='reuse_analysis', arbitration_ref=None)
    resources['asr']['backend'] = 'openai' if resources['asr']['kind'] == 'cloud_asr' else 'mlx_whisper'
    resources['asr']['language'] = resources['asr']['config'].get('language', 'zh')
    snapshot = {'schema_version': 2, 'resource_contract': 1, 'resources': resources,
                'processing': dict(config['processing']), 'output': dict(config['output']),
                'execution_policy': {'stage_parameters': {'scan': {'max_tokens': 4096, 'temperature': 0.1}, 'correction': {'max_tokens': 8192, 'temperature': 0.1}, 'refine': {'max_tokens': 2048, 'temperature': 0.1}},
                                     'correct_transcript': False, 'refine': False, 'refine_top_n': 25,
                                     'prompt_directory': str(settings.prompts.directory.resolve()) if settings.prompts.directory else None,
                                     'glossary_path': str(settings.paths.glossary_path.resolve()),
                                     'review_timeout_minutes': settings.review_automation.timeout_minutes,
                                     'review_prompt_template': settings.review_automation.prompt_template,
                                     'review_max_candidates': settings.review_automation.model.max_candidates,
                                     'review_retry_attempts': settings.review_automation.model.retry_attempts},
                'retry_policy': {'version': 'project_runtime_retry_v1', 'ai': {'max_retries': 2, 'delays_seconds': [30, 120]}, 'render': {'max_retries': 1, 'delays_seconds': [30]}}}
    from .prompt_loader import load_prompt

    glossary = settings.paths.glossary_path.resolve()
    if not glossary.exists() and glossary.name == 'common_terms.json':
        glossary = glossary.with_name('common_terms.example.json')
    snapshot['execution_policy']['glossary_path'] = str(glossary)
    snapshot['execution_policy']['glossary_hash'] = hashlib.sha256(glossary.read_bytes()).hexdigest() if glossary.is_file() else None
    snapshot['execution_policy']['prompt_hashes'] = {name: hashlib.sha256(load_prompt(name, name, prompt_dir=settings.prompts.directory).encode()).hexdigest() for name in ('cheap_scan_window.md', 'cheap_correct_transcript.md', 'cheap_refine_candidate.md', 'project_auto_review.md', 'review_select_clips.md')}
    assert_secret_free(snapshot)
    return snapshot
