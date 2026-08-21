from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from live_clipper.config import RecordingSourceDefaultConfig, Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_scan import ProjectScanError, list_source_files, scan_project
from live_clipper.project_service import ProjectManager, open_project_repository


def _project(tmp_path, name="P"):
    source = tmp_path / f"source-{name}"
    output = tmp_path / f"output-{name}"
    source.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    config = default_project_config(source, output)
    repo = open_project_repository(tmp_path / "service")
    manager = ProjectManager(repo, Settings(cheap_model_api_key="fake"))
    project = manager.create_project(name=name, config=config, activation_state="active")
    settings = Settings(
        cheap_model_api_key="fake",
        recording_source_default=RecordingSourceDefaultConfig(min_age_minutes=0, stable_check_seconds=0),
    )
    return repo, project, source, settings


def test_scan_deduplicates_within_project_and_lists_relative_sources(tmp_path):
    repo, project, source, settings = _project(tmp_path)
    (source / "b.mp4").write_bytes(b"same-content")
    first = scan_project(repo, project.project_id, settings=settings, service_dir=tmp_path / "service")
    second = scan_project(repo, project.project_id, settings=settings, service_dir=tmp_path / "service")
    assert first.created_count == 1
    assert second.created_count == 0 and second.duplicate_count == 1
    assert len(repo.list_runs(project.project_id)) == 1
    assert [item.relative_path for item in list_source_files(repo, project.project_id)] == ["b.mp4"]


def test_selected_scan_rejects_traversal_and_isolates_file_failure(tmp_path):
    repo, project, source, settings = _project(tmp_path)
    fixed_now = datetime(2026, 8, 21, tzinfo=UTC)
    stable_mtime = (fixed_now - timedelta(seconds=1)).timestamp()
    repo.update_runtime(
        project.project_id,
        discovery_baseline=(fixed_now - timedelta(seconds=2)).isoformat(),
    )
    ok_path = source / "ok.mp4"
    bad_path = source / "bad.mp4"
    ok_path.write_bytes(b"ok")
    bad_path.write_bytes(b"bad")
    os.utime(ok_path, (stable_mtime, stable_mtime))
    os.utime(bad_path, (stable_mtime, stable_mtime))
    with pytest.raises(ProjectScanError) as error:
        scan_project(
            repo,
            project.project_id,
            scope="selected",
            selected_relative_paths=["../outside.mp4"],
            settings=settings,
            service_dir=tmp_path / "service",
        )
    assert error.value.code == "source_path_outside_project"

    def identity(path, **_kwargs):
        if path.name == "bad.mp4":
            raise OSError("cannot read")
        return {"content_id": "ok-id", "bytes": path.stat().st_size, "cache_hit": False}

    report = scan_project(
        repo,
        project.project_id,
        settings=settings,
        service_dir=tmp_path / "service",
        identity_fn=identity,
        now=fixed_now,
    )
    assert report.status == "partial"
    assert report.created_count == 1 and report.failed_count == 1


def test_recent_selected_and_cross_project_content_identity(tmp_path):
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    output_a = tmp_path / "output-a"
    output_b = tmp_path / "output-b"
    for path in (source_a, source_b, output_a, output_b):
        path.mkdir()
    recent_now = datetime(2026, 8, 20, tzinfo=UTC)
    recent = source_a / "recent.mp4"
    old = source_a / "old.mp4"
    recent.write_bytes(b"same")
    old.write_bytes(b"old")
    os.utime(recent, (1787112000, 1787112000))  # 2026-08-19 UTC
    os.utime(old, (1786680000, 1786680000))  # 2026-08-14 UTC
    config_a = default_project_config(source_a, output_a)
    config_a["source"].update(first_scan_mode="recent", lookback_days=3)
    settings = Settings(
        cheap_model_api_key="fake",
        recording_source_default=RecordingSourceDefaultConfig(min_age_minutes=0, stable_check_seconds=0),
    )
    repo = open_project_repository(tmp_path / "service")
    manager = ProjectManager(repo, settings)
    project_a = manager.create_project(name="A", config=config_a, activation_state="active")
    repo.update_runtime(project_a.project_id, discovery_baseline="2026-08-20T00:00:00Z")

    recent_report = scan_project(
        repo, project_a.project_id, settings=settings, service_dir=tmp_path / "service", now=recent_now
    )
    selected_report = scan_project(
        repo,
        project_a.project_id,
        scope="selected",
        selected_relative_paths=["old.mp4"],
        settings=settings,
        service_dir=tmp_path / "service",
        now=recent_now,
    )
    cross_now = datetime(2026, 8, 21, tzinfo=UTC)
    copy_path = source_b / "copy.mp4"
    copy_path.write_bytes(b"same")
    copy_mtime = (cross_now - timedelta(seconds=1)).timestamp()
    os.utime(copy_path, (copy_mtime, copy_mtime))
    config_b = default_project_config(source_b, output_b)
    project_b = manager.create_project(name="B", config=config_b, activation_state="active")
    cross_report = scan_project(
        repo,
        project_b.project_id,
        scope="selected",
        selected_relative_paths=["copy.mp4"],
        settings=settings,
        service_dir=tmp_path / "service",
        now=cross_now,
    )

    assert recent_report.created_count == 1 and recent_report.excluded_count == 1
    assert selected_report.created_count == 1
    assert cross_report.created_count == 1
    assert len(repo.list_runs()) == 3


def test_paused_project_allows_manual_scan_but_inactive_does_not(tmp_path):
    repo, project, source, settings = _project(tmp_path)
    (source / "paused.mp4").write_bytes(b"paused")
    ProjectManager(repo, settings).pause_project(project.project_id, request_id="pause")
    report = scan_project(repo, project.project_id, settings=settings, service_dir=tmp_path / "service")
    assert report.created_count == 1

    inactive_config = default_project_config(tmp_path / "inactive-source", tmp_path / "inactive-output")
    (tmp_path / "inactive-source").mkdir()
    (tmp_path / "inactive-output").mkdir()
    inactive = ProjectManager(repo, settings).create_project(name="inactive", config=inactive_config, activation_state="inactive")
    with pytest.raises(ProjectScanError) as error:
        scan_project(repo, inactive.project_id, settings=settings, service_dir=tmp_path / "service")
    assert error.value.code == "project_inactive"


def test_scan_rechecks_output_writability_before_creating_scan_or_run(tmp_path):
    repo, project, source, settings = _project(tmp_path)
    candidate = source / "unwritable.mp4"
    candidate.write_bytes(b"synthetic")
    revision = repo.get_config_revision(project.project_id)
    assert revision is not None
    output = Path(revision.config["output"]["directory"])
    original_mode = stat.S_IMODE(output.stat().st_mode)
    output.chmod(original_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    try:
        with pytest.raises(ProjectScanError) as error:
            scan_project(
                repo,
                project.project_id,
                scope="selected",
                selected_relative_paths=[candidate.name],
                settings=settings,
                service_dir=tmp_path / "service",
            )
    finally:
        output.chmod(original_mode)

    assert error.value.code == "output_unwritable"
    assert repo.list_scan_events(project.project_id) == []
    assert repo.list_runs(project.project_id) == []
