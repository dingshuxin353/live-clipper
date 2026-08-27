from __future__ import annotations

import json

import pytest

from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_service import ProjectError, ProjectManager, open_project_repository


def _ready_config(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    return default_project_config(source, output)


def test_empty_directory_enters_projects_mode_but_legacy_metadata_does_not(tmp_path):
    empty = tmp_path / "empty"
    repo = open_project_repository(empty)
    assert repo.get_data_mode() == "projects"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "runs.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
    legacy_repo = open_project_repository(legacy)
    assert legacy_repo.get_data_mode() == "legacy"
    manager = ProjectManager(legacy_repo, Settings())
    with pytest.raises(ProjectError) as error:
        manager.create_project(name="blocked", config=_ready_config(tmp_path), activation_state="inactive")
    assert error.value.code == "migration_required"


def test_validation_and_project_lifecycle(tmp_path):
    repo = open_project_repository(tmp_path / "service")
    manager = ProjectManager(repo, Settings(cheap_model_api_key="fake-key"))
    config = _ready_config(tmp_path)
    validation = manager.validate_project(name="项目一", config=config, activation_state="active")
    assert validation.ok
    project = manager.create_project(name="项目一", config=config, activation_state="active", request_id="create-1")
    repeated = manager.create_project(name="项目一", config=config, activation_state="active", request_id="create-1")
    assert repeated.project_id == project.project_id
    assert manager.pause_project(project.project_id, request_id="pause-1").activation_state == "paused"
    resumed = manager.resume_project(project.project_id, request_id="resume-1")
    assert resumed.activation_state == "active"
    assert repo.get_runtime(project.project_id).next_scan_at is None


def test_output_nested_in_source_is_a_blocker_even_through_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "clips"
    nested.mkdir()
    alias = tmp_path / "output-alias"
    alias.symlink_to(nested, target_is_directory=True)
    config = default_project_config(source, alias)
    manager = ProjectManager(open_project_repository(tmp_path / "service"), Settings(cheap_model_api_key="fake"))
    result = manager.validate_project(name="项目", config=config, activation_state="active")
    assert any(issue.code == "output_inside_source" for issue in result.blockers)


def test_existing_v1_project_upgrades_current_config_once_without_rewriting_run_snapshot(tmp_path):
    repository = open_project_repository(tmp_path / "service")
    config_v1 = _ready_config(tmp_path)
    project = repository.create_project("旧项目", config_v1, activation_state="inactive")
    run = repository.create_normal_run(
        project_id=project.project_id,
        content_id="legacy-content",
        trigger_source="manual",
        first_seen_path=str(tmp_path / "source" / "legacy.mp4"),
        latest_seen_path=str(tmp_path / "source" / "legacy.mp4"),
        parameter_snapshot={"schema_version": 1, "legacy": True},
    ).run
    manager = ProjectManager(repository, Settings(cheap_model_api_key="fake"))

    revision = manager.ensure_v2_config(project.project_id)
    repeated = manager.ensure_v2_config(project.project_id)

    assert revision == repeated == 2
    assert repository.get_config_revision(project.project_id).schema_version == 2
    assert repository.get_config_revision(project.project_id, 1).config == config_v1
    assert repository.get_run(run.run_id).parameter_snapshot == {"schema_version": 1, "legacy": True}
    events = [item for item in repository.list_workspace_events() if item.event_type == "project_config_upgraded_v2"]
    assert len(events) == 1
