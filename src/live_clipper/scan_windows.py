"""Batch scan transcript windows for candidate clips."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .cheap_model_client import CheapModelServiceError
from .models import ClipCandidate, TranscriptWindow
from .prompt_loader import load_prompt
from .utils import read_json, write_failure_log, write_json


def emit_progress(message: str) -> None:
    print(message, flush=True)


def normalize_candidate_payload(candidate_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate_payload)
    for key in ("suggested_context_before", "suggested_context_after"):
        if key not in normalized:
            continue
        try:
            normalized[key] = float(normalized[key])
        except (TypeError, ValueError):
            normalized.pop(key)
    return normalized


def validate_scan_result(result: Any, window: TranscriptWindow, seen_ids: set[str] | None = None) -> list[ClipCandidate]:
    if not isinstance(result, dict) or result.get("window_id") != window.id or not isinstance(result.get("candidates"), list):
        raise CheapModelServiceError("analysis_output_invalid")
    identifiers = set(seen_ids or ())
    candidates = []
    for index, item in enumerate(result["candidates"], start=1):
        if not isinstance(item, dict):
            raise CheapModelServiceError("analysis_output_invalid")
        payload = normalize_candidate_payload(item)
        payload.setdefault("id", f"{window.id}-c{index:03d}")
        try:
            candidate = ClipCandidate.model_validate(payload)
        except ValidationError:
            raise CheapModelServiceError("analysis_output_invalid") from None
        if candidate.start < window.start or candidate.end > window.end or candidate.id in identifiers:
            raise CheapModelServiceError("analysis_output_invalid")
        identifiers.add(candidate.id)
        candidates.append(candidate)
    return candidates


def scan_checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")


def write_scan_checkpoint(
    checkpoint_path: Path,
    processed_window_ids: list[str],
    candidates: list[ClipCandidate],
) -> None:
    write_json(
        checkpoint_path,
        {
            "processed_window_ids": processed_window_ids,
            "candidates": [candidate.model_dump() for candidate in candidates],
        },
    )


def load_scan_checkpoint(checkpoint_path: Path) -> tuple[list[str], list[ClipCandidate]]:
    checkpoint = read_json(checkpoint_path)
    processed_window_ids = list(checkpoint.get("processed_window_ids", []))
    candidates = [
        ClipCandidate.model_validate(item)
        for item in checkpoint.get("candidates", [])
    ]
    return processed_window_ids, candidates


def checkpoint_processed_window(
    checkpoint_path: Path,
    processed_window_ids: list[str],
    processed_window_id_set: set[str],
    window_id: str,
    candidates: list[ClipCandidate],
) -> None:
    processed_window_ids.append(window_id)
    processed_window_id_set.add(window_id)
    write_scan_checkpoint(checkpoint_path, processed_window_ids, candidates)


def scan_windows_file(
    windows_path: Path,
    output_path: Path,
    client: Any,
    *,
    resume: bool = False,
    prompt_dir: Path | None = None,
    request_parameters: dict[str, Any] | None = None,
) -> list[ClipCandidate]:
    system_prompt = load_prompt("cheap_scan_window.md", "cheap scan prompt", prompt_dir=prompt_dir)
    windows = [TranscriptWindow.model_validate(item) for item in read_json(windows_path)]
    checkpoint_path = scan_checkpoint_path(output_path)
    if resume and checkpoint_path.exists():
        processed_window_ids, candidates = load_scan_checkpoint(checkpoint_path)
    else:
        processed_window_ids = []
        candidates = []
    processed_window_id_set = set(processed_window_ids)
    seen_candidate_ids = {candidate.id for candidate in candidates}
    total_windows = len(windows)
    emit_progress(
        f"[候选扫描] 开始: 已完成 {len(processed_window_ids)}/{total_windows} 个窗口, "
        f"已加载 {len(candidates)} 条候选"
    )

    for window_index, window in enumerate(windows, start=1):
        if window.id in processed_window_id_set:
            emit_progress(f"[候选扫描] {window_index}/{total_windows} {window.id}: 复用断点, 跳过")
            continue
        payload = window.model_dump()
        before_count = len(candidates)
        emit_progress(f"[候选扫描] {window_index}/{total_windows} {window.id}: 正在请求 Agnes")
        result = client.complete_json(system_prompt, payload, **(request_parameters or {"max_tokens": 4096, "temperature": 0.1}))
        try:
            validated = validate_scan_result(result, window, seen_candidate_ids)
        except CheapModelServiceError:
            write_failure_log("scan_windows_validation_failure", {"window_id": window.id, "code": "analysis_output_invalid"})
            emit_progress(f"[候选扫描] {window_index}/{total_windows} {window.id}: 分析响应不符合候选合同，处理已停止")
            raise
        candidates.extend(validated)
        seen_candidate_ids.update(candidate.id for candidate in validated)
        checkpoint_processed_window(checkpoint_path, processed_window_ids, processed_window_id_set, window.id, candidates)
        emit_progress(
            f"[候选扫描] {window_index}/{total_windows} {window.id}: 完成, "
            f"新增 {len(candidates) - before_count} 条, 跳过 0 条, 当前累计 {len(candidates)} 条"
        )

    write_json(output_path, [candidate.model_dump() for candidate in candidates])
    checkpoint_path.unlink(missing_ok=True)
    emit_progress(f"[候选扫描] 全部完成: 共 {len(candidates)} 条候选 -> {output_path}")
    return candidates
