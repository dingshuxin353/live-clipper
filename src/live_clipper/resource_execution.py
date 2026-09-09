"""Resolve frozen resource revisions into per-call settings, without global mutation."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import ASRConfig, LLMConfig, ReviewAutomationLocalAgentConfig, Settings
from .project_storage import ProjectRepository
from .resource_store import ResourceError, ResourceStore


def settings_for_snapshot(repository: ProjectRepository, settings: Settings, snapshot: dict[str, Any], *, purpose: str = 'analysis') -> Settings:
    resources = snapshot.get('resources', {})
    if snapshot.get('resource_contract') != 1 or purpose not in {'analysis', 'review'}:
        raise ResourceError('original_configuration_unknown')
    store = ResourceStore(repository)
    try:
        asr, asr_key = store.resolved_binding(resources.get('asr', {}))
    except ResourceError as exc:
        raise ResourceError('asr_resource_unavailable', detail={'reason': exc.code, 'purpose': 'asr'}) from None
    try:
        selected, key = store.resolved_binding(resources.get(purpose, {}))
    except ResourceError as exc:
        raise ResourceError('ai_resource_unavailable', detail={'reason': exc.code, 'purpose': purpose}) from None
    ac = asr['config']
    asr_config = ASRConfig(backend='openai' if asr['kind'] == 'cloud_asr' else 'mlx_whisper',
                           model=ac['model'], language=ac.get('language', 'zh'), api_base=ac.get('endpoint'),
                           api_key=asr_key if asr['kind'] == 'cloud_asr' else None,
                           hf_token=asr_key if asr['kind'] == 'local_asr' else None,
                           model_source=ac.get('model_source', 'modelscope'))
    config = selected['config']
    policy = snapshot['execution_policy']
    from .prompt_loader import load_prompt

    prompt_directory = Path(policy['prompt_directory']) if policy.get('prompt_directory') else None
    for name, digest in policy.get('prompt_hashes', {}).items():
        if hashlib.sha256(load_prompt(name, name, prompt_dir=prompt_directory).encode()).hexdigest() != digest:
            raise ResourceError('original_prompt_changed')
    if policy.get('correct_transcript') and 'glossary_hash' in policy:
        glossary = Path(policy['glossary_path'])
        digest = hashlib.sha256(glossary.read_bytes()).hexdigest() if glossary.is_file() else None
        if digest != policy['glossary_hash']:
            raise ResourceError('original_glossary_changed')
    automation = replace(settings.review_automation, mode='local_agent' if selected['kind'] == 'local_agent' else 'model',
                         timeout_minutes=policy['review_timeout_minutes'], prompt_template=policy['review_prompt_template'],
                         model=replace(settings.review_automation.model, use_llm_config=True, model='',
                                       max_candidates=policy['review_max_candidates'], retry_attempts=policy['review_retry_attempts'],
                                       temperature=config.get('temperature', 0.2), max_tokens=config.get('max_tokens', 4096)),
                         local_agent=ReviewAutomationLocalAgentConfig(provider='claude_code',
                                       command_timeout_minutes=config.get('command_timeout_minutes', 60),
                                       include_review_package_inline=config.get('include_review_package_inline', True)))
    llm = LLMConfig(request_profile=config.get('request_profile', 'chat-completions-v1'), api_base=config.get('endpoint', ''), model=config.get('model', ''), api_key=key,
                    provider_label=config.get('provider', 'custom'), timeout_seconds=config.get('timeout_seconds', 300),
                    request_attempts=config.get('request_attempts', 1), retry_delay_seconds=config.get('retry_delay_seconds', 3.0))
    # Clear the old projections too: Settings normalizes them before the dataclass fields.
    return replace(settings, resource_execution_policy=policy,
                   prompts=replace(settings.prompts, directory=prompt_directory),
                   paths=replace(settings.paths, glossary_path=Path(policy['glossary_path'])) if policy.get('glossary_path') else settings.paths,
                   asr=asr_config, llm=llm, review_automation=automation,
                   asr_backend=asr_config.backend, asr_model=asr_config.model, asr_language=asr_config.language,
                   asr_api_base=asr_config.api_base, asr_api_key=asr_config.api_key, hf_token=asr_config.hf_token,
                   cheap_model_api_base=llm.api_base, cheap_model_name=llm.model, cheap_model_api_key=key)


def pipeline_settings(settings: Settings) -> Settings:
    """A child receives only its database/run identity; credentials are resolved locally."""
    import json
    import os

    context = os.environ.get('LIVE_CLIPPER_PROJECT_RUN')
    if context is None:
        raise ResourceError("project_run_context_required")
    try:
        service_dir, run_id = json.loads(context)
        if not isinstance(service_dir, str) or not isinstance(run_id, str):
            raise ValueError
    except (ValueError, TypeError):
        raise ResourceError('invalid_run_context') from None
    with ProjectRepository(service_dir) as repository:
        run = repository.get_run(run_id)
        if run is None or run.status not in {'processing', 'queued'}:
            raise ResourceError('run_not_active')
        return settings_for_snapshot(repository, settings, run.parameter_snapshot)
