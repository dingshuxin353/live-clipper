from __future__ import annotations

import pytest

from live_clipper.build_codex_brief import (
    build_codex_brief_file,
    build_codex_review_markdown,
    build_selected_clips_template,
)
from live_clipper.models import ClipCandidate, CorrectedTranscript, TranscriptSentence
from live_clipper.utils import read_json, write_json


def test_build_codex_brief_includes_candidate_context_without_full_transcript(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "codex_brief.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="强钩子",
            core_value="一个观点",
            reason="结构完整",
            risk=None,
        ).model_dump()
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[
        TranscriptSentence(start=0, end=5, text="太早"),
        TranscriptSentence(start=9, end=12, text="上下文前"),
        TranscriptSentence(start=15, end=20, text="核心内容"),
        TranscriptSentence(start=32, end=35, text="上下文后"),
        TranscriptSentence(start=100, end=105, text="太远"),
    ]).model_dump())

    brief = build_codex_brief_file(candidates_path, transcript_path, output_path, source_name="sample.mp4")

    assert brief["source_name"] == "sample.mp4"
    assert brief["candidates"][0]["id"] == "clip-1"
    assert [item["text"] for item in brief["candidates"][0]["context"]] == [
        "上下文前",
        "核心内容",
        "上下文后",
    ]
    assert "transcript" not in brief
    assert read_json(output_path)["candidates"][0]["id"] == "clip-1"


def test_build_codex_brief_includes_review_contract(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "codex_brief.json"
    write_json(candidates_path, [])
    write_json(transcript_path, CorrectedTranscript(sentences=[]).model_dump())

    brief = build_codex_brief_file(candidates_path, transcript_path, output_path, source_name="sample.mp4")

    assert "strong opening hook" in brief["review_instructions"]
    assert brief["expected_output"]["path"] == "selected_clips.json"
    assert brief["expected_output"]["type"] == "array"
    assert set(brief["expected_output"]["required_fields"]) == {
        "clip_id",
        "source_start",
        "source_end",
        "title",
    }


def test_build_codex_brief_context_respects_candidate_suggested_context(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "codex_brief.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=100,
            end=120,
            score=9,
            clip_type="insight",
            hook="强钩子",
            core_value="一个观点",
            reason="结构完整",
            suggested_context_before=20,
            suggested_context_after=15,
        ).model_dump()
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[
        TranscriptSentence(start=79, end=80, text="刚好太早"),
        TranscriptSentence(start=80, end=90, text="建议前文"),
        TranscriptSentence(start=100, end=110, text="核心内容"),
        TranscriptSentence(start=130, end=135, text="建议后文"),
        TranscriptSentence(start=135, end=136, text="刚好太晚"),
    ]).model_dump())

    brief = build_codex_brief_file(candidates_path, transcript_path, output_path, source_name="sample.mp4")

    assert [item["text"] for item in brief["candidates"][0]["context"]] == [
        "建议前文",
        "核心内容",
        "建议后文",
    ]


def test_build_codex_brief_preserves_refinement_metadata(tmp_path):
    candidates_path = tmp_path / "refined_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "codex_brief.json"
    write_json(candidates_path, [
        {
            **ClipCandidate(
                id="clip-1",
                start=10,
                end=30,
                score=8.8,
                clip_type="workflow",
                hook="AI 工作流真实案例",
                core_value="真实工作问题",
                reason="适合宣传 Agnes",
            ).model_dump(),
            "agnes_refinement": {
                "refined_score": 8.8,
                "commercial_fit": 9,
                "selection_reason": "展示 Agnes 适合解决真实工作问题",
            },
        }
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[
        TranscriptSentence(start=12, end=15, text="核心内容"),
    ]).model_dump())

    brief = build_codex_brief_file(candidates_path, transcript_path, output_path, source_name="sample.mp4")

    assert brief["candidates"][0]["agnes_refinement"]["commercial_fit"] == 9
    assert read_json(output_path)["candidates"][0]["agnes_refinement"]["refined_score"] == 8.8


def test_build_codex_brief_rejects_duplicate_candidate_ids(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "codex_brief.json"
    candidate = ClipCandidate(
        id="clip-1",
        start=10,
        end=30,
        score=9,
        clip_type="insight",
        hook="强钩子",
        core_value="一个观点",
        reason="结构完整",
    ).model_dump()
    write_json(candidates_path, [
        candidate,
        {**candidate, "start": 60, "end": 90},
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[]).model_dump())

    with pytest.raises(ValueError, match="Duplicate candidate id"):
        build_codex_brief_file(candidates_path, transcript_path, output_path, source_name="sample.mp4")

    assert not output_path.exists()


def test_build_codex_review_markdown_points_to_brief_and_selection_output():
    brief = {
        "source_name": "sample.mp4",
        "candidate_count": 2,
        "expected_output": {
            "path": "selected_clips.json",
            "required_fields": ["clip_id", "source_start", "source_end", "title"],
        },
    }

    markdown = build_codex_review_markdown(
        brief,
        brief_path="codex_brief.json",
        selection_path="selected_clips.json",
    )

    assert "# Codex Clip Review" in markdown
    assert "sample.mp4" in markdown
    assert "codex_brief.json" in markdown
    assert "selected_clips.json" in markdown
    assert "clip_id" in markdown
    assert "2 candidates" in markdown
    assert "at most once" in markdown
    assert "candidate IDs as unique" in markdown
    assert "letters, numbers, dots, underscores, or hyphens" in markdown


def test_build_selected_clips_template_uses_top_candidate_fields():
    brief = {
        "candidates": [
            {
                "id": "clip-1",
                "start": 10.0,
                "end": 30.0,
                "hook": "强钩子",
                "score": 9,
            }
        ]
    }

    template = build_selected_clips_template(brief)

    assert template == [
        {
            "clip_id": "clip-1",
            "source_start": 10.0,
            "source_end": 30.0,
            "title": "强钩子",
            "remove_ranges": [],
            "subtitle_highlights": [],
            "format": "horizontal_highlight",
            "priority": 1,
        }
    ]
