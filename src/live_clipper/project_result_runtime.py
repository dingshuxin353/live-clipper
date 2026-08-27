from __future__ import annotations

import errno
import hashlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .media_probe import MediaMetadata, probe_media
from .models import ClipCandidate, CorrectedTranscript, ProjectReviewResult, ReviewDecision, SelectedClip
from .project_domain import Run, new_id, stable_json
from .project_result_domain import AIReviewSession, RunOutput, sanitize_persisted_text
from .project_service import output_directory_is_writable
from .project_storage import ProjectRepository
from .utils import read_json

ReviewAdapter = Callable[[dict[str, Any]], Mapping[str, Any] | ProjectReviewResult | None]
ClipRenderer = Callable[[Path, CorrectedTranscript, SelectedClip, Path, Path], Any]
MediaProbe = Callable[[Path], MediaMetadata]


class ProjectReviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class ProjectRenderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RenderReport:
    run_id: str
    ready_output_ids: tuple[str, ...] = ()
    failed_output_ids: tuple[str, ...] = ()
    reused_output_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerTickReport:
    scheduled_review_run_ids: tuple[str, ...] = ()
    scheduled_render_run_ids: tuple[str, ...] = ()
    completed_run_ids: tuple[str, ...] = ()
    failed_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutomaticRetryPlan:
    retry: bool
    next_retry_at: str | None
    automatic_attempt_count: int
    exhausted: bool


def automatic_review_retry_plan(
    error: BaseException,
    *,
    attempt_number: int,
    now: datetime | None = None,
) -> AutomaticRetryPlan:
    root: BaseException = error
    while root.__cause__ is not None:
        root = root.__cause__
    response = getattr(root, "response", None)
    status = (
        getattr(root, "status", None)
        or getattr(root, "status_code", None)
        or getattr(response, "status_code", None)
    )
    transient = isinstance(root, (TimeoutError, ConnectionError)) or "Timeout" in type(root).__name__ or status == 429 or (
        isinstance(status, int) and 500 <= status <= 599
    )
    if not transient or attempt_number > 2:
        return AutomaticRetryPlan(False, None, max(0, attempt_number - 1), transient and attempt_number > 2)
    current = now or datetime.now(UTC)
    delay = (30, 120)[attempt_number - 1]
    retry_at = (current + timedelta(seconds=delay)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    return AutomaticRetryPlan(True, retry_at, attempt_number, False)


def automatic_render_retry_plan(
    error: BaseException,
    *,
    automatic_attempt_count: int,
    now: datetime | None = None,
) -> AutomaticRetryPlan:
    transient = isinstance(error, (BrokenPipeError, subprocess.CalledProcessError))
    if not transient or automatic_attempt_count >= 1:
        return AutomaticRetryPlan(False, None, automatic_attempt_count, transient and automatic_attempt_count >= 1)
    current = now or datetime.now(UTC)
    retry_at = (current + timedelta(seconds=30)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    return AutomaticRetryPlan(True, retry_at, 1, False)


def _read_candidate_items(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "refined_candidates.json"
    if not path.exists():
        path = run_dir / "merged_candidates.json"
    raw = read_json(path)
    if isinstance(raw, dict):
        raw = raw.get("candidates", [])
    if not isinstance(raw, list):
        raise ProjectReviewError("ai_review_invalid", "candidate package is not a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        candidate = ClipCandidate.model_validate(item)
        if candidate.id in seen:
            raise ProjectReviewError("ai_review_invalid", "candidate package contains duplicate ids")
        seen.add(candidate.id)
        normalized = candidate.model_dump()
        if isinstance(item, dict):
            normalized.update({key: value for key, value in item.items() if key not in normalized})
        result.append(normalized)
    return sorted(result, key=lambda item: (-float(item.get("score", 0)), str(item["id"])))


def _candidate_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    context = item.get("context", [])
    excerpts = [str(entry.get("text", "")) for entry in context if isinstance(entry, dict)]
    return {
        "candidate_id": item["id"],
        "candidate_type": item.get("clip_type", "highlight"),
        "score": item.get("score", 0),
        "source_start": item["start"],
        "source_end": item["end"],
        "suggested_context_before": item.get("suggested_context_before", 0),
        "suggested_context_after": item.get("suggested_context_after", 0),
        "hook": sanitize_persisted_text(item.get("hook", "")),
        "core_value": sanitize_persisted_text(item.get("core_value", "")),
        "reason": sanitize_persisted_text(item.get("reason", "")),
        "risk": sanitize_persisted_text(item.get("risk", "")) if item.get("risk") else None,
        "transcript_excerpt": sanitize_persisted_text("\n".join(excerpts))[:4000],
    }


def _sanitize_review_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if re.search(
                r"(?i)(?:api[_-]?key|token|secret|password|raw[_-]?response|full[_-]?prompt|chain[_-]?of[_-]?thought|hidden[_-]?reasoning)",
                normalized_key,
            ):
                raise ProjectReviewError("ai_review_invalid", "review result contains a non-persistable field")
            safe[normalized_key] = _sanitize_review_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_sanitize_review_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_persisted_text(value)
    return value


def build_project_review_payload(run: Run, run_dir: str | Path, *, max_candidates: int = 40) -> dict[str, Any]:
    target = Path(run_dir)
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    candidates = _read_candidate_items(target)
    brief = read_json(target / "codex_brief.json")
    sent = candidates[:max_candidates]
    return {
        "format_version": 1,
        "source_name": Path(run.latest_seen_path).name,
        "overall_analysis": sanitize_persisted_text(brief.get("summary", "")) if isinstance(brief, dict) else "",
        "review_policy_version": run.parameter_snapshot.get("processing", {}).get(
            "review_policy_version", "auto_review_v1"
        ),
        "candidates": [_candidate_payload(item) for item in sent],
        "candidate_count": len(candidates),
        "truncated": len(sent) < len(candidates),
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.stem}.tmp{path.suffix}")
    encoded = (stable_json(value) + "\n").encode("utf-8")
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_integrity_path(run_dir: Path, output_id: str) -> Path:
    return run_dir / "outputs" / output_id / "media_integrity.json"


def _registered_media_metadata(output: RunOutput) -> dict[str, int | str] | None:
    values = {
        "duration_ms": output.duration_ms,
        "width": output.width,
        "height": output.height,
        "container": output.container,
        "video_codec": output.video_codec,
        "byte_size": output.byte_size,
    }
    if any(value is None for value in values.values()):
        return None
    return {
        "duration_ms": int(output.duration_ms),
        "width": int(output.width),
        "height": int(output.height),
        "container": str(output.container),
        "video_codec": str(output.video_codec),
        "byte_size": int(output.byte_size),
    }


def _write_output_integrity(run_dir: Path, output_id: str, path: Path, metadata: MediaMetadata) -> None:
    _atomic_write_json(
        _output_integrity_path(run_dir, output_id),
        {
            "format_version": 1,
            "output_id": output_id,
            "sha256": _sha256_file(path),
            "media_metadata": metadata.as_storage_dict(),
        },
    )


def _ready_output_is_verified(
    output: RunOutput,
    final_path: Path,
    run_dir: Path,
    probe: MediaProbe,
) -> bool:
    registered = _registered_media_metadata(output)
    if registered is None:
        return False
    metadata = probe(final_path)
    if metadata.as_storage_dict() != registered:
        return False
    evidence = read_json(_output_integrity_path(run_dir, output.output_id))
    if not isinstance(evidence, dict):
        return False
    expected_hash = evidence.get("sha256")
    return bool(
        evidence.get("format_version") == 1
        and evidence.get("output_id") == output.output_id
        and evidence.get("media_metadata") == registered
        and isinstance(expected_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        and _sha256_file(final_path) == expected_hash
    )


def _functional_issue(
    repository: ProjectRepository,
    run: Run,
    code: str,
    *,
    output_id: str | None = None,
    status: str = "action_required",
    automatic_attempt_count: int = 0,
    next_retry_at: str | None = None,
    retry_exhausted: bool = False,
) -> None:
    details = {
        "ai_review_invalid": (
            "ai",
            "AI 审阅结果无效",
            "本次审阅没有形成可信结果",
            "检查审阅资源后继续同一记录",
            "continue_run",
        ),
        "ai_review_failed": (
            "ai",
            "AI 审阅失败",
            "本次审阅尚未完成",
            "稍后检查并继续同一记录",
            "continue_run",
        ),
        "ai_resource_unavailable": (
            "ai",
            "AI 审阅资源不可用",
            "已完成的读取、转写和分析产物保持不变",
            "修复审阅资源后继续同一记录",
            "continue_run",
        ),
        "asr_resource_unavailable": (
            "asr",
            "语音识别资源不可用",
            "来源录像和已完成产物保持不变",
            "修复语音识别资源后继续同一记录",
            "continue_run",
        ),
        "source_missing": (
            "recording",
            "来源录像不存在",
            "已完成产物保持不变",
            "选择内容一致的录像后继续",
            "continue_run",
        ),
        "source_unreadable": (
            "recording",
            "来源录像不可读",
            "已完成产物保持不变",
            "恢复录像访问后继续",
            "continue_run",
        ),
        "output_unwritable": (
            "storage",
            "输出目录不可写",
            "成片尚未写入目标目录",
            "恢复目录权限后继续渲染",
            "continue_run",
        ),
        "storage_full": (
            "storage",
            "输出空间不足",
            "已完成成片保持不变",
            "释放空间后继续渲染",
            "continue_run",
        ),
        "render_failed": (
            "render",
            "成片渲染失败",
            "其他已完成成片保持不变",
            "检查后只重试该成片",
            "retry_output",
        ),
    }
    category, title, preserved, next_step, capability = details[code]
    issue = repository.discover_issue(
        issue_code=code,
        category=category,
        scope_type="output" if output_id else "run",
        project_id=run.project_id,
        run_id=run.run_id,
        output_id=output_id,
        issue_group_key=f"{code}:{output_id or run.run_id}",
        status=status,
        title=title,
        summary=title,
        impact="当前处理无法完成",
        preserved_content=preserved,
        next_step=next_step,
        recovery_capability=capability,
        safe_checkpoint=(
            "validated_review"
            if code in {"render_failed", "output_unwritable", "storage_full"}
            else "artifacts"
        ),
        reuse_stages=(
            ("read_source", "transcribe", "analyze", "arbitrate")
            if code not in {"source_missing", "source_unreadable", "asr_resource_unavailable"}
            else ()
        ),
        redo_stages=(
            ("render",)
            if code in {"render_failed", "output_unwritable", "storage_full"}
            else (("read_source",) if code in {"source_missing", "source_unreadable"} else (("transcribe",) if code == "asr_resource_unavailable" else ("review",)))
        ),
        automatic_attempt_count=automatic_attempt_count,
        next_retry_at=next_retry_at,
        retry_exhausted=retry_exhausted,
    )
    if (
        issue.status != status
        or issue.automatic_attempt_count != automatic_attempt_count
        or issue.next_retry_at != next_retry_at
        or issue.retry_exhausted != retry_exhausted
    ):
        from .project_domain import normalize_utc

        with repository.transaction():
            repository.connection.execute(
                """UPDATE issues SET status = ?, automatic_attempt_count = ?, next_retry_at = ?,
                     retry_exhausted = ?, updated_at = ?, issue_revision = issue_revision + 1
                   WHERE issue_id = ?""",
                (
                    status,
                    automatic_attempt_count,
                    next_retry_at,
                    int(retry_exhausted),
                    normalize_utc(),
                    issue.issue_id,
                ),
            )


def _mark_review_failure(
    repository: ProjectRepository,
    run: Run,
    session: AIReviewSession,
    *,
    code: str,
    retry_plan: AutomaticRetryPlan | None = None,
) -> None:
    from .project_domain import normalize_utc

    timestamp = normalize_utc()
    with repository.transaction():
        repository.connection.execute(
            """UPDATE ai_review_sessions SET status = ?, completed_at = ?, updated_at = ?
               WHERE review_session_id = ? AND status = 'running'""",
            ("invalid" if code == "ai_review_invalid" else "failed", timestamp, timestamp, session.review_session_id),
        )
    repository.transition_run(
        run.run_id,
        status="failed",
        stage="review",
        event_type="failed",
        detail={"reason": code},
        error_code=code,
        error_summary="AI 审阅未完成",
    )
    plan = retry_plan or AutomaticRetryPlan(False, None, 0, False)
    _functional_issue(
        repository,
        run,
        code,
        status="retrying" if plan.retry else "action_required",
        automatic_attempt_count=plan.automatic_attempt_count,
        next_retry_at=plan.next_retry_at,
        retry_exhausted=plan.exhausted,
    )


def _resolve_review_issues(repository: ProjectRepository, run_id: str) -> None:
    from .project_domain import normalize_utc

    timestamp = normalize_utc()
    with repository.transaction():
        rows = repository.connection.execute(
            """SELECT issue_id FROM issues WHERE run_id = ? AND status <> 'resolved'
               AND issue_code IN ('ai_review_invalid', 'ai_review_failed', 'ai_resource_unavailable')""",
            (run_id,),
        ).fetchall()
        for (issue_id,) in rows:
            repository.connection.execute(
                """UPDATE issues SET status = 'resolved', issue_revision = issue_revision + 1,
                     updated_at = ?, resolved_at = ? WHERE issue_id = ?""",
                (timestamp, timestamp, issue_id),
            )
            repository.connection.execute(
                "INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json) VALUES (?, 'review_verified', ?, '{}')",
                (issue_id, timestamp),
            )


def _resolve_render_issues(repository: ProjectRepository, run_id: str, output_id: str) -> None:
    from .project_domain import normalize_utc

    timestamp = normalize_utc()
    with repository.transaction():
        rows = repository.connection.execute(
            """SELECT issue_id FROM issues WHERE run_id = ? AND status <> 'resolved'
               AND (output_id = ? OR output_id IS NULL)
               AND issue_code IN ('render_failed', 'output_unwritable', 'storage_full',
                                  'output_missing', 'output_unreadable')""",
            (run_id, output_id),
        ).fetchall()
        for (issue_id,) in rows:
            repository.connection.execute(
                """UPDATE issues SET status = 'resolved', issue_revision = issue_revision + 1,
                     updated_at = ?, resolved_at = ? WHERE issue_id = ?""",
                (timestamp, timestamp, issue_id),
            )
            repository.connection.execute(
                "INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json) VALUES (?, 'output_verified', ?, '{}')",
                (issue_id, timestamp),
            )


def _validate_review(
    raw: Mapping[str, Any] | ProjectReviewResult | None,
    *,
    sent_candidates: Sequence[Mapping[str, Any]],
) -> ProjectReviewResult:
    if raw is None:
        raise ProjectReviewError("ai_review_invalid", "model response is empty")
    try:
        result = raw if isinstance(raw, ProjectReviewResult) else ProjectReviewResult.model_validate(raw)
    except (TypeError, ValidationError) as exc:
        raise ProjectReviewError("ai_review_invalid", "model response does not match review_result") from exc
    try:
        result = ProjectReviewResult.model_validate(_sanitize_review_value(result.model_dump(mode="json")))
    except ValidationError as exc:
        raise ProjectReviewError("ai_review_invalid", "sanitized review result is invalid") from exc
    expected = {str(item["id"]) for item in sent_candidates}
    actual = {item.candidate_id for item in result.decisions}
    if actual != expected:
        raise ProjectReviewError("ai_review_invalid", "review decisions must cover every received candidate")
    by_id = {str(item["id"]): item for item in sent_candidates}
    for decision in result.decisions:
        if decision.decision != "selected":
            continue
        assert decision.selected_clip is not None
        candidate = by_id[decision.candidate_id]
        allowed_start = max(0.0, float(candidate["start"]) - float(candidate.get("suggested_context_before", 0)))
        allowed_end = float(candidate["end"]) + float(candidate.get("suggested_context_after", 0))
        clip = decision.selected_clip
        if clip.source_start < allowed_start or clip.source_end > allowed_end:
            raise ProjectReviewError("ai_review_invalid", "selected range is outside candidate context")
        cursor = clip.source_start
        removed = 0.0
        for start, end in sorted(clip.remove_ranges):
            if start < cursor or start >= end or start < clip.source_start or end > clip.source_end:
                raise ProjectReviewError("ai_review_invalid", "selected clip has invalid remove ranges")
            removed += end - start
            cursor = end
        if removed >= clip.source_end - clip.source_start:
            raise ProjectReviewError("ai_review_invalid", "remove ranges delete the selected clip")
    return result


def safe_output_name(source_name: str) -> str:
    cleaned_source = re.sub(r"[\\/\x00-\x1f\x7f]", "", source_name)
    clean = Path(cleaned_source).stem.strip()
    return (clean or "recording")[:60]


def _decision_evidence(decision: ReviewDecision) -> dict[str, Any]:
    return decision.model_dump(mode="json")


def run_project_review(
    repository: ProjectRepository,
    run_id: str,
    *,
    run_dir: str | Path,
    adapter: ReviewAdapter,
    max_candidates: int = 40,
    clock: Callable[[], datetime] | None = None,
) -> AIReviewSession:
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run.parameter_snapshot.get("schema_version") != 2:
        raise ProjectReviewError("legacy_run", "schema v1 runs retain the legacy review contract")
    target = Path(run_dir)
    candidates = _read_candidate_items(target)
    payload = build_project_review_payload(run, target, max_candidates=max_candidates)
    sent = candidates[:max_candidates]
    sessions = repository.list_ai_review_sessions(run_id)
    running = next((item for item in sessions if item.status == "running"), None)
    review_resource = run.parameter_snapshot.get("resources", {}).get("review", {})
    session = running or repository.create_ai_review_session(
        run_id,
        attempt_number=len(sessions) + 1,
        resource_ref=str(run.parameter_snapshot.get("resources", {}).get("review_ref", "legacy.analysis.default")),
        model_name=str(review_resource.get("model") or "configured-review-model"),
        strategy_version=str(run.parameter_snapshot.get("processing", {}).get("review_policy_version", "auto_review_v1")),
        parameter_snapshot={
            "resource": review_resource,
            "candidate_count": len(candidates),
            "max_candidates": max_candidates,
        },
        evidence_relative_path="review_result.json",
    )
    if run.status != "processing" or run.current_stage != "review":
        repository.transition_run(run_id, status="processing", stage="review", event_type="review_started")
    evidence_written = False
    try:
        result = _validate_review(adapter(payload), sent_candidates=sent)
        decisions = list(result.decisions)
        used_ranks = {item.rank for item in decisions}
        next_rank = max(used_ranks, default=0) + 1
        for candidate in candidates[max_candidates:]:
            while next_rank in used_ranks:
                next_rank += 1
            decisions.append(
                ReviewDecision(
                    candidate_id=str(candidate["id"]),
                    decision="rejected",
                    rank=next_rank,
                    reason="候选超出本次审阅上限",
                    rejection_reason_code="candidate_limit",
                )
            )
            used_ranks.add(next_rank)
            next_rank += 1
        evidence = {
            "format_version": 1,
            "overall_summary": sanitize_persisted_text(result.overall_summary),
            "warnings": result.warnings,
            "decisions": [_decision_evidence(item) for item in sorted(decisions, key=lambda item: item.rank)],
        }
        evidence_path = target / "review_result.json"
        _atomic_write_json(evidence_path, evidence)
        evidence_written = True
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        candidate_by_id = {str(item["id"]): item for item in candidates}
        storage_decisions: list[dict[str, Any]] = []
        outputs: list[dict[str, Any]] = []
        materials: list[dict[str, Any]] = []
        selected_clips: list[dict[str, Any]] = []
        source_name = safe_output_name(Path(run.latest_seen_path).name)
        selected_order = 0
        for decision in sorted(decisions, key=lambda item: item.rank):
            candidate = candidate_by_id[decision.candidate_id]
            clip = decision.selected_clip
            item: dict[str, Any] = {
                "candidate_id": decision.candidate_id,
                "decision": decision.decision,
                "rank": decision.rank,
                "candidate_type": candidate.get("clip_type", "highlight"),
                "source_start_ms": round(float(candidate["start"]) * 1000),
                "source_end_ms": round(float(candidate["end"]) * 1000),
                "hook": candidate.get("hook", ""),
                "core_value": candidate.get("core_value", ""),
                "reason": decision.reason,
                "rejection_reason_code": decision.rejection_reason_code,
                "risks": ([{"summary": candidate["risk"]}] if candidate.get("risk") else []),
                "transcript_excerpt": _candidate_payload(candidate)["transcript_excerpt"],
                "internal_sort_value": float(candidate.get("score", 0)),
            }
            if clip is not None:
                selected_order += 1
                output_id = new_id()
                file_name = f"{source_name}-clip-{selected_order:02d}-{output_id[:8]}.mp4"
                item.update(
                    selected_start_ms=round(clip.source_start * 1000),
                    selected_end_ms=round(clip.source_end * 1000),
                    remove_ranges=[[round(start * 1000), round(end * 1000)] for start, end in clip.remove_ranges],
                )
                outputs.append(
                    {
                        "output_id": output_id,
                        "candidate_id": decision.candidate_id,
                        "display_order": selected_order,
                        "relative_path": file_name,
                        "file_name": file_name,
                    }
                )
                assert decision.material is not None
                titles = [
                    {"title_id": f"{output_id}-title-{index}", "text": title}
                    for index, title in enumerate(decision.material.titles, start=1)
                ]
                materials.append(
                    {
                        "output_id": output_id,
                        "title_candidates": titles,
                        "preferred_title_id": titles[0]["title_id"],
                        "description": decision.material.description,
                        "tags": decision.material.tags,
                        "generation_source": "ai_review",
                        "status": "ready",
                    }
                )
                selected_clips.append(clip.model_dump(mode="json"))
            else:
                item["remove_ranges"] = []
            storage_decisions.append(item)
        _atomic_write_json(target / "selected_clips.json", selected_clips)
        status = "selected" if selected_clips else "no_clip"
        registered = repository.register_verified_review(
            session.review_session_id,
            status=status,
            decisions=storage_decisions,
            outputs=outputs,
            materials=materials,
            overall_summary=result.overall_summary,
            warnings=result.warnings,
            evidence_relative_path="review_result.json",
            evidence_sha256=evidence_sha256,
        )
        _resolve_review_issues(repository, run_id)
        return registered
    except ProjectReviewError:
        (target / "review_result.tmp.json").unlink(missing_ok=True)
        _mark_review_failure(repository, run, session, code="ai_review_invalid")
        raise
    except Exception as exc:
        (target / "review_result.tmp.json").unlink(missing_ok=True)
        if evidence_written:
            repository.transition_run(
                run_id,
                status="processing",
                stage="review",
                event_type="review_registration_pending",
                detail={"evidence": "review_result.json"},
            )
            raise ProjectReviewError(
                "review_registration_pending",
                "validated review evidence is waiting for durable registration",
            ) from exc
        error_code = str(getattr(exc, "error_code", "ai_review_failed"))
        if error_code not in {"ai_resource_unavailable", "ai_review_failed"}:
            error_code = "ai_review_failed"
        retry_plan = (
            automatic_review_retry_plan(
                exc,
                attempt_number=session.attempt_number,
                now=(clock or (lambda: datetime.now(UTC)))(),
            )
            if error_code == "ai_review_failed"
            else AutomaticRetryPlan(False, None, 0, False)
        )
        _mark_review_failure(repository, run, session, code=error_code, retry_plan=retry_plan)
        raise ProjectReviewError(error_code, "review adapter failed", retryable=retry_plan.retry) from exc


def reconcile_review_evidence(
    repository: ProjectRepository,
    run_id: str,
    *,
    run_dir: str | Path,
) -> str:
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    sessions = repository.list_ai_review_sessions(run_id)
    if not sessions:
        return "none"
    session = sessions[-1]
    evidence_path = Path(run_dir) / str(session.evidence_relative_path or "review_result.json")
    if session.status == "running" and evidence_path.is_file():
        return "registration_pending"
    if session.status not in {"selected", "no_clip"}:
        return session.status
    actual_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest() if evidence_path.is_file() else None
    if actual_hash == session.evidence_sha256:
        return "verified"
    from .project_domain import normalize_utc

    timestamp = normalize_utc()
    with repository.transaction():
        repository.connection.execute(
            """UPDATE ai_review_sessions SET status = 'invalid', updated_at = ?
               WHERE review_session_id = ?""",
            (timestamp, session.review_session_id),
        )
    repository.transition_run(
        run_id,
        status="failed",
        stage="review",
        event_type="evidence_invalid",
        error_code="ai_review_invalid",
        error_summary="审阅证据缺失或校验不一致",
    )
    _functional_issue(repository, run, "ai_review_invalid")
    return "invalid"


def _selected_clip_for_output(repository: ProjectRepository, output_id: str) -> SelectedClip:
    output = repository.get_run_output(output_id)
    if output is None:
        raise KeyError(output_id)
    decisions = repository.get_candidate_decisions(output.review_session_id)
    decision = next(item for item in decisions if item.output_id == output_id)
    material = repository.get_output_material(output_id)
    title = "clip"
    if material and material.title_candidates:
        preferred = next(
            (item["text"] for item in material.title_candidates if item["title_id"] == material.preferred_title_id),
            material.title_candidates[0]["text"],
        )
        title = str(preferred)
    return SelectedClip(
        clip_id=decision.candidate_id,
        source_start=float(decision.selected_start_ms or decision.source_start_ms) / 1000,
        source_end=float(decision.selected_end_ms or decision.source_end_ms) / 1000,
        title=title,
        remove_ranges=[(start / 1000, end / 1000) for start, end in decision.remove_ranges],
        priority=output.display_order,
    )


def _default_renderer(
    source: Path,
    transcript: CorrectedTranscript,
    clip: SelectedClip,
    work_dir: Path,
    partial: Path,
) -> None:
    from .render_clips import render_single_clip

    render_single_clip(source, transcript, clip, work_dir=work_dir, output_path=partial)


def render_project_outputs(
    repository: ProjectRepository,
    run_id: str,
    *,
    run_dir: str | Path,
    renderer: ClipRenderer = _default_renderer,
    probe: MediaProbe = probe_media,
) -> RenderReport:
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    active_issues = repository.list_issues(run_id=run_id, active_only=True)
    overrides = next(
        (issue.operational_overrides for issue in active_issues if issue.status == "recovering" and issue.operational_overrides),
        {},
    )
    output_dir = Path(
        str(overrides.get("output_directory") or run.parameter_snapshot.get("output", {}).get("directory", ""))
    ).expanduser().resolve()
    if not output_directory_is_writable(output_dir):
        _functional_issue(repository, run, "output_unwritable")
        repository.transition_run(
            run_id,
            status="failed",
            stage="render",
            event_type="failed",
            error_code="output_unwritable",
            error_summary="输出目录不可写",
        )
        raise ProjectRenderError("output_unwritable", "output directory is not writable")
    source = Path(str(overrides.get("source_path") or run.latest_seen_path))
    if not source.is_file():
        repository.transition_run(
            run_id,
            status="failed",
            stage="read_source",
            event_type="failed",
            error_code="source_missing",
            error_summary="来源录像不存在",
        )
        _functional_issue(repository, run, "source_missing")
        raise ProjectRenderError("source_missing", "source recording is missing")
    target = Path(run_dir)
    transcript = CorrectedTranscript.model_validate(read_json(target / "transcript.json"))
    local_output_retry = any(
        issue.status == "recovering" and issue.recovery_capability == "retry_output"
        for issue in active_issues
    )
    if not local_output_retry:
        repository.transition_run(run_id, status="processing", stage="render", event_type="render_started")
    ready: list[str] = []
    failed: list[str] = []
    reused: list[str] = []
    for output in repository.list_run_outputs(run_id):
        final_path = output_dir / output.file_name
        partial = output_dir / f".venus-{output.output_id}.partial.mp4"
        if output.status == "ready":
            if final_path.is_file():
                try:
                    if _ready_output_is_verified(output, final_path, target, probe):
                        reused.append(output.output_id)
                        continue
                except Exception:  # noqa: BLE001 - any unverifiable ready file must be blocked.
                    pass
                repository.update_output_and_reproject_result(
                    output.output_id,
                    status="unreadable",
                    error_code="output_unreadable",
                    error_summary="已登记成片文件完整性校验失败",
                )
                failed.append(output.output_id)
                continue
            repository.update_output_and_reproject_result(
                output.output_id,
                status="missing",
                error_code="output_missing",
                error_summary="已登记成片文件不存在",
            )
            failed.append(output.output_id)
            continue
        if output.status == "rendering" and not final_path.exists() and partial.is_file():
            try:
                metadata = probe(partial)
                os.link(partial, final_path)
                partial.unlink()
                _write_output_integrity(target, output.output_id, final_path, metadata)
                repository.update_output_and_reproject_result(
                    output.output_id,
                    status="ready",
                    media_metadata=metadata.as_storage_dict(),
                )
                _resolve_render_issues(repository, run_id, output.output_id)
                reused.append(output.output_id)
                continue
            except Exception:  # noqa: BLE001 - an unverifiable partial must be rendered again.
                partial.unlink(missing_ok=True)
        if output.status == "rendering" and final_path.is_file():
            try:
                metadata = probe(final_path)
                _write_output_integrity(target, output.output_id, final_path, metadata)
                repository.update_output_and_reproject_result(
                    output.output_id,
                    status="ready",
                    media_metadata=metadata.as_storage_dict(),
                )
                _resolve_render_issues(repository, run_id, output.output_id)
                reused.append(output.output_id)
                continue
            except Exception:  # noqa: BLE001 - fall through to a durable conflict issue.
                pass
        if final_path.exists():
            repository.update_output_and_reproject_result(
                output.output_id,
                status="failed",
                error_code="render_failed",
                error_summary="目标文件已存在且无法证明属于当前成片",
            )
            _functional_issue(repository, run, "render_failed", output_id=output.output_id)
            failed.append(output.output_id)
            continue
        partial.unlink(missing_ok=True)
        try:
            repository.update_output_and_reproject_result(output.output_id, status="rendering")
            clip = _selected_clip_for_output(repository, output.output_id)
            renderer(source, transcript, clip, target / "outputs" / output.output_id, partial)
            if not partial.is_file():
                raise ProjectRenderError("render_failed", "renderer did not create the partial file")
            metadata = probe(partial)
            try:
                os.link(partial, final_path)
            except FileExistsError as exc:
                raise ProjectRenderError("render_failed", "target output appeared during rendering") from exc
            partial.unlink()
            _write_output_integrity(target, output.output_id, final_path, metadata)
            repository.update_output_and_reproject_result(
                output.output_id,
                status="ready",
                media_metadata=metadata.as_storage_dict(),
            )
            _resolve_render_issues(repository, run_id, output.output_id)
            ready.append(output.output_id)
        except Exception as exc:  # noqa: BLE001 - one output must not stop later outputs.
            partial.unlink(missing_ok=True)
            code = "storage_full" if isinstance(exc, OSError) and exc.errno == errno.ENOSPC else "render_failed"
            repository.update_output_and_reproject_result(
                output.output_id,
                status="failed",
                error_code=code,
                error_summary="成片渲染未成功",
            )
            existing = next(
                (
                    issue
                    for issue in repository.list_issues(run_id=run_id, active_only=True)
                    if issue.issue_code == code and issue.output_id == output.output_id
                ),
                None,
            )
            plan = automatic_render_retry_plan(
                exc,
                automatic_attempt_count=existing.automatic_attempt_count if existing else 0,
            )
            _functional_issue(
                repository,
                run,
                code,
                output_id=output.output_id,
                status="retrying" if plan.retry else "action_required",
                automatic_attempt_count=plan.automatic_attempt_count,
                next_retry_at=plan.next_retry_at,
                retry_exhausted=plan.exhausted,
            )
            failed.append(output.output_id)
    return RenderReport(run_id, tuple(ready), tuple(failed), tuple(reused))


class ProjectWorkerPool:
    """One durable review lane and one durable render lane; ticks never wait for model/FFmpeg work."""

    def __init__(
        self,
        *,
        review_adapter: Callable[[Settings, dict[str, Any]], Mapping[str, Any]] | None = None,
        renderer: ClipRenderer = _default_renderer,
        probe: MediaProbe = probe_media,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._review_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="venus-project-review")
        self._render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="venus-project-render")
        self._review_adapter = review_adapter
        self._renderer = renderer
        self._probe = probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._futures: dict[tuple[str, str], Future[Any]] = {}
        self._lock = threading.Lock()
        self._accepting = True

    def _review(self, service_dir: Path, settings: Settings, work_dir: Path, run_id: str) -> None:
        from .review_automation import run_structured_review_adapter

        with ProjectRepository(service_dir) as repository:
            run = repository.get_run(run_id)
            if run is None:
                return
            adapter = self._review_adapter or run_structured_review_adapter
            target = work_dir / "projects" / run.project_id / "runs" / run_id
            sessions = repository.list_ai_review_sessions(run_id)
            evidence_path = target / "review_result.json"
            if sessions and sessions[-1].status == "running" and evidence_path.is_file():
                def adapter_call(_settings: Settings, _payload: dict[str, Any]) -> Mapping[str, Any]:
                    return read_json(evidence_path)
            else:
                adapter_call = adapter
            run_project_review(
                repository,
                run_id,
                run_dir=target,
                max_candidates=settings.review_automation.model.max_candidates,
                adapter=lambda payload: adapter_call(settings, payload),
                clock=self._clock,
            )

    def _render(self, service_dir: Path, work_dir: Path, run_id: str) -> None:
        with ProjectRepository(service_dir) as repository:
            run = repository.get_run(run_id)
            if run is None:
                return
            render_project_outputs(
                repository,
                run_id,
                run_dir=work_dir / "projects" / run.project_id / "runs" / run_id,
                renderer=self._renderer,
                probe=self._probe,
            )

    def tick(
        self,
        repository: ProjectRepository,
        settings: Settings,
        *,
        work_dir: str | Path,
    ) -> WorkerTickReport:
        service_dir = repository.service_dir
        work_root = Path(work_dir).expanduser().resolve()
        completed: list[str] = []
        failed: list[str] = []
        scheduled_review: list[str] = []
        scheduled_render: list[str] = []
        with self._lock:
            for key, future in list(self._futures.items()):
                if not future.done():
                    continue
                self._futures.pop(key)
                if future.exception() is None:
                    completed.append(key[1])
                else:
                    failed.append(key[1])
            if not self._accepting:
                return WorkerTickReport((), (), tuple(completed), tuple(failed))
            active_run_ids = {run_id for _kind, run_id in self._futures}
            review_busy = any(kind == "review" for kind, _run_id in self._futures)
            render_busy = any(kind == "render" for kind, _run_id in self._futures)
            for run in repository.list_runs():
                if run.parameter_snapshot.get("schema_version") != 2 or run.run_id in active_run_ids:
                    continue
                target = work_root / "projects" / run.project_id / "runs" / run.run_id
                sessions = repository.list_ai_review_sessions(run.run_id)
                verified = next((item for item in reversed(sessions) if item.status in {"selected", "no_clip"}), None)
                if verified is not None and reconcile_review_evidence(
                    repository, run.run_id, run_dir=target
                ) != "verified":
                    verified = None
                    run = repository.get_run(run.run_id) or run
                retry_issue = next(
                    (
                        issue
                        for issue in repository.list_issues(run_id=run.run_id, active_only=True)
                        if issue.status == "retrying" and issue.next_retry_at
                    ),
                    None,
                )
                retry_due = bool(
                    retry_issue
                    and datetime.fromisoformat(str(retry_issue.next_retry_at).replace("Z", "+00:00")) <= self._clock()
                )
                if retry_due and retry_issue and retry_issue.issue_code == "ai_review_failed":
                    repository.transition_run(
                        run.run_id,
                        status="processing",
                        stage="review",
                        event_type="automatic_retry_started",
                    )
                    run = repository.get_run(run.run_id) or run
                elif retry_due and retry_issue and retry_issue.issue_code == "render_failed" and retry_issue.output_id:
                    with repository.transaction():
                        repository.connection.execute(
                            "UPDATE run_outputs SET status = 'pending' WHERE output_id = ? AND status = 'failed'",
                            (retry_issue.output_id,),
                        )
                if (
                    not review_busy
                    and verified is None
                    and run.status in {"queued", "processing"}
                    and run.current_stage == "review"
                    and (target / "codex_brief.json").is_file()
                ):
                    self._futures[("review", run.run_id)] = self._review_executor.submit(
                        self._review, service_dir, settings, work_root, run.run_id
                    )
                    review_busy = True
                    scheduled_review.append(run.run_id)
                    active_run_ids.add(run.run_id)
                    continue
                if (
                    not render_busy
                    and verified is not None
                    and verified.status == "selected"
                    and any(output.status == "pending" for output in repository.list_run_outputs(run.run_id))
                ):
                    self._futures[("render", run.run_id)] = self._render_executor.submit(
                        self._render, service_dir, work_root, run.run_id
                    )
                    render_busy = True
                    scheduled_render.append(run.run_id)
                    active_run_ids.add(run.run_id)
        return WorkerTickReport(
            tuple(scheduled_review),
            tuple(scheduled_render),
            tuple(dict.fromkeys(completed)),
            tuple(dict.fromkeys(failed)),
        )

    def wait_for_idle(self, timeout: float = 10.0) -> None:
        with self._lock:
            futures = list(self._futures.values())
        for future in futures:
            future.result(timeout=timeout)

    def shutdown(self, *, wait: bool = True, grace_seconds: float = 2.0) -> None:
        with self._lock:
            self._accepting = False
            futures = list(self._futures.values())
        all_done = not futures
        if wait and futures:
            deadline = time.monotonic() + max(0.0, grace_seconds)
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    future.result(timeout=remaining)
                except TimeoutError:
                    break
                except Exception:  # noqa: BLE001 - failure is already persisted by the worker boundary.
                    continue
            all_done = all(future.done() for future in futures)
        self._review_executor.shutdown(wait=wait and all_done, cancel_futures=True)
        self._render_executor.shutdown(wait=wait and all_done, cancel_futures=True)


_WORKER_POOLS: dict[Path, ProjectWorkerPool] = {}
_WORKER_POOLS_LOCK = threading.Lock()


def tick_project_result_workers(
    repository: ProjectRepository,
    settings: Settings,
    *,
    work_dir: str | Path,
) -> WorkerTickReport:
    key = repository.service_dir.resolve()
    with _WORKER_POOLS_LOCK:
        pool = _WORKER_POOLS.setdefault(key, ProjectWorkerPool())
    return pool.tick(repository, settings, work_dir=work_dir)


def shutdown_project_result_workers(service_dir: str | Path, *, wait: bool = True) -> None:
    key = Path(service_dir).expanduser().resolve()
    with _WORKER_POOLS_LOCK:
        pool = _WORKER_POOLS.pop(key, None)
    if pool is not None:
        pool.shutdown(wait=wait)
