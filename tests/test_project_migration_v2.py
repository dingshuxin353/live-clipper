from __future__ import annotations

import json

from live_clipper.project_migration import build_migration_plan, inspect_legacy_state


def test_history_classifies_importable_compatibility_quarantine_duplicates_and_safe_result(tmp_path):
    service = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    service.mkdir()
    source.mkdir()
    output.mkdir()
    runs = [
        {
            "run_id": "completed",
            "content_id": "content-completed",
            "source_path": str(source / "a.mp4"),
            "phase": "rendered",
            "created_at": "2026-08-01T00:00:00Z",
            "result_path": str(output / "a.mp4"),
            "result_sha256": "a" * 64,
        },
        {
            "run_id": "review",
            "content_id": "content-review",
            "source_path": str(source / "b.mp4"),
            "phase": "needs_review",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {
            "run_id": "processing",
            "content_id": "content-processing",
            "source_path": str(source / "c.mp4"),
            "phase": "processing",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {
            "run_id": "duplicate-1",
            "content_id": "duplicate",
            "source_path": str(source / "d.mp4"),
            "phase": "failed",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {
            "run_id": "duplicate-2",
            "content_id": "duplicate",
            "source_path": str(source / "e.mp4"),
            "phase": "failed",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {"run_id": "missing", "phase": "failed"},
        {
            "run_id": "bad-time",
            "content_id": "bad-time",
            "source_path": str(source / "f.mp4"),
            "phase": "failed",
            "created_at": "not-a-time",
        },
    ]
    (service / "runs.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")
    inspected = inspect_legacy_state(service)
    inspected = inspected.__class__(
        **{
            **inspected.__dict__,
            "source_directory": str(source),
            "output_directory": str(output),
        }
    )
    plan = build_migration_plan(
        inspected,
        choices={"source_directory": str(source), "output_directory": str(output)},
        available_bytes=10**9,
    )
    summary = plan.history_summary
    assert summary["counts"] == {
        "importable": 2,
        "compatibility": 1,
        "quarantined": 4,
        "safe_result": 1,
    }
    entries = {item["legacy_run_id"]: item for item in summary["entries"]}
    assert entries["completed"]["safe_result"]["sha256"] == "a" * 64
    assert entries["review"]["category"] == "compatibility"
    assert entries["processing"]["target_state"] == "failed"
    assert entries["processing"]["failure_code"] == "legacy_processing_interrupted"
    assert entries["duplicate-1"] == {
        "category": "quarantined",
        "legacy_run_id": "duplicate-1",
        "reason_code": "duplicate_content_identity",
    }
    assert entries["duplicate-2"]["reason_code"] == "duplicate_content_identity"
    assert entries["missing"]["reason_code"] == "content_identity_missing"
    assert entries["bad-time"]["reason_code"] == "timestamp_untrusted"


def test_result_is_not_inferred_from_unknown_or_same_named_output(tmp_path):
    service = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    service.mkdir()
    source.mkdir()
    output.mkdir()
    (output / "same-name.mp4").write_bytes(b"media-that-must-not-be-read")
    (service / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "completed",
                        "content_id": "content",
                        "source_path": str(source / "same-name.mp4"),
                        "phase": "rendered",
                        "created_at": "2026-08-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    inspected = inspect_legacy_state(service)
    inspected = inspected.__class__(
        **{**inspected.__dict__, "source_directory": str(source), "output_directory": str(output)}
    )
    before = (output / "same-name.mp4").stat().st_atime_ns
    plan = build_migration_plan(
        inspected,
        choices={"source_directory": str(source), "output_directory": str(output)},
        available_bytes=10**9,
    )
    assert "safe_result" not in plan.history_summary["entries"][0]
    assert (output / "same-name.mp4").stat().st_atime_ns == before


def test_safe_result_rejects_symlink_escape_without_reading_target(tmp_path):
    service = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    service.mkdir()
    source.mkdir()
    output.mkdir()
    outside = tmp_path / "outside-result.mp4"
    outside.write_bytes(b"outside-result-must-not-be-read")
    linked = output / "linked-result.mp4"
    linked.symlink_to(outside)
    (service / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "escaped-result",
                        "content_id": "content-escaped-result",
                        "source_path": str(source / "recording.mp4"),
                        "phase": "rendered",
                        "created_at": "2026-08-01T00:00:00Z",
                        "result_path": str(linked),
                        "result_sha256": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    inspected = inspect_legacy_state(service)
    inspected = inspected.__class__(
        **{**inspected.__dict__, "source_directory": str(source), "output_directory": str(output)}
    )
    before_atime = outside.stat().st_atime_ns

    plan = build_migration_plan(
        inspected,
        choices={"source_directory": str(source), "output_directory": str(output)},
        available_bytes=10**9,
    )

    assert "safe_result" not in plan.history_summary["entries"][0]
    assert outside.stat().st_atime_ns == before_atime
