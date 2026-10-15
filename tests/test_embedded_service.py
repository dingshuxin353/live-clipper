from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from live_clipper import service
from live_clipper.config import RecordingSourceDefaultConfig, Settings
from live_clipper.utils import read_json


@pytest.fixture(autouse=True)
def _stop_embedded_after_test():
    yield
    service.stop_embedded_service()


def _wait_for_state(service_dir, predicate, timeout_seconds=5.0):
    deadline = time.monotonic() + timeout_seconds
    state_path = service_dir / "service.json"
    while time.monotonic() < deadline:
        if state_path.exists():
            state = read_json(state_path)
            if predicate(state):
                return state
        time.sleep(0.05)
    raise AssertionError("embedded service state did not reach expected condition")


def test_embedded_service_waits_when_source_not_configured(tmp_path):
    service_dir = tmp_path / "service"
    report = service.start_embedded_service(lambda: Settings(), service_dir=service_dir)
    assert report["ok"] is True
    assert report["embedded"] is True
    state = _wait_for_state(service_dir, lambda s: s.get("waiting") == "recording_source_not_configured")
    assert state["status"] == "running"
    assert service.embedded_service_active() is True


def test_embedded_service_start_is_idempotent(tmp_path):
    service_dir = tmp_path / "service"
    first = service.start_embedded_service(lambda: Settings(), service_dir=service_dir)
    second = service.start_embedded_service(lambda: Settings(), service_dir=service_dir)
    assert first["started"] is True
    assert second["started"] is False
    assert second["reason"] == "embedded_service_already_running"


def test_embedded_service_pause_and_resume(tmp_path):
    service_dir = tmp_path / "service"
    service.start_embedded_service(lambda: Settings(), service_dir=service_dir)
    _wait_for_state(service_dir, lambda s: s.get("status") == "running")

    paused = service.pause_embedded_service()
    assert paused["paused"] is True
    _wait_for_state(service_dir, lambda s: s.get("status") == "paused")

    resumed = service.resume_embedded_service()
    assert resumed["resumed"] is True
    _wait_for_state(service_dir, lambda s: s.get("status") == "running")


def test_embedded_service_runs_tick_with_configured_source(tmp_path):
    source = tmp_path / "recordings"
    source.mkdir()
    service_dir = tmp_path / "service"

    def loader():
        return Settings(
            recording_source_default=RecordingSourceDefaultConfig(
                source_dir=source,
                input_dir=tmp_path / "input",
                output_root=tmp_path / "output",
            )
        )

    service.start_embedded_service(loader, service_dir=service_dir)
    state = _wait_for_state(service_dir, lambda s: "last_report" in s)
    assert state["status"] == "running"
    assert state["last_report"]["ok"] is True


def test_stop_embedded_service_terminates_thread(tmp_path):
    service_dir = tmp_path / "service"
    service.start_embedded_service(lambda: Settings(), service_dir=service_dir)
    assert service.embedded_service_active() is True
    result = service.stop_embedded_service()
    assert result["stopped"] is True
    assert service.embedded_service_active() is False
    _wait_for_state(service_dir, lambda s: s.get("status") == "stopped")


def test_embedded_start_preserves_existing_start_time(tmp_path):
    started_at = "2026-10-15T00:00:00+00:00"
    service._write_service_state(tmp_path, {"status": "stopped", "started_at": started_at})
    service.start_embedded_service(lambda: Settings(), service_dir=tmp_path)
    state = _wait_for_state(tmp_path, lambda s: s.get("waiting") == "recording_source_not_configured")
    assert state["started_at"] == started_at


@pytest.mark.parametrize("existing", [False, True])
def test_status_readers_see_only_complete_publications(tmp_path, monkeypatch, existing):
    service_dir = tmp_path / "service"
    if existing:
        service._write_service_state(service_dir, {"status": "stopped", "generation": "old"})
    writing, release = Event(), Event()
    original_write = Path.write_text

    def paused_write(path, data, *args, **kwargs):
        if path.parent != service_dir or not data.startswith('{\n'):
            return original_write(path, data, *args, **kwargs)
        with path.open("w", encoding="utf-8") as stream:
            stream.write(data[:1])
            stream.flush()
            writing.set()
            assert release.wait(5), "reader did not release writer"
            return 1 + stream.write(data[1:])

    monkeypatch.setattr(Path, "write_text", paused_write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service._write_service_state, service_dir, {"status": "stopped", "generation": "new"},
        )
        try:
            assert writing.wait(5), "writer did not reach file write"
            state = service.get_service_status(service_dir=service_dir)["service"]
            assert state.get("generation") == ("old" if existing else None)
            if existing:
                assert _wait_for_state(service_dir, lambda s: s.get("generation") == "old")["generation"] == "old"
            else:
                assert not (service_dir / "service.json").exists()
        finally:
            release.set()
        future.result(timeout=5)
    assert _wait_for_state(service_dir, lambda s: s.get("generation") == "new")["generation"] == "new"
