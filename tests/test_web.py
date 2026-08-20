from __future__ import annotations

import re
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

from live_clipper import web
from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import ProjectManager, open_project_repository
from live_clipper.utils import write_json
from live_clipper.web import LiveClipperRequestHandler, WebPaths, build_run_detail, build_runs_index, handle_api_request


def test_build_runs_index_merges_files_state_and_log(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    run_dir = output_root / "recording"
    log_path = log_dir / "recording.log"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", {"candidates": [{"id": "c1"}]})
    write_json(run_dir / "codex_brief.json", {"candidates": [{"id": "c1"}]})
    write_json(state_dir / "recording.json", {"pid": 4321, "log_path": str(log_path)})
    log_path.parent.mkdir(parents=True)
    log_path.write_text("first\nsecond\n", encoding="utf-8")
    monkeypatch.setattr("live_clipper.web._pid_is_running", lambda pid: True)

    index = build_runs_index(WebPaths(output_root=output_root, state_dir=state_dir, log_dir=log_dir, service_dir=tmp_path / "service"))

    assert index["ok"] is True
    assert index["runs"][0]["run_id"] == "recording"
    assert index["runs"][0]["running"] is True
    assert index["runs"][0]["requires_codex"] is True
    assert index["runs"][0]["candidate_count"] == 1
    assert index["runs"][0]["selected_count"] == 0
    assert index["runs"][0]["clip_count"] == 0
    assert index["runs"][0]["log_path"] == str(log_path)
    assert index["runs"][0]["phase"] == "needs_codex_selection"


def test_build_run_detail_includes_steps_files_actions_and_log_tail(tmp_path):
    output_root = tmp_path / "output"
    log_dir = tmp_path / "logs"
    run_dir = output_root / "recording"
    log_path = log_dir / "recording.log"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", {"candidates": [{"id": "c1"}, {"id": "c2"}]})
    write_json(run_dir / "codex_brief.json", {"candidates": [{"id": "c1"}, {"id": "c2"}]})
    log_path.parent.mkdir(parents=True)
    log_path.write_text("\n".join(f"line {index}" for index in range(5)), encoding="utf-8")

    detail = build_run_detail(
        "recording",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=log_dir),
        log_lines=2,
    )

    assert detail["ok"] is True
    assert detail["run"]["run_id"] == "recording"
    assert detail["steps"][3]["label"] == "Agnes 扫描"
    assert detail["steps"][3]["done"] is True
    assert detail["steps"][5]["label"] == "Codex 选择"
    assert detail["steps"][5]["state"] == "waiting"
    assert detail["actions"]["can_render"] is False
    assert detail["actions"]["can_cleanup"] is False
    assert detail["log"]["tail"] == "line 3\nline 4"


def test_build_run_detail_lists_generated_clips_and_cleanup_targets(tmp_path):
    output_root = tmp_path / "output"
    input_dir = tmp_path / "input"
    run_dir = output_root / "recording"
    local_source = input_dir / "recording.mkv"
    original_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    original_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    original_source.write_bytes(b"nas video")
    (run_dir / "audio.wav").parent.mkdir(parents=True)
    (run_dir / "audio.wav").write_bytes(b"audio")
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "clip_01.mp4").write_bytes(b"clip one")
    write_json(run_dir / "run_metadata.json", {
        "source_name": "recording.mkv",
        "pipeline": {
            "local_source_path": str(local_source),
            "original_source_path": str(original_source),
        },
    })
    write_json(run_dir / "selected_clips.json", {"candidates": [{"id": "clip_01"}]})

    detail = build_run_detail(
        "recording",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs", input_dir=input_dir),
    )

    assert detail["clips"][0]["name"] == "clip_01.mp4"
    assert detail["clips"][0]["url"] == "/media/runs/recording/clips/clip_01.mp4"
    local_source_target = next(target for target in detail["cleanup"]["targets"] if target["kind"] == "local_source_video")
    assert local_source_target["deletable"] is True
    assert detail["actions"]["can_delete_local_source"] is True


def test_service_run_media_and_cleanup_use_saved_run_paths(tmp_path):
    service_dir = tmp_path / "service"
    workspace_dir = tmp_path / "workspace" / "runs" / "business-run"
    input_dir = workspace_dir / "input"
    run_dir = workspace_dir / "output"
    local_source = input_dir / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True)
    clip = clips_dir / "clip_01.mp4"
    clip.write_bytes(b"clip one")
    write_json(
        run_dir / "run_metadata.json",
        {
            "pipeline": {
                "local_source_path": str(local_source),
                "original_source_path": str(tmp_path / "nas" / "recording.mkv"),
            }
        },
    )
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                    {
                        "run_id": "business-run",
                        "run_dir": str(run_dir),
                        "input_dir": str(input_dir),
                        "local_source_path": str(local_source),
                        "log_path": str(service_dir / "runs" / "business-run.log"),
                        "phase": "rendered",
                    }
            ]
        },
    )
    paths = WebPaths(
        output_root=tmp_path / "legacy-output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "legacy-input",
        service_dir=service_dir,
        config_path=tmp_path / "missing.toml",
    )

    detail = build_run_detail("business-run", paths)

    assert detail["clips"][0]["url"] == "/media/runs/business-run/clips/clip_01.mp4"
    assert next(
        target for target in detail["cleanup"]["targets"] if target["kind"] == "local_source_video"
    )["path"] == str(local_source)
    assert web._media_clip_path("/media/runs/business-run/clips/clip_01.mp4", paths) == clip
    assert web._media_clip_path("/media/runs/business-run/clips/../secret.mp4", paths) is None


def test_handle_api_request_returns_json_payloads(tmp_path):
    output_root = tmp_path / "output"
    run_dir = output_root / "recording"
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})

    status_code, headers, body = handle_api_request(
        "GET",
        "/api/runs",
        WebPaths(
            output_root=output_root,
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "logs",
            service_dir=tmp_path / "service",
        ),
    )

    assert status_code == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert body["ok"] is True
    assert body["runs"][0]["run_id"] == "recording"


def test_runs_api_filters_full_snapshot_before_pagination_and_reports_counts(tmp_path):
    service_dir = tmp_path / "service"
    historical = [
        {
            "run_id": f"rendered-{index}",
            "phase": "rendered",
            "run_dir": str(tmp_path / "output" / f"rendered-{index}"),
            "updated_at": f"2026-08-{index + 1:02d}T00:00:00+00:00",
        }
        for index in range(20)
    ]
    queued = [
        {
            "run_id": f"queued-{index}",
            "phase": "queued",
            "run_dir": str(tmp_path / "output" / f"queued-{index}"),
        }
        for index in range(57)
    ]
    queued.insert(
        20,
        {
            "run_id": "processing-hidden-after-first-page",
            "phase": "processing",
            "run_dir": str(tmp_path / "output" / "processing-hidden-after-first-page"),
        },
    )
    queued.insert(
        21,
        {
            "run_id": "failed-hidden-after-first-page",
            "phase": "failed",
            "run_dir": str(tmp_path / "output" / "failed-hidden-after-first-page"),
        },
    )
    write_json(service_dir / "runs.json", {"runs": historical + queued})
    paths = WebPaths(service_dir=service_dir, output_root=tmp_path / "output")

    first_status, _headers, first = handle_api_request(
        "GET", "/api/runs?phase=queued&offset=0&limit=20", paths
    )
    last_status, _headers, last = handle_api_request(
        "GET", "/api/runs?phase=queued&offset=40&limit=20", paths
    )
    processing_status, _headers, processing = handle_api_request(
        "GET", "/api/runs?phase=processing", paths
    )

    assert first_status == last_status == processing_status == 200
    assert first["total"] == 57
    assert first["count"] == 20
    assert first["has_more"] is True
    assert first["phase_counts"] == {
        "all": 79,
        "queued": 57,
        "processing": 1,
        "needs_review": 0,
        "rendered": 20,
        "failed": 1,
        "other": 0,
    }
    assert [run["queue_position"] for run in first["runs"]] == list(range(1, 21))
    assert [run["queue_position"] for run in last["runs"]] == list(range(41, 58))
    assert last["count"] == 17
    assert last["has_more"] is False
    assert processing["total"] == 1
    assert processing["runs"][0]["run_id"] == "processing-hidden-after-first-page"


def test_runs_api_uses_one_phase_group_contract_for_legacy_phases(tmp_path):
    service_dir = tmp_path / "service"
    phases = [
        "queued",
        "processing",
        "rendering",
        "running",
        "ready_to_render",
        "needs_review",
        "needs_codex_selection",
        "rendered",
        "cleanup_ready",
        "failed",
        "failed_needs_codex",
        "waiting_or_manual",
        "missing",
        "unknown",
        "future_phase",
    ]
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {"run_id": phase, "phase": phase, "run_dir": str(tmp_path / "output" / phase)}
                for phase in phases
            ]
        },
    )
    paths = WebPaths(service_dir=service_dir)

    status, _headers, all_runs = handle_api_request("GET", "/api/runs?limit=100", paths)
    review_status, _headers, review = handle_api_request("GET", "/api/runs?phase=needs_review", paths)
    failed_status, _headers, failed = handle_api_request("GET", "/api/runs?phase=failed", paths)

    assert status == review_status == failed_status == 200
    assert all_runs["phase_counts"] == {
        "all": 15,
        "queued": 1,
        "processing": 4,
        "needs_review": 2,
        "rendered": 2,
        "failed": 2,
        "other": 4,
    }
    assert {run["phase"] for run in review["runs"]} == {"needs_review", "needs_codex_selection"}
    assert {run["phase"] for run in failed["runs"]} == {"failed", "failed_needs_codex"}
    assert {run["run_id"] for run in all_runs["runs"]} == set(phases)


def test_runs_api_rejects_invalid_query_parameters_with_stable_error(tmp_path):
    paths = WebPaths(service_dir=tmp_path / "service")

    for query in [
        "phase=not-a-phase",
        "offset=-1",
        "offset=abc",
        "limit=0",
        "limit=101",
        "limit=1.5",
    ]:
        status, _headers, body = handle_api_request("GET", f"/api/runs?{query}", paths)

        assert status == 400
        assert body["ok"] is False
        assert body["error_code"] == "invalid_query_parameter"


def test_runs_api_applies_the_same_contract_to_legacy_output_fallback(tmp_path):
    output_root = tmp_path / "output"
    for index in range(25):
        run_dir = output_root / f"legacy-{index}"
        write_json(run_dir / "run_metadata.json", {"source_name": f"legacy-{index}.mkv"})
        write_json(run_dir / "codex_brief.json", {"candidates": [{"id": f"clip-{index}"}]})

    status, _headers, payload = handle_api_request(
        "GET",
        "/api/runs?phase=needs_review&offset=20&limit=20",
        WebPaths(output_root=output_root, service_dir=tmp_path / "missing-service"),
    )

    assert status == 200
    assert payload["total"] == 25
    assert payload["count"] == 5
    assert payload["has_more"] is False
    assert payload["phase_counts"]["all"] == 25
    assert payload["phase_counts"]["needs_review"] == 25
    assert {run["phase"] for run in payload["runs"]} == {"needs_codex_selection"}


def test_scan_and_retry_configuration_errors_use_http_409(tmp_path, monkeypatch):
    paths = WebPaths(
        output_root=tmp_path / "output",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "missing.toml",
    )
    run_dir = tmp_path / "workspace" / "runs" / "run-failed" / "output"
    write_json(paths.service_dir / "runs.json", {"runs": [{
        "run_id": "run-failed",
        "phase": "failed",
        "run_dir": str(run_dir),
        "input_dir": str(run_dir.parent / "input"),
        "source_path": str(tmp_path / "source.mkv"),
        "local_source_path": str(run_dir.parent / "input" / "source.mkv"),
        "last_error": "old error",
    }]})
    monkeypatch.setattr(web, "_settings_for_paths", lambda request_paths: web.Settings())

    scan_status, _headers, scan = handle_api_request("POST", "/api/service/scan-now", paths)
    retry_status, _headers, retry = handle_api_request("POST", "/api/runs/run-failed/retry", paths)

    assert scan_status == 409
    assert retry_status == 409
    assert scan["error_code"] == retry["error_code"] == "pipeline_configuration_required"
    assert "设置 → AI 服务" in scan["message"]


def test_empty_selection_disables_render_and_cleanup_and_returns_http_409(tmp_path):
    service_dir = tmp_path / "service"
    run_dir = tmp_path / "workspace" / "runs" / "run-empty" / "output"
    write_json(run_dir / "codex_brief.json", {"candidates": []})
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "selected_clips.json", [])
    write_json(
        service_dir / "runs.json",
        {
            "runs": [
                {
                    "run_id": "run-empty",
                    "phase": "needs_review",
                    "run_dir": str(run_dir),
                    "input_dir": str(run_dir.parent / "input"),
                    "source_path": str(tmp_path / "source.mkv"),
                    "local_source_path": None,
                }
            ]
        },
    )
    paths = WebPaths(service_dir=service_dir, config_path=tmp_path / "missing.toml")

    detail = build_run_detail("run-empty", paths)
    status, _headers, payload = handle_api_request("POST", "/api/runs/run-empty/render", paths)

    assert detail["actions"]["can_render"] is False
    assert detail["actions"]["can_cleanup_preview"] is False
    assert detail["actions"]["can_cleanup"] is False
    assert detail["actions"]["can_ai_review"] is True
    assert status == 409
    assert payload["error_code"] == "selection_empty"


def test_static_assets_disable_cache_and_serve_hashed_react_js(tmp_path):
    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(output_root=tmp_path / "output", state_dir=tmp_path / "state", log_dir=tmp_path / "logs")

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        index_response = urlopen(f"{base_url}/", timeout=5)
        html = index_response.read().decode("utf-8")
        assert index_response.headers["Cache-Control"] == "no-store"
        script_match = re.search(
            r'<script[^>]+src="(/static/react/assets/index-[A-Za-z0-9_-]+\.js)"[^>]*></script>',
            html,
        )
        assert script_match

        app_response = urlopen(f"{base_url}{script_match.group(1)}", timeout=5)
        assert app_response.status == 200
        assert "javascript" in app_response.headers["Content-Type"]
        assert app_response.headers["Cache-Control"] == "no-store"
        assert b"/api/review-automation/run-due" in app_response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_handle_api_request_deletes_single_generated_clip(tmp_path):
    output_root = tmp_path / "output"
    run_dir = output_root / "recording"
    clip_path = run_dir / "clips" / "clip_01.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"clip")
    write_json(run_dir / "run_metadata.json", {"source_name": "recording.mkv"})

    status_code, _headers, body = handle_api_request(
        "POST",
        "/api/runs/recording/clips/clip_01.mp4/delete",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs"),
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["deleted"] == str(clip_path)
    assert not clip_path.exists()


def test_handle_api_request_deletes_only_local_source_copy(tmp_path):
    output_root = tmp_path / "output"
    input_dir = tmp_path / "input"
    run_dir = output_root / "recording"
    local_source = input_dir / "recording.mkv"
    nas_source = tmp_path / "nas" / "recording.mkv"
    local_source.parent.mkdir(parents=True)
    nas_source.parent.mkdir(parents=True)
    local_source.write_bytes(b"local video")
    nas_source.write_bytes(b"nas video")
    (run_dir / "clips").mkdir(parents=True)
    (run_dir / "clips" / "clip_01.mp4").write_bytes(b"clip")
    write_json(run_dir / "selected_clips.json", {"candidates": [{"id": "clip_01"}]})
    write_json(run_dir / "run_metadata.json", {
        "source_name": "recording.mkv",
        "pipeline": {
            "local_source_path": str(local_source),
            "original_source_path": str(nas_source),
        },
    })

    status_code, _headers, body = handle_api_request(
        "POST",
        "/api/runs/recording/delete-local-source",
        WebPaths(output_root=output_root, state_dir=tmp_path / "state", log_dir=tmp_path / "logs", input_dir=input_dir),
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["deleted"] == str(local_source)
    assert not local_source.exists()
    assert nas_source.exists()


def test_project_routes_use_sqlite_and_global_scan_is_blocked(tmp_path):
    service_dir = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    repository = open_project_repository(service_dir)
    project = ProjectManager(repository, Settings()).create_project(
        name="项目",
        config=default_project_config(source, output),
        activation_state="inactive",
        request_id="create-web-project",
    )
    repository.close()
    paths = WebPaths(service_dir=service_dir, config_path=tmp_path / "missing.toml")

    status, _headers, projects = handle_api_request("GET", "/api/projects", paths)
    run_status, _headers, missing_run = handle_api_request("GET", "/api/runs/not-found", paths)
    scan_status, _headers, scan = handle_api_request("POST", "/api/service/scan-now", paths)

    assert status == 200
    assert projects["projects"][0]["project_id"] == project.project_id
    assert run_status == 404 and missing_run["error"]["code"] == "run_not_found"
    assert scan_status == 409 and scan["error"]["code"] == "project_scope_required"
    assert not (service_dir / "runs.json").exists()


def test_spa_history_route_returns_react_index(tmp_path):
    class TestHandler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=tmp_path / "service")

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        root = urlopen(f"{base_url}/", timeout=5).read()
        deep = urlopen(f"{base_url}/projects/project-1/runs/run-1", timeout=5)
        assert deep.status == 200
        assert deep.headers["Content-Type"] == "text/html; charset=utf-8"
        assert deep.read() == root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
