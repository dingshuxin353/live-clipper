from __future__ import annotations

import pytest

from live_clipper.project_projection import (
    project_projection_v2,
    project_result_projection,
    result_workload_counts,
)


def _decision(candidate_id: str, decision: str, output_id: str | None = None):
    return {"candidate_id": candidate_id, "decision": decision, "output_id": output_id}


def _output(output_id: str, status: str, *, duration_ms: int = 1000):
    item = {"output_id": output_id, "status": status}
    if status == "ready":
        item.update(
            duration_ms=duration_ms,
            width=1920,
            height=1080,
            container="mp4",
            video_codec="h264",
            byte_size=100,
        )
    return item


@pytest.mark.parametrize(
    ("review_status", "decisions", "outputs", "material_problems", "expected"),
    [
        ("no_clip", [_decision("a", "rejected")], [], 0, "no_clip"),
        ("selected", [_decision("a", "selected", "o")], [_output("o", "ready")], 0, "clips_ready"),
        (
            "selected",
            [_decision("a", "selected", "o1"), _decision("b", "selected", "o2")],
            [_output("o1", "ready"), _output("o2", "failed")],
            0,
            "partial",
        ),
        ("selected", [_decision("a", "selected", "o")], [_output("o", "failed")], 0, "unavailable"),
        ("selected", [_decision("a", "selected", "o")], [_output("o", "ready")], 1, "partial"),
    ],
)
def test_result_projection_has_one_truth_table(review_status, decisions, outputs, material_problems, expected):
    projected = project_result_projection(
        review_status=review_status,
        decisions=decisions,
        outputs=outputs,
        material_problem_count=material_problems,
    )
    assert projected.result_type == expected
    assert projected.candidate_count == projected.selected_count + projected.rejected_count


def test_result_projection_rejects_broken_decision_output_relationships():
    with pytest.raises(ValueError, match="selected decision"):
        project_result_projection(
            review_status="selected",
            decisions=[_decision("a", "selected", "missing")],
            outputs=[],
        )
    with pytest.raises(ValueError, match="rejected"):
        project_result_projection(
            review_status="selected",
            decisions=[_decision("a", "rejected", "output")],
            outputs=[_output("output", "ready")],
        )
    with pytest.raises(ValueError, match="media metadata"):
        project_result_projection(
            review_status="selected",
            decisions=[_decision("a", "selected", "output")],
            outputs=[{"output_id": "output", "status": "ready"}],
        )


def test_legacy_awaiting_review_is_excluded_from_second_batch_workload():
    runs = [
        {"run_id": "legacy", "status": "awaiting_review"},
        {"run_id": "processing", "status": "processing"},
        {"run_id": "queued", "status": "queued"},
        {"run_id": "failed", "status": "failed"},
        {"run_id": "new", "status": "completed"},
        {"run_id": "seen", "status": "completed"},
    ]
    results = [
        {"run_id": "new", "result_revision": 2, "seen_result_revision": 1},
        {"run_id": "seen", "result_revision": 1, "seen_result_revision": 1},
    ]
    workload = result_workload_counts(runs, results)
    assert workload.as_dict() == {
        "processing": 1,
        "queued": 1,
        "failed": 1,
        "completed": 2,
        "new_results": 1,
    }


def test_project_v2_status_priority_keeps_activation_separate():
    projected = project_projection_v2(
        activation_state="paused",
        runs=[{"run_id": "new", "status": "completed"}],
        results=[{"run_id": "new", "result_revision": 1, "seen_result_revision": None}],
    )
    assert projected.main_status == "new_results" and projected.activation_state == "paused"
    assert project_projection_v2(
        activation_state="active", runs=[], results=[], blocking_issue=True
    ).main_status == "blocked"
