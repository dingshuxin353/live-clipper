"""Refine merged clip candidates with Agnes before final review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import ClipCandidate, CorrectedTranscript
from .prompt_loader import load_prompt
from .utils import read_json, write_failure_log, write_json


def emit_progress(message: str) -> None:
    print(message, flush=True)


def candidate_context(
    candidate: ClipCandidate,
    transcript: CorrectedTranscript,
    *,
    context_seconds: int = 8,
) -> list[dict[str, Any]]:
    context_start = max(0.0, candidate.start - max(context_seconds, candidate.suggested_context_before))
    context_end = candidate.end + max(context_seconds, candidate.suggested_context_after)
    return [
        sentence.model_dump()
        for sentence in transcript.sentences
        if sentence.start < context_end and sentence.end > context_start
    ]


def _coerce_score(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return min(10.0, max(0.0, score))


def _normalize_refinement(candidate: ClipCandidate, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Refinement response must be a JSON object")
    if payload.get("candidate_id") != candidate.id:
        raise ValueError("Refinement response candidate_id must match the requested candidate")

    keep = bool(payload.get("keep", True))
    refined_score = _coerce_score(payload.get("refined_score"), candidate.score)
    return {
        "candidate_id": candidate.id,
        "keep": keep,
        "refined_score": refined_score,
        "commercial_fit": _coerce_score(payload.get("commercial_fit"), 0.0),
        "hook_strength": _coerce_score(payload.get("hook_strength"), 0.0),
        "standalone_value": _coerce_score(payload.get("standalone_value"), 0.0),
        "clarity": _coerce_score(payload.get("clarity"), 0.0),
        "suggested_title": str(payload.get("suggested_title") or candidate.hook),
        "selection_reason": str(payload.get("selection_reason") or candidate.reason),
        "weaknesses": payload.get("weaknesses", []) if isinstance(payload.get("weaknesses", []), list) else [],
        "recommended_adjustments": (
            payload.get("recommended_adjustments", [])
            if isinstance(payload.get("recommended_adjustments", []), list)
            else []
        ),
    }


def refine_candidates_file(
    candidates_path: Path,
    transcript_path: Path,
    output_path: Path,
    client: Any,
    *,
    top_n: int = 25,
    prompt_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if top_n <= 0:
        raise ValueError("top_n must be greater than 0")

    system_prompt = load_prompt("cheap_refine_candidate.md", "cheap candidate refinement prompt", prompt_dir=prompt_dir)
    candidates = [ClipCandidate.model_validate(item) for item in read_json(candidates_path)]
    transcript = CorrectedTranscript.model_validate(read_json(transcript_path))
    ranked = sorted(candidates, key=lambda item: (-item.score, item.start, item.end))[:top_n]
    refined_items: list[dict[str, Any]] = []

    emit_progress(f"[复评] 开始: 将用 Agnes 复评 {len(ranked)}/{len(candidates)} 条候选")
    for index, candidate in enumerate(ranked, start=1):
        emit_progress(f"[复评] {index}/{len(ranked)} {candidate.id}: 正在请求 Agnes")
        payload = {
            "candidate": candidate.model_dump(),
            "context": candidate_context(candidate, transcript),
            "business_goal": "Promote Agnes as a practical AI model for real work, using useful livestream highlights.",
        }
        try:
            refinement = _normalize_refinement(candidate, client.complete_json(system_prompt, payload, max_tokens=2048))
        except ValueError as exc:
            write_failure_log(
                "refine_candidates_validation_failure",
                {
                    "candidate_id": candidate.id,
                    "user_payload": payload,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            refinement = {
                "candidate_id": candidate.id,
                "keep": True,
                "refined_score": candidate.score,
                "commercial_fit": 0.0,
                "hook_strength": 0.0,
                "standalone_value": 0.0,
                "clarity": 0.0,
                "suggested_title": candidate.hook,
                "selection_reason": candidate.reason,
                "weaknesses": ["Agnes refinement response was invalid; kept original candidate for review."],
                "recommended_adjustments": [],
            }

        if not refinement["keep"]:
            emit_progress(f"[复评] {index}/{len(ranked)} {candidate.id}: Agnes 判定不进入最终候选")
            continue

        item = candidate.model_dump()
        item["score"] = refinement["refined_score"]
        item["hook"] = refinement["suggested_title"]
        item["reason"] = refinement["selection_reason"]
        item["agnes_refinement"] = refinement
        refined_items.append(item)
        emit_progress(
            f"[复评] {index}/{len(ranked)} {candidate.id}: 保留, 复评分 {refinement['refined_score']:.2f}"
        )

    refined_items.sort(key=lambda item: (-item["agnes_refinement"]["refined_score"], item["start"], item["end"]))
    write_json(output_path, refined_items)
    emit_progress(f"[复评] 全部完成: 保留 {len(refined_items)} 条候选 -> {output_path}")
    return refined_items
