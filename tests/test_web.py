from __future__ import annotations

import re
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

from live_clipper import web
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
