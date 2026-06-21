"""Merge overlapping or duplicate clip candidates."""

from __future__ import annotations

from pathlib import Path

from .models import ClipCandidate
from .utils import read_json, write_json


def _overlap_seconds(left: ClipCandidate, right: ClipCandidate) -> float:
    return max(0.0, min(left.end, right.end) - max(left.start, right.start))


def _should_merge(left: ClipCandidate, right: ClipCandidate) -> bool:
    overlap = _overlap_seconds(left, right)
    shorter = min(left.end - left.start, right.end - right.start)
    if shorter > 0 and overlap / shorter > 0.5:
        return True
    return abs(left.start - right.start) < 20 and abs(left.end - right.end) < 20


def merge_candidates(candidates: list[ClipCandidate]) -> list[ClipCandidate]:
    groups: list[list[ClipCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: (item.start, item.end)):
        for group in groups:
            if any(_should_merge(candidate, existing) for existing in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    merged: list[ClipCandidate] = []
    for group in groups:
        best = max(group, key=lambda item: item.score).model_copy()
        best.start = min(item.start for item in group)
        best.end = max(item.end for item in group)
        merged.append(best)

    return sorted(merged, key=lambda item: (item.start, -item.score))


def validate_unique_candidate_ids(candidates: list[ClipCandidate]) -> None:
    seen_ids: set[str] = set()
    for candidate in candidates:
        if candidate.id in seen_ids:
            raise ValueError(f"Duplicate candidate id: {candidate.id}")
        seen_ids.add(candidate.id)


def merge_candidates_file(input_path: Path, output_path: Path) -> list[ClipCandidate]:
    candidates = [ClipCandidate.model_validate(item) for item in read_json(input_path)]
    validate_unique_candidate_ids(candidates)
    merged = merge_candidates(candidates)
    write_json(output_path, [candidate.model_dump() for candidate in merged])
    return merged
