from live_clipper.config import Settings
from live_clipper.project_api import ProjectAPI
from live_clipper.project_domain import default_project_config
from live_clipper.project_scan import ProjectScanError


def test_api_create_list_detail_and_stable_errors(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    api = ProjectAPI(tmp_path / "service", Settings(cheap_model_api_key="fake"))
    config = default_project_config(source, output)
    status, payload = api.handle(
        "POST",
        "/api/projects",
        body={
            "request_id": "create-1",
            "activation_state": "active",
            "project": {"name": "项目", "description": "", "config": config},
        },
    )
    assert status == 201 and payload["ok"]
    project_id = payload["project"]["project_id"]
    status, listed = api.handle("GET", "/api/projects")
    assert status == 200 and listed["projects"][0]["project_id"] == project_id
    status, detail = api.handle("GET", f"/api/projects/{project_id}")
    assert status == 200 and detail["project"]["main_status"] == "idle"
    status, missing = api.handle("GET", "/api/projects/not-found")
    assert status == 404
    assert missing == {
        "ok": False,
        "error": {"code": "project_not_found", "message": "项目不存在", "fields": {}},
    }


def test_api_rejects_unknown_fields_and_legacy_write_mode(tmp_path):
    api = ProjectAPI(tmp_path / "service", Settings(cheap_model_api_key="fake"))
    status, payload = api.handle("POST", "/api/projects", body={"unknown": True})
    assert status == 422 and payload["error"]["code"] == "validation_failed"
    api.repository.set_data_mode("legacy")
    status, payload = api.handle("POST", "/api/projects", body={})
    assert status == 409 and payload["error"]["code"] == "migration_required"


def test_api_preview_revision_conflict_cursor_and_seen_anchor(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "existing.mp4").write_bytes(b"video")
    api = ProjectAPI(tmp_path / "service", Settings(cheap_model_api_key="fake"))
    status, preview = api.handle(
        "POST",
        "/api/projects/scan-preview",
        body={"source_directory": str(source), "first_scan_mode": "choose_existing"},
    )
    assert status == 200 and preview["processable_files"] == 1
    config = default_project_config(source, output)
    status, created = api.handle(
        "POST",
        "/api/projects",
        body={
            "request_id": "create",
            "activation_state": "active",
            "project": {"name": "项目", "description": "", "config": config},
        },
    )
    project_id = created["project"]["project_id"]
    changed_config = default_project_config(source, output)
    changed_config["output"]["intermediate_retention"] = "keep"
    status, updated = api.handle(
        "PATCH",
        f"/api/projects/{project_id}",
        body={
            "request_id": "update-1",
            "expected_revision": 1,
            "project": {"name": "项目", "description": "新版", "config": changed_config},
        },
    )
    assert status == 200 and updated["project"]["current_config_revision"] == 2
    status, conflict = api.handle(
        "PATCH",
        f"/api/projects/{project_id}",
        body={
            "request_id": "update-stale",
            "expected_revision": 1,
            "project": {"name": "项目", "description": "冲突", "config": changed_config},
        },
    )
    assert status == 409 and conflict["error"]["fields"]["current_revision"] == "2"

    for index in range(2):
        api.repository.create_normal_run(
            project_id=project_id,
            content_id=f"content-{index}",
            trigger_source="manual",
            first_seen_path=str(source / f"{index}.mp4"),
            latest_seen_path=str(source / f"{index}.mp4"),
            parameter_snapshot={},
            queued_at=f"2026-08-20T00:00:0{index}Z",
        )
    status, first_page = api.handle("GET", f"/api/projects/{project_id}/runs?limit=1")
    status, second_page = api.handle(
        "GET", f"/api/projects/{project_id}/runs?limit=1&cursor={first_page['cursor']}"
    )
    assert status == 200 and first_page["has_more"] and len(second_page["runs"]) == 1

    status, studio = api.handle("GET", "/api/studio")
    through = studio["through_event_id"]
    status, seen = api.handle("POST", "/api/studio/seen", body={"through_event_id": through})
    assert status == 200 and seen["last_seen_event_id"] == through
    status, backward = api.handle("POST", "/api/studio/seen", body={"through_event_id": through - 1})
    assert status == 422 and backward["error"]["code"] == "validation_failed"


def test_recent_project_creation_uses_legal_initial_scan_trigger(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    config = default_project_config(source, output)
    config["source"].update(first_scan_mode="recent", lookback_days=3)
    api = ProjectAPI(tmp_path / "service", Settings(cheap_model_api_key="fake"))

    status, payload = api.handle(
        "POST",
        "/api/projects",
        body={
            "request_id": "recent-create",
            "activation_state": "active",
            "project": {"name": "recent", "description": "", "config": config},
        },
    )

    assert status == 201 and payload["initial_scan"]["status"] == "success"
    scan = api.repository.list_scan_events(payload["project"]["project_id"])[0]
    assert scan.trigger_source == "manual"
    assert api.repository.get_runtime(payload["project"]["project_id"]).first_scan_state == "completed"


def test_initial_scan_failure_returns_coherent_created_project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    config = default_project_config(source, output)
    config["source"].update(first_scan_mode="recent", lookback_days=3)
    api = ProjectAPI(tmp_path / "service", Settings(cheap_model_api_key="fake"))
    monkeypatch.setattr(
        "live_clipper.project_api.scan_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProjectScanError("source_unavailable", "录像目录不可用")),
    )

    status, payload = api.handle(
        "POST",
        "/api/projects",
        body={
            "request_id": "recent-failure",
            "activation_state": "active",
            "project": {"name": "recent", "description": "", "config": config},
        },
    )

    project_id = payload["project"]["project_id"]
    runtime = api.repository.get_runtime(project_id)
    assert status == 201 and payload["initial_scan"]["error"]["code"] == "source_unavailable"
    assert len(api.repository.list_projects()) == 1
    assert runtime.first_scan_state == "pending"
    assert runtime.readiness_state == "blocked" and runtime.failure_code == "source_unavailable"
    assert api.repository.list_workspace_events()[-1].event_type == "scan_failed"
