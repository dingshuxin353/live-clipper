from __future__ import annotations

from pathlib import Path

from live_clipper.web import WebPaths, handle_api_request


def _paths(root: Path) -> WebPaths:
    return WebPaths(
        output_root=root / "output",
        state_dir=root / "state",
        log_dir=root / "logs",
        input_dir=root / "input",
        service_dir=root / "work" / "service",
        config_path=root / "live-clipper.toml",
    )


def test_new_onboarding_route_lifecycle_has_frozen_dto(tmp_path: Path, monkeypatch) -> None:
    from live_clipper import first_run_detection

    plans = []
    original = first_run_detection.build_migration_plan

    def plan(*args, **kwargs):
        result = original(*args, **kwargs)
        plans.append(result)
        return result

    monkeypatch.setattr(first_run_detection, 'build_migration_plan', plan)
    paths = _paths(tmp_path)
    status, _headers, payload = handle_api_request("GET", "/api/onboarding", paths)
    assert status == 200
    assert payload["entry"]["mode"] == "onboarding"
    status, _headers, started = handle_api_request("POST", "/api/onboarding/start", paths, body={})
    assert status == 201
    assert started["session"]["state"] == "in_progress"
    status, _headers, repeated = handle_api_request("POST", "/api/onboarding/start", paths, body={})
    assert status == 200
    assert repeated["reused"] is True
    status, _, paused = handle_api_request("POST", "/api/onboarding/pause", paths,
                                          body={"request_id": "pause", "expected_revision": 1})
    assert status == 200 and paused["session"]["state"] == "paused"
    status, _, current = handle_api_request("GET", "/api/onboarding", paths)
    assert status == 200 and current["entry"]["onboarding"] == "paused"
    assert current["session"]["state"] == "paused" and current["migration"] is None
    status, _, resumed = handle_api_request("POST", "/api/onboarding/resume", paths,
                                           body={"request_id": "resume", "expected_revision": paused["session"]["revision"]})
    assert status == 200 and resumed["session"]["state"] == "in_progress"
    assert not plans


def test_unknown_method_and_body_are_structured_errors(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    status, _headers, payload = handle_api_request("POST", "/api/onboarding/start", paths, body={"secret": "sentinel"})
    assert status == 422
    assert payload["error"]["code"] == "validation_failed"
    status, _headers, payload = handle_api_request("GET", "/api/onboarding/start", paths)
    assert status == 404
    assert payload["error_code"] == "route_not_found"
