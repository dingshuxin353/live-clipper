from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from live_clipper.project_migration import apply_migration_plan, build_migration_plan, inspect_legacy_state
from live_clipper.project_storage import ProjectRepository


def _legacy_tree(tmp_path):
    fixture_dir = Path(__file__).parent / "fixtures" / "migration_v0_3"
    config = tmp_path / "live-clipper.toml"
    shutil.copy2(fixture_dir / "live-clipper.toml", config)
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    shutil.copy2(fixture_dir / "runs.json", service_dir / "runs.json")
    return config, service_dir


def test_explicit_migration_backs_up_is_idempotent_and_quarantines(tmp_path):
    config, service_dir = _legacy_tree(tmp_path)
    original_config = config.read_bytes()
    original_runs = (service_dir / "runs.json").read_bytes()
    inspected = inspect_legacy_state(service_dir=service_dir, config_path=config)
    plan = build_migration_plan(inspected)

    assert plan.needs_user_review
    assert [item["legacy_run_id"] for item in plan.quarantined_runs] == ["old-no-content"]
    repo = ProjectRepository(service_dir)
    first = apply_migration_plan(plan, repo)
    second = apply_migration_plan(plan, repo)

    assert first.completed and second.already_applied
    assert first.project_id == second.project_id
    assert len(repo.list_projects()) == 1
    assert len(repo.list_runs(first.project_id)) == 1
    assert repo.list_runs(first.project_id)[0].parameter_snapshot["legacy_run_id"] == "old-1"
    assert config.read_bytes() == original_config
    assert (service_dir / "runs.json").read_bytes() == original_runs
    assert first.backup_path.is_dir()
    assert (first.backup_path / "live-clipper.toml").read_bytes() == original_config
    assert (first.backup_path / "runs.json").read_bytes() == original_runs
    assert "sk-fixture-secret" not in service_dir.joinpath("venus.sqlite3").read_bytes().decode("utf-8", errors="ignore")
    assert "sk-fixture-secret" not in repr(plan)


def test_building_a_plan_has_no_database_or_backup_side_effect(tmp_path):
    config, service_dir = _legacy_tree(tmp_path)
    plan = build_migration_plan(inspect_legacy_state(service_dir=service_dir, config_path=config))
    assert plan.source_fingerprint
    assert not (service_dir / "venus.sqlite3").exists()
    assert not (service_dir / "migration-backups").exists()


def test_failed_database_conversion_leaves_legacy_bytes_and_mode_untouched(tmp_path, monkeypatch):
    config, service_dir = _legacy_tree(tmp_path)
    before = {config: config.read_bytes(), service_dir / "runs.json": (service_dir / "runs.json").read_bytes()}
    plan = build_migration_plan(inspect_legacy_state(service_dir=service_dir, config_path=config))
    repo = ProjectRepository(service_dir)

    def fail(**_kwargs):
        raise RuntimeError("database fault")

    monkeypatch.setattr(repo, "apply_legacy_import", fail)
    with pytest.raises(RuntimeError, match="database fault"):
        apply_migration_plan(plan, repo)

    assert repo.get_data_mode() == "legacy"
    assert repo.list_projects() == []
    assert all(path.read_bytes() == content for path, content in before.items())
