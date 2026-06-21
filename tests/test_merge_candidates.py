from __future__ import annotations

import pytest

from live_clipper.merge_candidates import merge_candidates, merge_candidates_file
from live_clipper.models import ClipCandidate
from live_clipper.utils import read_json, write_json


def candidate(id_, start, end, score):
    return ClipCandidate(
        id=id_,
        start=start,
        end=end,
        score=score,
        clip_type="insight",
        hook=f"hook {id_}",
        core_value="value",
        reason="reason",
    )


def test_merge_candidates_combines_heavily_overlapping_group_and_keeps_best_text():
    merged = merge_candidates([
        candidate("a", 10, 60, 7.0),
        candidate("b", 20, 70, 9.0),
        candidate("c", 200, 240, 8.0),
    ])

    assert [item.id for item in merged] == ["b", "c"]
    assert merged[0].start == 10
    assert merged[0].end == 70
    assert merged[0].score == 9.0


def test_merge_candidates_file_serializes_result(tmp_path):
    input_path = tmp_path / "cheap_candidates.json"
    output_path = tmp_path / "merged_candidates.json"
    write_json(input_path, [
        candidate("a", 10, 60, 7.0).model_dump(),
        candidate("b", 20, 70, 9.0).model_dump(),
    ])

    merge_candidates_file(input_path, output_path)

    assert read_json(output_path)[0]["id"] == "b"
    assert read_json(output_path)[0]["start"] == 10


def test_merge_candidates_file_rejects_duplicate_candidate_ids(tmp_path):
    input_path = tmp_path / "cheap_candidates.json"
    output_path = tmp_path / "merged_candidates.json"
    write_json(input_path, [
        candidate("dup-clip", 10, 20, 7.0).model_dump(),
        candidate("dup-clip", 100, 120, 8.0).model_dump(),
    ])

    with pytest.raises(ValueError, match="Duplicate candidate id"):
        merge_candidates_file(input_path, output_path)

    assert not output_path.exists()
