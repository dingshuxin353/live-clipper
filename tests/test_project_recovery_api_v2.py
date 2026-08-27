from __future__ import annotations

from test_project_result_api_v2 import result_api_fixture


def test_continue_is_blocked_until_recheck_then_requeues_same_run(tmp_path):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="output_unwritable",
        category="storage",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="storage:output",
        recovery_capability="continue_run",
        safe_checkpoint="validated_review",
        reuse_stages=["review"],
        redo_stages=["render"],
    )
    status, blocked = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
        "request_id": "continue-before-check",
        "expected_issue_revision": issue.issue_revision,
    })
    assert status == 409 and blocked["error"]["code"] == "issue_not_ready"

    status, checked = api.handle("POST", f"/api/issues/{issue.issue_id}/recheck", body={
        "request_id": "recheck-1",
        "expected_issue_revision": issue.issue_revision,
    })
    assert status == 200 and checked["issue"]["status"] == "ready_to_recover"
    revision = checked["issue"]["issue_revision"]
    status, continued = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
        "request_id": "continue-1",
        "expected_issue_revision": revision,
    })
    assert status == 200 and continued["run_id"] == run.run_id
    assert repository.get_run(run.run_id).status == "queued"


def test_group_recheck_only_checks_and_never_continues(tmp_path):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="output_unwritable",
        category="storage",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="storage:shared",
        recovery_capability="continue_run",
    )
    status, response = api.handle("POST", "/api/issue-groups/storage%3Ashared/recheck", body={
        "request_id": "group-1",
        "issue_revisions": {issue.issue_id: issue.issue_revision},
    })
    assert status == 200 and response["issues"][0]["status"] == "ready_to_recover"
    assert repository.get_run(run.run_id).status != "queued"
