from __future__ import annotations

import json

from live_clipper.project_migration import build_migration_v2_plan, inspect_legacy_state


def test_v2_migration_extension_is_explicit_and_does_not_scan_artifacts(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "completed",
                        "content_id": "content-completed",
                        "phase": "rendered",
                    },
                    {
                        "run_id": "legacy-review",
                        "content_id": "content-review",
                        "phase": "needs_review",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    inspected = inspect_legacy_state(service_dir)
    plan = build_migration_v2_plan(inspected)
    summary = plan.summary()

    assert summary["schema_version"] == 2
    assert summary["automatic_result_index"] is False
    assert len(plan.result_index_run_ids) == 1
    assert len(plan.compatibility_run_ids) == 1
    assert not (service_dir / "venus.sqlite3").exists()
    assert not (service_dir / "migration-backups").exists()


def test_v2_migration_plan_keeps_awaiting_review_out_of_new_result_candidates(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "old-review",
                        "content_id": "content",
                        "phase": "ready_to_render",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    plan = build_migration_v2_plan(inspect_legacy_state(service_dir))
    assert plan.result_index_run_ids == ()
    assert plan.compatibility_run_ids == (plan.foundation.runs[0]["run_id"],)
