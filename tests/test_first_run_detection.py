from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from live_clipper.config import DEFAULT_CONFIG_TEMPLATE
from live_clipper.first_run_detection import detect_first_run_environment, inspect_startup
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import ProjectError, open_project_repository
from live_clipper.project_storage import database_path


def _fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(item.relative_to(root)): (
            item.stat().st_size,
            item.stat().st_mtime_ns,
            hashlib.sha256(item.read_bytes()).hexdigest(),
        )
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def test_empty_template_and_env_are_new_and_detection_is_read_only(tmp_path):
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    service_dir = tmp_path / "work" / "service"
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(config_path=config_path, env_path=env_path, service_dir=service_dir)
    decision = inspect_startup(config_path=config_path, env_path=env_path, service_dir=service_dir)

    assert not detection.has_legacy_evidence
    assert detection.data_mode == "absent" and detection.project_count == 0
    assert decision.entry == "onboarding" and decision.onboarding == "new"
    assert _fingerprint(tmp_path) == before
    assert not database_path(service_dir).exists()


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        ("metadata", "legacy_metadata"),
        ("marker_completed", "legacy_onboarding_marker"),
        ("marker_skipped", "legacy_onboarding_marker"),
        ("global_source", "legacy_global_source_configured"),
    ],
)
def test_legacy_evidence_routes_to_migration_without_writes(tmp_path, fixture, expected_code):
    config_path = tmp_path / "live-clipper.toml"
    env_path = tmp_path / ".env"
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    env_path.write_text("SENTINEL_KEY=do-not-read-or-copy\n", encoding="utf-8")
    if fixture == "metadata":
        (service_dir / "runs.json").write_text('{"runs": []}', encoding="utf-8")
    elif fixture.startswith("marker"):
        key = "completed_at" if fixture.endswith("completed") else "skipped_at"
        (service_dir / "onboarding.json").write_text(json.dumps({key: "2026-01-01T00:00:00Z"}), encoding="utf-8")
    else:
        config_path.write_text(
            DEFAULT_CONFIG_TEMPLATE.replace('source_dir = ""\nsince_hours = 36', 'source_dir = "/Volumes/archive"\nsince_hours = 36'),
            encoding="utf-8",
        )
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(config_path=config_path, env_path=env_path, service_dir=service_dir)
    decision = inspect_startup(config_path=config_path, env_path=env_path, service_dir=service_dir)

    assert expected_code in detection.evidence_codes
    assert decision.entry == "migration_required"
    assert _fingerprint(tmp_path) == before
    assert "SENTINEL_KEY" not in repr(detection) + repr(decision)


def test_legacy_only_preflight_blocks_repository_creation(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    before = _fingerprint(tmp_path)
    with pytest.raises(ProjectError) as error:
        open_project_repository(service_dir)
    assert error.value.code == "migration_required"
    assert _fingerprint(tmp_path) == before
    assert not database_path(service_dir).exists()


def test_projects_and_first_run_facts_are_read_with_no_database_writes(tmp_path):
    service_dir = tmp_path / "service"
    repository = open_project_repository(service_dir)
    repository.begin_first_run_session()
    repository.close()
    before = _fingerprint(tmp_path)

    detection = detect_first_run_environment(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )

    assert detection.has_project_database and detection.data_mode == "projects"
    assert detection.has_first_run_session and detection.project_count == 0
    assert decision.entry == "onboarding" and decision.onboarding == "resume"
    assert _fingerprint(tmp_path) == before


def test_existing_project_without_session_routes_to_workbench(tmp_path):
    service_dir = tmp_path / "service"
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    repository = open_project_repository(service_dir)
    project = repository.create_project("已有项目", default_project_config(source, output))
    repository.close()

    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert project.project_id
    assert decision.entry == "workbench" and decision.onboarding is None


def test_unexplained_legacy_v2_database_is_diagnostic_and_not_upgraded(tmp_path):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    connection = sqlite3.connect(database_path(service_dir))
    connection.executescript(
        """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE system_state(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
INSERT INTO schema_migrations VALUES (1, 'v1', '2026-08-29T00:00:00Z');
INSERT INTO schema_migrations VALUES (2, 'v2', '2026-08-29T00:00:01Z');
INSERT INTO system_state VALUES ('data_mode', 'legacy', '2026-08-29T00:00:01Z');
"""
    )
    connection.close()
    before = _fingerprint(tmp_path)

    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert decision.entry == "diagnostic_required"
    with pytest.raises(ProjectError) as error:
        open_project_repository(service_dir)
    assert error.value.code == "diagnostic_required"
    assert _fingerprint(tmp_path) == before
    check = sqlite3.connect(database_path(service_dir))
    assert check.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]


def test_legacy_import_and_projects_conflict_is_diagnostic(tmp_path):
    service_dir = tmp_path / "service"
    repository = open_project_repository(service_dir)
    repository.begin_first_run_session()
    repository.connection.execute(
        """INSERT INTO legacy_imports(
             import_id, source_fingerprint, plan_json, backup_path, status, summary_json, created_at, completed_at
           ) VALUES ('import-1', 'fingerprint', '{}', 'backup', 'completed', '{}', ?, ?)""",
        ("2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
    )
    repository.close()

    detection = detect_first_run_environment(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    decision = inspect_startup(
        config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing.env", service_dir=service_dir
    )
    assert "legacy_project_import" in detection.evidence_codes
    assert decision.entry == "diagnostic_required"
