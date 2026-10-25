from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Thread
from urllib.request import Request, urlopen

import pytest
from test_project_result_api_v2 import result_api_fixture

from live_clipper.project_result_api import ProjectResultAPI
from live_clipper.project_storage import ProjectRepository
from live_clipper.web import LiveClipperRequestHandler, WebPaths


def recovery_fixture(tmp_path, action="continue"):
    repository, api, project, run, *_ = result_api_fixture(tmp_path)
    repository.transition_run(run.run_id, status="failed", stage="render", event_type="failed",
                              error_code="render_failed", error_summary="render failed")
    target = {}
    if action == "retry-output":
        repository.update_output_and_reproject_result("output-1", status="missing", error_code="output_missing")
        target = {"output_id": "output-1"}
    elif action == "retry-material":
        target = {"output_id": "output-1", "material_id": "material-1"}
    issue = repository.discover_issue(
        issue_code="output_unwritable", category="storage",
        scope_type={"continue": "run", "retry-output": "output", "retry-material": "material"}[action],
        project_id=project.project_id, run_id=run.run_id, issue_group_key="recovery",
        recovery_capability={"continue": "continue_run", "retry-output": "retry_output", "retry-material": "retry_material"}[action],
        status="ready_to_recover", reuse_stages=("review",), redo_stages=("render",), **target,
    )
    return repository, api, run, issue


def concurrent_requests(repository, api, issue, action, request_ids):
    barrier = Barrier(2)
    errors = []

    def submit(request_id):
        with ProjectRepository(repository.service_dir) as connection:
            reached = False

            def before_write(statement):
                nonlocal reached
                if statement == "BEGIN IMMEDIATE" and not reached:
                    reached = True
                    try:
                        barrier.wait(timeout=5)
                    except Exception as exc:
                        errors.append(exc)

            connection.connection.set_trace_callback(before_write)
            client = ProjectResultAPI(connection, api.settings, service_dir=api.service_dir, config_path=api.config_path)
            response = client.handle("POST", f"/api/issues/{issue.issue_id}/{action}", body={
                "request_id": request_id, "expected_issue_revision": issue.issue_revision,
            })
            assert reached
            return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, request_ids))
    assert not errors
    return responses


@pytest.mark.parametrize("action", ["continue", "retry-output", "retry-material"])
@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_recovery_requests_are_accepted_once(tmp_path, action, same_request):
    repository, api, run, issue = recovery_fixture(tmp_path, action)
    responses = concurrent_requests(repository, api, issue, action, ["first", "first" if same_request else "second"])
    if same_request:
        assert all(status == 200 for status, _ in responses)
        assert responses[0][1] == responses[1][1]
    else:
        assert sorted(status for status, _ in responses) == [200, 409]
        failure = next(body for status, body in responses if status == 409)
        assert failure["error"]["code"] == "revision_conflict"
    assert len(repository.list_recovery_attempts(issue.issue_id)) == 1
    current = repository.get_issue(issue.issue_id)
    assert current.total_attempt_count == 1 and current.issue_revision == issue.issue_revision + 1
    assert repository.get_run(run.run_id).status == ("queued" if action == "continue" else "failed")
    assert [e.event_type for e in repository.list_issue_events(issue.issue_id)].count("recovery_accepted") == 1
    assert [e.event_type for e in repository.list_workspace_events()].count("recovery_accepted") == 1


def test_recovery_request_cannot_change_action(tmp_path):
    repository, api, _run, issue = recovery_fixture(tmp_path)
    body = {"request_id": "same", "expected_issue_revision": issue.issue_revision}
    assert api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body=body)[0] == 200
    current = repository.get_issue(issue.issue_id)
    status, result = api.handle("POST", f"/api/issues/{issue.issue_id}/retry-output", body={
        **body, "expected_issue_revision": current.issue_revision,
    })
    assert status == 409 and result["error"]["code"] == "request_id_conflict"
    assert repository.get_issue(issue.issue_id) == current
    assert len(repository.list_recovery_attempts(issue.issue_id)) == 1


def test_unsupported_continue_keeps_existing_error_contract(tmp_path):
    repository, api, project, *_ = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="invalid_target", category="process", scope_type="project", project_id=project.project_id,
        issue_group_key="project", recovery_capability="continue_run", status="ready_to_recover",
    )
    status, response = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
        "request_id": "invalid", "expected_issue_revision": issue.issue_revision,
    })
    assert status == 409 and response["error"]["code"] == "issue_not_ready"
    assert repository.get_issue(issue.issue_id) == issue and not repository.list_recovery_attempts(issue.issue_id)


def test_recovery_without_redo_stages_queues_source_read(tmp_path):
    repository, api, run, _issue = recovery_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="processing_interrupted", category="process", scope_type="run", project_id=run.project_id,
        run_id=run.run_id, issue_group_key="empty-redo", recovery_capability="continue_run", status="ready_to_recover",
    )
    status, accepted = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
        "request_id": "read-source", "expected_issue_revision": issue.issue_revision,
    })
    assert status == 200 and accepted["rerun_stages"] == []
    assert repository.get_run(run.run_id).current_stage == "read_source"
    assert repository.list_stage_events(run.run_id)[-1].stage == "read_source"


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


def recovery_state(repository, run, issue):
    outputs = repository.list_run_outputs(run.run_id)
    return (
        repository.get_issue(issue.issue_id), repository.get_run(run.run_id), outputs,
        [repository.get_output_material(output.output_id) for output in outputs],
        repository.list_recovery_attempts(issue.issue_id), repository.list_issue_events(issue.issue_id),
        repository.list_stage_events(run.run_id), repository.list_workspace_events(),
    )


@pytest.mark.parametrize("action", ["continue", "retry-output", "retry-material"])
def test_recovery_survives_reopening_and_preserves_target_semantics(tmp_path, action):
    repository, api, run, issue = recovery_fixture(tmp_path, action)
    edited = repository.update_output_material(
        "output-1", expected_material_revision=1,
        title_candidates=[{"title_id": "edited", "text": "我的标题"}], preferred_title_id="edited",
        description="我的描述", tags=["保留"],
    )
    before_run = repository.get_run(run.run_id)
    before_output = repository.get_run_output("output-1")
    stages = repository.list_stage_events(run.run_id)
    body = {"request_id": "reopen", "expected_issue_revision": issue.issue_revision}
    path = f"/api/issues/{issue.issue_id}/{action}"
    status, accepted = api.handle("POST", path, body=body)
    assert status == 200
    attempt = repository.get_recovery_attempt(accepted["recovery_attempt_id"])
    assert attempt.status == "accepted" and attempt.requested_by == "web"
    assert attempt.reuse_stages == ["review"] and attempt.redo_stages == ["render"]
    after_run = repository.get_run(run.run_id)
    after_output = repository.get_run_output("output-1")
    after_material = repository.get_output_material("output-1")
    if action == "continue":
        assert before_run.completed_at and before_run.queued_at != after_run.queued_at
        assert after_run == replace(before_run, status="queued", current_stage="render", completed_at=None,
                                    queued_at=attempt.accepted_at, updated_at=attempt.accepted_at,
                                    error_code=None, error_summary=None)
        events = repository.list_stage_events(run.run_id)
        assert events[:-1] == stages and events[-1].event_type == "recovery_queued"
        assert events[-1].stage == "render" and events[-1].detail == {"attempt_id": attempt.attempt_id}
    else:
        assert after_run == before_run and repository.list_stage_events(run.run_id) == stages
    if action == "retry-output":
        assert after_output == replace(before_output, status="pending", error_code=None, error_summary=None,
                                       updated_at=attempt.accepted_at)
    else:
        assert after_output == before_output
    assert after_material == (replace(edited, status="pending", updated_at=attempt.accepted_at)
                              if action == "retry-material" else edited)
    state = recovery_state(repository, run, issue)
    repository.close()
    with ProjectRepository(api.service_dir) as reopened:
        client = ProjectResultAPI(reopened, api.settings, service_dir=api.service_dir, config_path=api.config_path)
        assert client.handle("POST", path, body=body) == (status, accepted)
        assert recovery_state(reopened, run, issue) == state
        changed = client.handle("POST", path, body={**body, "expected_issue_revision": issue.issue_revision + 1})
        assert changed[0] == 409 and changed[1]["error"]["code"] == "request_id_conflict"
        assert recovery_state(reopened, run, issue) == state


@pytest.mark.parametrize("action,table", [
    (action, table) for action in ("continue", "retry-output", "retry-material")
    for table in ("issue_events", "workspace_events", "idempotency_keys")
] + [("continue", "run_stage_events")])
def test_recovery_write_failure_rolls_back_and_original_request_can_retry(tmp_path, action, table):
    repository, api, run, issue = recovery_fixture(tmp_path, action)
    before = recovery_state(repository, run, issue)
    body = {"request_id": "retry-after-failure", "expected_issue_revision": issue.issue_revision}
    path = f"/api/issues/{issue.issue_id}/{action}"
    repository.connection.execute(f"""CREATE TEMP TRIGGER fail_recovery BEFORE INSERT ON {table}
        BEGIN SELECT RAISE(ABORT, '/private/fixture-secret'); END""")
    status, response = api.handle("POST", path, body=body)
    assert status == 500 and response["error"]["code"] == "internal_error"
    assert "fixture-secret" not in str(response)
    assert recovery_state(repository, run, issue) == before
    assert repository.get_idempotency_key(f"issue.{action}:{issue.issue_id}", body["request_id"]) is None
    repository.connection.execute("DROP TRIGGER fail_recovery")
    status, accepted = api.handle("POST", path, body=body)
    assert status == 200
    assert api.handle("POST", path, body=body) == (status, accepted)
    assert len(repository.list_recovery_attempts(issue.issue_id)) == 1


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_active_run_reuse_records_requests_without_business_changes(tmp_path, same_request):
    repository, api, run, issue = recovery_fixture(tmp_path)
    active, reason = repository.create_reprocess_run(
        run.run_id, request_id="active-run", request_hash="active-hash", config_revision=run.config_revision,
        parameter_snapshot=run.parameter_snapshot, source_path=run.latest_seen_path,
    )
    assert reason == "created"
    before = recovery_state(repository, run, issue)
    request_ids = ["first", "first" if same_request else "second"]
    responses = concurrent_requests(repository, api, issue, "continue", request_ids)
    expected = {"ok": True, "run_id": active.run_id, "reused": True, "reuse_reason": "active_run"}
    assert responses == [(200, expected), (200, expected)]
    assert recovery_state(repository, run, issue) == before
    repository.close()
    with ProjectRepository(api.service_dir) as reopened:
        client = ProjectResultAPI(reopened, api.settings, service_dir=api.service_dir, config_path=api.config_path)
        for request_id in set(request_ids):
            saved = reopened.get_idempotency_key(f"issue.continue:{issue.issue_id}", request_id)
            assert saved["object_type"] == "run" and saved["object_id"] == active.run_id
            assert client.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
                "request_id": request_id, "expected_issue_revision": issue.issue_revision,
            }) == (200, expected)
        assert recovery_state(reopened, run, issue) == before


def test_real_http_recheck_continue_and_replay(tmp_path):
    repository, _api, project, run, *_ = result_api_fixture(tmp_path)
    repository.transition_run(run.run_id, status="failed", stage="render", event_type="failed")
    issue = repository.discover_issue(
        issue_code="output_unwritable", category="storage", scope_type="run", project_id=project.project_id,
        run_id=run.run_id, issue_group_key="http", recovery_capability="continue_run", redo_stages=("render",),
    )
    config = tmp_path / "live-clipper.toml"
    config.write_text(f'[paths]\nwork_dir = "{tmp_path / "work"}"\noutput_root = "{tmp_path / "output"}"\n')

    class Handler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=repository.service_dir, config_path=config)
        access_token = "isolated-test-token"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(action, body):
        request = Request(f"http://127.0.0.1:{server.server_port}/api/issues/{issue.issue_id}/{action}",
                          data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer isolated-test-token"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            return json.loads(response.read())

    try:
        checked = post("recheck", {"request_id": "check", "expected_issue_revision": issue.issue_revision})
        assert checked["issue"]["status"] == "ready_to_recover"
        assert repository.get_run(run.run_id).status == "failed"
        body = {"request_id": "continue", "expected_issue_revision": checked["issue"]["issue_revision"]}
        accepted = post("continue", body)
        assert post("continue", body) == accepted
        assert repository.get_run(run.run_id).status == "queued"
        assert len(repository.list_recovery_attempts(issue.issue_id)) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        repository.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("capability", ["continue_run", "operational_repair"])
def test_source_recheck_persists_atomically_and_keeps_authorization(tmp_path, capability):
    from live_clipper.project_file_grants import FileSelectionGrantStore

    repository, browser, project, run, *_ = result_api_fixture(tmp_path)
    issue = repository.discover_issue(
        issue_code="source_missing", category="source", scope_type="run", project_id=project.project_id,
        run_id=run.run_id, issue_group_key="source", recovery_capability=capability,
    )
    source = tmp_path / "replacement.mp4"
    source.write_bytes(Path(run.latest_seen_path).read_bytes())
    api = ProjectResultAPI(repository, browser.settings, service_dir=browser.service_dir,
                           grants=FileSelectionGrantStore(), auth_context="bearer")
    before = recovery_state(repository, run, issue)

    def selection():
        status, response = api.handle("POST", "/api/desktop/file-selections", body={
            "issue_id": issue.issue_id, "kind": "source", "selected_path": str(source),
        })
        assert status == 201
        return response["selection_token"]

    event = "source_repaired" if capability == "operational_repair" else "recheck_succeeded"
    repository.connection.execute(f"""CREATE TEMP TRIGGER fail_source BEFORE INSERT ON issue_events
        WHEN NEW.event_type = '{event}' BEGIN SELECT RAISE(ABORT, 'test source save failure'); END""")
    body = {"request_id": "source", "expected_issue_revision": issue.issue_revision, "selection_token": selection()}
    route = f"/api/issues/{issue.issue_id}/source"
    assert api.handle("POST", route, body=body)[0] == 500
    assert recovery_state(repository, run, issue) == before
    assert repository.get_idempotency_key(f"issue.source:{issue.issue_id}", "source") is None
    repository.connection.execute("DROP TRIGGER fail_source")
    # Selection grants remain single use even when the database transaction rolls back.
    reused = api.handle("POST", route, body=body)
    assert reused[0] == 409 and reused[1]["error"]["code"] == "selection_token_already_used"
    body["selection_token"] = selection()
    status, saved = api.handle("POST", route, body=body)
    assert status == 200
    current = repository.get_issue(issue.issue_id)
    assert current.status == ("resolved" if capability == "operational_repair" else "ready_to_recover")
    assert current.issue_revision == issue.issue_revision + 2
    assert repository.list_issue_events(issue.issue_id)[-1].event_type == event
    if capability == "operational_repair":
        assert repository.get_run(run.run_id).latest_seen_path == str(source)
        assert not current.operational_overrides
    else:
        assert repository.get_run(run.run_id) == before[1]
        assert current.operational_overrides == {"source_path": str(source)}
    assert not repository.list_recovery_attempts(issue.issue_id)
    replay = api.handle("POST", route, body=body)
    assert replay[0] == 200 and replay[1]["reused"] and replay[1]["issue"] == saved["issue"]


@pytest.mark.parametrize("reason", ["credential", "review"])
def test_http_recovery_refuses_unavailable_original_resources_and_review_reprocessing(tmp_path, reason):
    repository, api, run, issue = recovery_fixture(tmp_path)
    if reason == "credential":
        binding = run.parameter_snapshot["resources"]["analysis"]["binding_ref"]
        (repository.service_dir / "resource-credentials" / f"{binding}.env").unlink()
    else:
        issue = repository.discover_issue(
            issue_code="ai_review_invalid", category="ai", scope_type="run",
            project_id=run.project_id, run_id=run.run_id, issue_group_key="review",
            recovery_capability="continue_run", status="ready_to_recover", redo_stages=("review",),
        )
    before = recovery_state(repository, run, issue)
    status, response = api.handle("POST", f"/api/issues/{issue.issue_id}/continue", body={
        "request_id": "refused", "expected_issue_revision": issue.issue_revision,
    })
    assert status == 409 and response["error"]["code"] == "issue_not_ready"
    assert recovery_state(repository, run, issue) == before
    assert str(tmp_path) not in str(response)
