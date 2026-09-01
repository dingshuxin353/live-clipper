from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time

import pytest

from live_clipper import service
from live_clipper.migration_coordinator import MigrationCoordinator, MigrationError
from live_clipper.project_storage import ProjectRepository, database_path


@pytest.fixture(autouse=True)
def _ready_service(monkeypatch):
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True})


def _legacy_home(tmp_path):
    service = tmp_path / "work" / "service"
    source = tmp_path / "recordings"
    output = tmp_path / "output"
    service.mkdir(parents=True)
    source.mkdir()
    output.mkdir()
    config = tmp_path / "live-clipper.toml"
    config.write_text(
        f'''[recording_source.default]
source_dir = "{source}"
output_root = "{output}"
[scheduler]
timezone = "Asia/Tokyo"
[asr]
model = "ready-asr"
api_key = "SENTINEL-ASR-SECRET"
[llm]
model = "ready-ai"
api_key = "SENTINEL-AI-SECRET"
''',
        encoding="utf-8",
    )
    (service / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "old-completed",
                        "content_id": "content-1",
                        "source_path": str(source / "one.mp4"),
                        "phase": "completed",
                        "created_at": "2026-08-01T00:00:00Z",
                    },
                    {"run_id": "unknown", "phase": "mystery"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return MigrationCoordinator(
        service_dir=service,
        config_path=config,
        env_path=tmp_path / ".env",
        input_dir=tmp_path / "input",
        output_root=output,
    ), service


def _validated(coordinator):
    inspected = coordinator.inspect({})[1]
    plan = inspected["plan"]
    validated = coordinator.validate(
        {
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": {"project_name": "迁移项目", "trigger_mode": "manual"},
        }
    )[1]
    return validated["plan"]


def _wait_completed(coordinator, migration_id):
    for _ in range(200):
        payload = coordinator.snapshot()
        if payload["session"] and payload["session"]["migration_id"] == migration_id:
            if payload["session"]["state"] in {
                "completed_ready",
                "completed_attention",
                "failed_rolled_back",
            }:
                return payload
        time.sleep(0.01)
    raise AssertionError("migration did not finish")


def test_inspect_validate_are_zero_write_and_secret_free(tmp_path):
    coordinator, service = _legacy_home(tmp_path)
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    plan = _validated(coordinator)
    assert not database_path(service).exists()
    assert not (tmp_path / "work" / "migration-backups").exists()
    assert "source_manifest" not in plan
    assert "SENTINEL" not in json.dumps(plan)
    after = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_execute_is_idempotent_and_atomically_switches_to_projects(tmp_path):
    coordinator, service = _legacy_home(tmp_path)
    legacy_before = {
        path.name: path.read_bytes()
        for path in (coordinator.config_path, service / "runs.json")
    }
    plan = _validated(coordinator)
    body = {
        "request_id": "request-1",
        "source_fingerprint": plan["source_fingerprint"],
        "plan_hash": plan["plan_hash"],
        "choices": plan["choices"],
    }
    first = coordinator.execute(body)[1]
    replay = coordinator.execute(body)[1]
    assert replay["session"]["migration_id"] == first["session"]["migration_id"]
    completed = _wait_completed(coordinator, first["session"]["migration_id"])
    assert completed["session"]["state"] == "completed_ready"
    with ProjectRepository(service) as repository:
        assert repository.get_data_mode() == "projects"
        assert len(repository.list_projects()) == 1
        runs = repository.list_runs()
        assert len(runs) == 1 and runs[0].trigger_source == "legacy_import"
        assert runs[0].status == "completed"
        assert repository.connection.execute("SELECT count(*) FROM issues").fetchone()[0] == 0
    backup = tmp_path / "work" / "migration-backups" / first["session"]["migration_id"]
    assert backup.is_dir() and (backup / "manifest.json").is_file()
    assert "SENTINEL" not in (backup / "manifest.json").read_text(encoding="utf-8")
    for database_file in service.glob("venus.sqlite3*"):
        assert b"SENTINEL" not in database_file.read_bytes()
    assert {path.name: path.read_bytes() for path in (coordinator.config_path, service / "runs.json")} == legacy_before


def test_execute_rejects_source_change_and_request_conflict(tmp_path):
    coordinator, _service = _legacy_home(tmp_path)
    plan = _validated(coordinator)
    body = {
        "request_id": "request-1",
        "source_fingerprint": plan["source_fingerprint"],
        "plan_hash": plan["plan_hash"],
        "choices": plan["choices"],
    }
    changed = dict(body)
    changed["choices"] = {**plan["choices"], "project_name": "另一个项目"}
    coordinator.execute(body)
    with pytest.raises(MigrationError) as conflict:
        coordinator.execute(changed)
    assert conflict.value.code == "request_id_conflict"


@pytest.mark.parametrize(
    "choices, expected",
    [
        ({"trigger_mode": "manual"}, (False, "daily", None)),
        (
            {"trigger_mode": "scheduled", "schedule_mode": "daily", "daily_time": "09:30"},
            (True, "daily", None),
        ),
        (
            {"trigger_mode": "scheduled", "schedule_mode": "interval", "interval_minutes": 60},
            (True, "interval", 60),
        ),
    ],
)
def test_trigger_choices_become_real_project_config(tmp_path, choices, expected):
    coordinator, service = _legacy_home(tmp_path)
    inspected = coordinator.inspect({})[1]["plan"]
    plan = coordinator.validate(
        {
            "source_fingerprint": inspected["source_fingerprint"],
            "plan_hash": inspected["plan_hash"],
            "choices": choices,
        }
    )[1]["plan"]
    accepted = coordinator.execute(
        {
            "request_id": "trigger-request",
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": plan["choices"],
        }
    )[1]
    _wait_completed(coordinator, accepted["session"]["migration_id"])
    with ProjectRepository(service) as repository:
        project = repository.list_projects()[0]
        config = repository.get_config_revision(project.project_id).config
        assert (
            config["schedule"]["enabled"],
            config["schedule"]["mode"],
            config["schedule"]["interval_minutes"],
        ) == expected


def test_restart_recovery_marks_unowned_execution_failed_without_resuming(tmp_path):
    coordinator, service = _legacy_home(tmp_path)
    plan = _validated(coordinator)
    with ProjectRepository(service) as repository:
        session = repository.create_migration_session(
            migration_id="interrupted-migration",
            source_fingerprint=plan["source_fingerprint"],
            plan_version=plan["plan_version"],
            plan_hash=plan["plan_hash"],
            source_manifest=[],
            choices=plan["choices"],
            request_id="interrupted-request",
            request_hash="a" * 64,
            backup_path=str(tmp_path / "missing-backup"),
        )
    recovered = coordinator.recover_interrupted()
    assert recovered is not None and recovered.state == "failed_rolled_back"
    assert recovered.revision == session.revision + 1
    with ProjectRepository(service) as repository:
        assert repository.get_data_mode() == "legacy"
        assert repository.list_projects() == []


def test_service_start_failure_becomes_completed_attention_on_same_project(tmp_path, monkeypatch):
    coordinator, service_dir = _legacy_home(tmp_path)
    monkeypatch.setattr(
        service,
        "ensure_service_ready",
        lambda *args, **kwargs: {"ok": False, "error_code": "service_not_ready"},
    )
    plan = _validated(coordinator)
    accepted = coordinator.execute(
        {
            "request_id": "service-failure",
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": plan["choices"],
        }
    )[1]
    completed = _wait_completed(coordinator, accepted["session"]["migration_id"])
    assert completed["session"]["state"] == "completed_attention"
    with ProjectRepository(service_dir) as repository:
        project = repository.list_projects()[0]
        runtime = repository.get_runtime(project.project_id)
        assert project.activation_state == "inactive"
        assert runtime.readiness_state == "blocked"
        assert len(repository.list_issues(project_id=project.project_id, active_only=True)) == 1


def test_safe_result_is_hash_verified_and_written_with_result_identities(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg unavailable")
    coordinator, service = _legacy_home(tmp_path)
    output = tmp_path / "output" / "legacy.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=15:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        capture_output=True,
        check=True,
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    runs_path = service / "runs.json"
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    payload["runs"][0]["result_path"] = str(output)
    payload["runs"][0]["result_sha256"] = digest
    runs_path.write_text(json.dumps(payload), encoding="utf-8")
    plan = _validated(coordinator)
    accepted = coordinator.execute(
        {
            "request_id": "request-safe-result",
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": plan["choices"],
        }
    )[1]
    completed = _wait_completed(coordinator, accepted["session"]["migration_id"])
    assert completed["report"]["safe_results"] == 1
    with ProjectRepository(service) as repository:
        run = repository.list_runs()[0]
        result = repository.get_run_result(run.run_id)
        outputs = repository.list_run_outputs(run.run_id)
        assert result is not None and result.source_kind == "indexed_v1"
        assert len(outputs) == 1 and outputs[0].status == "ready"
        assert repository.get_output_material(outputs[0].output_id) is not None
        evidence = (
            service.parent
            / "projects"
            / run.project_id
            / "runs"
            / run.run_id
            / "outputs"
            / outputs[0].output_id
            / "media_integrity.json"
        )
        assert json.loads(evidence.read_text(encoding="utf-8"))["sha256"] == digest


@pytest.mark.parametrize(
    "fault_phase",
    ["after_project", "after_runs", "after_results", "after_issues", "before_mode", "after_mode"],
)
def test_fault_rolls_back_all_business_facts_and_retry_reuses_backup(tmp_path, fault_phase):
    coordinator, service = _legacy_home(tmp_path)
    plan = _validated(coordinator)
    coordinator.fault_injection = (
        lambda phase: (_ for _ in ()).throw(RuntimeError("fault")) if phase == fault_phase else None
    )
    accepted = coordinator.execute(
        {
            "request_id": "request-fault",
            "source_fingerprint": plan["source_fingerprint"],
            "plan_hash": plan["plan_hash"],
            "choices": plan["choices"],
        }
    )[1]
    failed = _wait_completed(coordinator, accepted["session"]["migration_id"])
    assert failed["session"]["state"] == "failed_rolled_back"
    with ProjectRepository(service) as repository:
        assert repository.get_data_mode() == "legacy"
        assert repository.list_projects() == []
        assert repository.list_runs() == []
    coordinator.fault_injection = None
    retried = coordinator.retry(
        {
            "request_id": "retry-1",
            "migration_id": accepted["session"]["migration_id"],
            "expected_revision": failed["session"]["revision"],
        }
    )[1]
    completed = _wait_completed(coordinator, retried["session"]["migration_id"])
    assert completed["session"]["state"] == "completed_ready"
