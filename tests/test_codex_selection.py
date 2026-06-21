from __future__ import annotations

import pytest

from live_clipper.codex_selection import validate_selected_clips_file
from live_clipper.models import ClipCandidate, SelectedClip
from live_clipper.utils import write_json


def test_validate_selected_clips_file_rejects_unknown_clip_id(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="missing",
            source_start=10,
            source_end=30,
            title="标题",
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="Unknown clip_id"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_accepts_known_clip_id_and_valid_time(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=30,
            title="标题",
        ).model_dump()
    ])

    selected = validate_selected_clips_file(selection_path, candidates_path)

    assert selected[0].clip_id == "clip-1"


def test_validate_selected_clips_file_rejects_duplicate_clip_ids(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题 1",
        ).model_dump(),
        SelectedClip(
            clip_id="clip-1",
            source_start=20,
            source_end=30,
            title="标题 2",
        ).model_dump(),
    ])

    with pytest.raises(ValueError, match="Duplicate clip_id"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_rejects_duplicate_candidate_ids(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=20,
            score=9,
            clip_type="insight",
            hook="hook 1",
            core_value="value",
            reason="reason",
        ).model_dump(),
        ClipCandidate(
            id="clip-1",
            start=30,
            end=40,
            score=8,
            clip_type="insight",
            hook="hook 2",
            core_value="value",
            reason="reason",
        ).model_dump(),
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="Duplicate candidate id"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_rejects_selection_outside_candidate_range(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
            suggested_context_before=2,
            suggested_context_after=3,
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=7,
            source_end=34,
            title="标题",
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="outside allowed candidate context"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_allows_suggested_context(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
            suggested_context_before=2,
            suggested_context_after=3,
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=8,
            source_end=33,
            title="标题",
        ).model_dump()
    ])

    selected = validate_selected_clips_file(selection_path, candidates_path)

    assert selected[0].source_start == 8


def test_validate_selected_clips_file_rejects_invalid_remove_ranges(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=30,
            title="标题",
            remove_ranges=[(29, 31)],
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="Invalid remove range"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_rejects_remove_ranges_that_delete_entire_clip(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=30,
            title="标题",
            remove_ranges=[(10, 30)],
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="remove ranges delete entire clip"):
        validate_selected_clips_file(selection_path, candidates_path)


def test_validate_selected_clips_file_rejects_overlapping_remove_ranges(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    selection_path = tmp_path / "selected_clips.json"
    write_json(candidates_path, [
        ClipCandidate(
            id="clip-1",
            start=10,
            end=30,
            score=9,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        ).model_dump()
    ])
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=30,
            title="标题",
            remove_ranges=[(12, 15), (14, 16)],
        ).model_dump()
    ])

    with pytest.raises(ValueError, match="Overlapping remove ranges"):
        validate_selected_clips_file(selection_path, candidates_path)
