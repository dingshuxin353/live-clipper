from __future__ import annotations

import pytest
from test_project_result_api_v2 import result_api_fixture

from live_clipper.project_file_grants import FileSelectionGrantError, FileSelectionGrantStore
from live_clipper.project_result_api import ProjectResultAPI


def test_file_selection_grant_is_bound_single_use_and_expires(tmp_path):
    now = [10.0]
    store = FileSelectionGrantStore(ttl_seconds=300, clock=lambda: now[0])
    selected = tmp_path / "selected.mp4"
    token = store.issue(issue_id="issue-1", kind="source", selected_path=selected)
    assert store.consume(token, issue_id="issue-1", kind="source") == selected.resolve()
    with pytest.raises(FileSelectionGrantError) as reused:
        store.consume(token, issue_id="issue-1", kind="source")
    assert reused.value.code == "selection_token_already_used"

    expired = store.issue(issue_id="issue-2", kind="recovery_output", selected_path=tmp_path)
    now[0] = 311.0
    with pytest.raises(FileSelectionGrantError) as error:
        store.consume(expired, issue_id="issue-2", kind="recovery_output")
    assert error.value.code == "selection_token_expired"


def test_desktop_routes_reject_browser_auth(tmp_path):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="source_missing",
        category="source",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="source:missing",
        recovery_capability="continue_run",
    )
    status, response = api.handle("POST", "/api/desktop/file-selections", body={
        "issue_id": issue.issue_id,
        "kind": "source",
        "selected_path": str(tmp_path / "source.mp4"),
    })
    assert status == 403 and response["error"]["code"] == "desktop_auth_required"


def test_desktop_grant_hides_path_and_formal_source_action_consumes_it_once(tmp_path):
    repository, browser_api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="source_missing",
        category="source",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="source:missing",
        recovery_capability="continue_run",
    )
    grants = FileSelectionGrantStore()
    desktop_api = ProjectResultAPI(
        repository,
        browser_api.settings,
        service_dir=tmp_path / "service",
        grants=grants,
        auth_context="bearer",
    )
    status, granted = desktop_api.handle("POST", "/api/desktop/file-selections", body={
        "issue_id": issue.issue_id,
        "kind": "source",
        "selected_path": run.latest_seen_path,
    })
    assert status == 201 and run.latest_seen_path not in str(granted)
    status, checked = desktop_api.handle("POST", f"/api/issues/{issue.issue_id}/source", body={
        "request_id": "source-select-1",
        "expected_issue_revision": issue.issue_revision,
        "selection_token": granted["selection_token"],
    })
    assert status == 200 and checked["issue"]["status"] == "ready_to_recover"
    status, reused = desktop_api.handle("POST", f"/api/issues/{issue.issue_id}/source", body={
        "request_id": "source-select-2",
        "expected_issue_revision": checked["issue"]["issue_revision"],
        "selection_token": granted["selection_token"],
    })
    assert status == 409 and reused["error"]["code"] == "selection_token_already_used"
