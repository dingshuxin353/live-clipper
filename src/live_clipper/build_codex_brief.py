"""Build the compact candidate package reviewed by Codex."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .merge_candidates import validate_unique_candidate_ids
from .models import ClipCandidate, CorrectedTranscript
from .prompt_loader import load_prompt
from .utils import read_json, write_json


def build_codex_brief_file(
    candidates_path: Path,
    transcript_path: Path,
    output_path: Path,
    *,
    source_name: str,
    context_seconds: int = 5,
    prompt_dir: Path | None = None,
) -> dict[str, Any]:
    raw_candidates = read_json(candidates_path)
    candidates = [ClipCandidate.model_validate(item) for item in raw_candidates]
    validate_unique_candidate_ids(candidates)
    transcript = CorrectedTranscript.model_validate(read_json(transcript_path))
    brief_candidates: list[dict[str, Any]] = []

    for candidate, raw_item in zip(candidates, raw_candidates, strict=True):
        context_before = max(context_seconds, candidate.suggested_context_before)
        context_after = max(context_seconds, candidate.suggested_context_after)
        context_start = max(0.0, candidate.start - context_before)
        context_end = candidate.end + context_after
        context = [
            sentence.model_dump()
            for sentence in transcript.sentences
            if sentence.start < context_end and sentence.end > context_start
        ]
        item = candidate.model_dump()
        if isinstance(raw_item, dict):
            for key, value in raw_item.items():
                if key not in item:
                    item[key] = value
        item["context"] = context
        brief_candidates.append(item)

    brief = {
        "source_name": source_name,
        "review_instructions": load_prompt("codex_select_clips.md", "Codex selection prompt", prompt_dir=prompt_dir),
        "expected_output": {
            "path": "selected_clips.json",
            "type": "array",
            "required_fields": ["clip_id", "source_start", "source_end", "title"],
            "optional_fields": ["remove_ranges", "subtitle_highlights", "format", "priority"],
        },
        "candidate_count": len(brief_candidates),
        "candidates": brief_candidates,
    }
    write_json(output_path, brief)
    return brief


def build_codex_review_markdown(
    brief: dict[str, Any],
    *,
    brief_path: str,
    selection_path: str,
) -> str:
    required_fields = ", ".join(brief["expected_output"]["required_fields"])
    candidate_count = brief["candidate_count"]
    return (
        "# Codex Clip Review\n\n"
        f"Source: `{brief['source_name']}`\n\n"
        f"Review `{brief_path}` and write `{selection_path}`.\n\n"
        f"The brief contains {candidate_count} candidates. Select only clips that are worth rendering.\n\n"
        "Output contract:\n\n"
        f"- Write JSON array to `{selection_path}`.\n"
        f"- Required fields: {required_fields}.\n"
        "- Select each `clip_id` at most once.\n"
        "- Treat candidate IDs as unique; do not invent duplicate `clip_id` values.\n"
        "- Keep `clip_id` values filename-safe: letters, numbers, dots, underscores, or hyphens only.\n"
        "- Keep `source_start` and `source_end` inside the candidate context.\n"
        "- Use `remove_ranges` only when an otherwise strong clip has a small removable section.\n"
        "- `remove_ranges` MUST be an array of [start, end] number pairs in seconds, e.g. [[12.5, 18.0]]. "
        "Never use objects like {\"start\": ..., \"end\": ...}.\n"
    )


def build_selected_clips_template(brief: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = brief.get("candidates", [])
    if not candidates:
        return []
    candidate = candidates[0]
    return [
        {
            "clip_id": candidate["id"],
            "source_start": candidate["start"],
            "source_end": candidate["end"],
            "title": candidate.get("hook") or candidate["id"],
            "remove_ranges": [],
            "subtitle_highlights": [],
            "format": "horizontal_highlight",
            "priority": 1,
        }
    ]
