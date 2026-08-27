from __future__ import annotations

import json

from test_project_result_api_v2 import result_api_fixture


def test_repair_context_is_issue_bound_and_never_returns_key(tmp_path):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="ai_resource_unavailable",
        category="resource",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="resource:ai",
        root_cause_ref="legacy.analysis.default",
        recovery_capability="continue_run",
    )
    status, response = api.handle(
        "GET",
        f"/api/resources/legacy.analysis.default/repair-context?issue_id={issue.issue_id}",
    )
    assert status == 200
    assert response["repair_context"]["repair_capability"] == "inline_connection"
    assert "secret-key" not in json.dumps(response)


def test_connection_request_hash_and_database_never_contain_api_key(tmp_path, monkeypatch):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="ai_resource_unavailable",
        category="resource",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="resource:ai",
        root_cause_ref="legacy.analysis.default",
        recovery_capability="continue_run",
    )
    monkeypatch.setattr("live_clipper.project_result_api.config_editor.save_llm_api_base", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("live_clipper.project_result_api.onboarding.save_llm_api_key", lambda *_args, **_kwargs: {"ok": True})
    status, response = api.handle("PATCH", "/api/resources/legacy.analysis.default/connection", body={
        "request_id": "connection-1",
        "issue_id": issue.issue_id,
        "api_base": "https://example.com/v1",
        "api_key": "sk-never-persist-this",
    })
    assert status == 200 and "sk-never" not in json.dumps(response)
    database_dump = "\n".join(repository.connection.iterdump())
    assert "sk-never-persist-this" not in database_dump
    assert api.repository.get_idempotency_key(
        f"resource.connection:legacy.analysis.default:{issue.issue_id}", "connection-1"
    ) is not None


def test_failed_connection_test_is_stably_idempotent(tmp_path, monkeypatch):
    repository, api, project, run, _path, _media = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="ai_resource_unavailable",
        category="resource",
        scope_type="run",
        project_id=project.project_id,
        run_id=run.run_id,
        issue_group_key="resource:ai",
        root_cause_ref="legacy.analysis.default",
        recovery_capability="continue_run",
    )
    calls = []
    monkeypatch.setattr(
        "live_clipper.project_result_api.onboarding.test_llm",
        lambda *_args, **_kwargs: calls.append(True) or {"ok": False, "error_code": "llm_auth_failed"},
    )
    payload = {"request_id": "test-1", "issue_id": issue.issue_id}
    first_status, first = api.handle("POST", "/api/resources/legacy.analysis.default/connection-test", body=payload)
    second_status, second = api.handle("POST", "/api/resources/legacy.analysis.default/connection-test", body=payload)
    assert (first_status, second_status) == (409, 409)
    assert first["error"]["code"] == second["error"]["code"] == "connection_test_failed"
    assert len(calls) == 1
