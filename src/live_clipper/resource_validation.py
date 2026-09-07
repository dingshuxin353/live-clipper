"""Explicit capability probes use built-in content and the production request/validation code."""
from __future__ import annotations

import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cheap_model_client import CheapModelClient, CheapModelServiceError
from .config import ASRConfig, LLMConfig, ReviewAutomationConfig, Settings
from .models import ClipCandidate, CorrectedTranscript, TranscriptWindow
from .project_domain import normalize_utc
from .prompt_loader import load_prompt
from .resource_store import ResourceError, ResourceStore, encoded, fingerprint, normalize_proposal

TEXT = '整理课程资料时，先按主题分组，再写出每组的一句话摘要。这样能更快找到需要的内容，也能检查有没有遗漏。'
SENTENCE = {'start': 0.0, 'end': 20.0, 'text': TEXT}
CANDIDATE = {'id': 'venus-probe', 'start': 0.0, 'end': 20.0, 'score': 7, 'clip_type': 'method', 'hook': '整理课程资料', 'core_value': '先分组再摘要', 'reason': '可操作的方法'}


def probe_settings(proposal: dict[str, Any], credential: str | None) -> Settings:
    config = proposal['config']
    if proposal['kind'] == 'ai' and not config.get('provider'):
        raise ResourceError('required_connection_fields')
    if proposal['kind'] in {'ai', 'cloud_asr'} and (not credential or not config.get('endpoint') or not config.get('model')):
        raise ResourceError('required_connection_fields')
    llm = LLMConfig(api_base=config.get('endpoint', ''), model=config.get('model', ''), api_key=credential,
                    provider_label=config.get('provider', 'custom'), request_profile=config.get('request_profile', 'chat-completions-v1'),
                    timeout_seconds=config.get('timeout_seconds', 300), request_attempts=1)
    review = ReviewAutomationConfig(mode='local_agent' if proposal['kind'] == 'local_agent' else 'model')
    review = replace(review, model=replace(review.model, temperature=config.get('temperature', 0.2), max_tokens=config.get('max_tokens', 4096)),
                     local_agent=replace(review.local_agent, provider='claude_code', command_timeout_minutes=config.get('command_timeout_minutes', 60)))
    return Settings(llm=llm, review_automation=review,
                    asr=ASRConfig(backend='openai' if proposal['kind'] == 'cloud_asr' else 'mlx_whisper',
                                  model=config.get('model', ''), language=config.get('language', 'zh'),
                                  api_base=config.get('endpoint'), api_key=credential if proposal['kind'] == 'cloud_asr' else None))


def validate_analysis(settings: Settings) -> None:
    from .refine_candidates import _normalize_refinement
    from .scan_windows import normalize_candidate_payload

    client = CheapModelClient(settings, request_attempts=1)
    window = TranscriptWindow(id='venus-probe', start=0, end=20, sentences=[SENTENCE])
    raw = client.complete_json(load_prompt('cheap_scan_window.md', 'scan probe'), window.model_dump(), max_tokens=4096)
    if not isinstance(raw, dict) or raw.get('window_id') != window.id or not isinstance(raw.get('candidates'), list):
        raise ResourceError('analysis_output_invalid')
    identifiers = set()
    for index, item in enumerate(raw['candidates']):
        candidate = ClipCandidate.model_validate({'id': f'venus-probe-{index}', **normalize_candidate_payload(item)})
        if candidate.start < window.start or candidate.end > window.end or candidate.id in identifiers:
            raise ResourceError('analysis_output_invalid')
        identifiers.add(candidate.id)
    corrected = client.complete_json(load_prompt('cheap_correct_transcript.md', 'correction probe'), {'sentences': [SENTENCE], 'glossary': []}, max_tokens=8192)
    if isinstance(corrected, list):
        corrected = {'sentences': corrected}
    result = CorrectedTranscript.model_validate(corrected)
    if len(result.sentences) != 1:
        raise ResourceError('correction_output_invalid')
    refined = client.complete_json(load_prompt('cheap_refine_candidate.md', 'refinement probe'), {'candidate': CANDIDATE, 'context': [SENTENCE], 'business_goal': '整理课程资料'}, max_tokens=2048)
    _normalize_refinement(ClipCandidate.model_validate(CANDIDATE), refined)


def validate_review(settings: Settings) -> None:
    from .project_result_runtime import _validate_review
    from .review_automation import run_structured_review_adapter

    payload = {'format_version': 1, 'source_name': 'Venus 内置验证内容', 'overall_analysis': '',
               'review_policy_version': 'auto_review_v1', 'candidates': [{**CANDIDATE, 'transcript_excerpt': TEXT}],
               'candidate_count': 1, 'truncated': False}
    result = run_structured_review_adapter(settings, payload)
    _validate_review(result, sent_candidates=[CANDIDATE])


def validate_asr(settings: Settings, *, local: bool) -> None:
    from . import asr_models
    from .transcribe import transcribe_audio, transcript_sentences_from_raw

    if local and asr_models.local_path_for(settings.asr_model) is None:
        model_path = Path(settings.asr_model)
        if not model_path.is_absolute() or not model_path.is_dir():
            raise ResourceError('model_not_installed')
    sample = Path(__file__).parent / 'validation_audio.wav'
    if not sample.is_file():
        raise ResourceError('validation_audio_unavailable')
    with tempfile.TemporaryDirectory(prefix='venus-resource-check-') as directory:
        result = transcribe_audio(sample, Path(directory) / 'transcript.json', settings)
    if not isinstance(result.get('segments'), list) or not transcript_sentences_from_raw(result):
        raise ResourceError('asr_timestamps_unavailable')


def validate_resource(store: ResourceStore, proposal: dict[str, Any], *, credential: str | None = None,
                      resource_id: str | None = None, revision: int | None = None, purposes: list[str], request_id: str | None = None) -> dict[str, Any]:
    proposal = normalize_proposal(proposal)
    if not purposes or set(purposes) - set(proposal['config']['purposes']):
        raise ResourceError('invalid_purposes')
    credential = store._effective_credential(proposal, credential, resource_id, revision)
    settings = probe_settings(proposal, credential)
    task_id = uuid.uuid4().hex
    request_id = request_id or 'local-check-' + task_id
    digest = fingerprint(['validate', proposal, credential, resource_id, revision, purposes])
    with store.repository.transaction():
        previous = store._operation(request_id, digest)
        if previous is not None:
            if previous.get('validation_id'):
                return previous
            raise ResourceError('validation_result_unknown', detail={'request_id': request_id, 'state': previous.get('state')})
        store._save_operation(request_id, digest, {'operation': 'validate', 'state': 'running'})
        if resource_id and store.get(resource_id, revision)['deleted']:
            raise ResourceError('resource_deleted')
        store.db.execute('INSERT INTO resource_tasks VALUES(?,?,?,?,?,NULL)', (task_id, resource_id, proposal['config'].get('model'), 'running', normalize_utc()))
    try:
        results = {}
        for purpose in purposes:
            try:
                if purpose == 'analysis':
                    validate_analysis(settings)
                elif purpose == 'review':
                    validate_review(settings)
                else:
                    validate_asr(settings, local=proposal['kind'] == 'local_asr')
            except Exception as exc:
                code = exc.code if isinstance(exc, (ResourceError, CheapModelServiceError)) else 'capability_validation_failed'
                results[purpose] = {'state': 'needs_repair', 'code': code}
            else:
                results[purpose] = {'state': 'ready'}
        identifier = store.record_validation(proposal, credential=credential, results=results, resource_id=resource_id, revision=revision)
        result = {'validation_id': identifier, 'results': results}
        with store.repository.transaction():
            store.db.execute('UPDATE resource_operations SET result_json=? WHERE request_id=?', (encoded(result), request_id))
        return result
    finally:
        with store.repository.transaction():
            store.db.execute("UPDATE resource_tasks SET state='finished',finished_at=? WHERE task_id=?", (normalize_utc(), task_id))
