"""Helpers for validating Codex-produced clip selections."""

from __future__ import annotations

from pathlib import Path

from .models import ClipCandidate, SelectedClip
from .utils import read_json


def remaining_duration_after_removes(
    source_start: float,
    source_end: float,
    remove_ranges: list[tuple[float, float]],
) -> float:
    cursor = source_start
    kept = 0.0
    for remove_start, remove_end in sorted(remove_ranges):
        if remove_start < cursor:
            raise ValueError("Overlapping remove ranges")
        kept += max(0.0, remove_start - cursor)
        cursor = remove_end
    kept += max(0.0, source_end - cursor)
    return kept


def validate_selected_clips_file(
    selection_path: Path,
    candidates_path: Path,
) -> list[SelectedClip]:
    candidates = [ClipCandidate.model_validate(item) for item in read_json(candidates_path)]
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if candidate.id in candidate_ids:
            raise ValueError(f"Duplicate candidate id: {candidate.id}")
        candidate_ids.add(candidate.id)
    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    selected = [SelectedClip.model_validate(item) for item in read_json(selection_path)]
    seen_clip_ids: set[str] = set()

    for clip in selected:
        if clip.clip_id in seen_clip_ids:
            raise ValueError(f"Duplicate clip_id: {clip.clip_id}")
        seen_clip_ids.add(clip.clip_id)
        candidate = candidates_by_id.get(clip.clip_id)
        if candidate is None:
            raise ValueError(f"Unknown clip_id: {clip.clip_id}")
        if clip.source_start >= clip.source_end:
            raise ValueError(f"Invalid time range for clip_id: {clip.clip_id}")
        allowed_start = max(0.0, candidate.start - candidate.suggested_context_before)
        allowed_end = candidate.end + candidate.suggested_context_after
        if clip.source_start < allowed_start or clip.source_end > allowed_end:
            raise ValueError(f"Selection outside allowed candidate context for clip_id: {clip.clip_id}")
        for remove_start, remove_end in clip.remove_ranges:
            if (
                remove_start >= remove_end
                or remove_start < clip.source_start
                or remove_end > clip.source_end
            ):
                raise ValueError(f"Invalid remove range for clip_id: {clip.clip_id}")
        if clip.remove_ranges:
            try:
                remaining_duration = remaining_duration_after_removes(
                    clip.source_start,
                    clip.source_end,
                    clip.remove_ranges,
                )
            except ValueError as exc:
                raise ValueError(f"Overlapping remove ranges for clip_id: {clip.clip_id}") from exc
            if remaining_duration <= 0:
                raise ValueError(f"remove ranges delete entire clip for clip_id: {clip.clip_id}")

    return selected
