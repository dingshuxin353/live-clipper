from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import live_clipper.project_migration as migration
from live_clipper.project_migration import LegacySourceError, build_migration_plan, inspect_legacy_state


def _tree(tmp_path: Path, *, weekly: bool = True):
    service = tmp_path / "service"
    source = tmp_path / "recordings"
    output = tmp_path / "clips"
    service.mkdir()
    source.mkdir()
    output.mkdir()
    config = tmp_path / "live-clipper.toml"
    weekly_job = (
        '[[scheduler.jobs]]\ntype = "scan_recordings"\nschedule = "weekly"\n' if weekly else ""
    )
    config.write_text(
        f'''[recording_source.default]
source_dir = "{source}"
output_root = "{output}"
[scheduler]
timezone = "Asia/Tokyo"
{weekly_job}[llm]
model = "safe-model"
api_key = "sk-sentinel-secret"
''',
        encoding="utf-8",
    )
    (service / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "run_id": "old-1",
                        "content_id": "content-1",
                        "source_path": str(source / "a.mp4"),
                        "phase": "rendered",
                        "created_at": "2026-08-01T00:00:00Z",
                    },
                    {"run_id": "missing", "source_path": str(source / "missing.mp4"), "phase": "failed"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (service / "service.json").write_text('{"status":"stopped"}', encoding="utf-8")
    return config, service, source, output


def _snapshot(root: Path):
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            stat_mode(path),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def stat_mode(path: Path) -> int:
    return path.stat().st_mode


def test_manifest_plan_and_hash_are_deterministic_secret_free_and_read_only(tmp_path):
    config, service, _source, _output = _tree(tmp_path)
    before = _snapshot(tmp_path)
    first = inspect_legacy_state(service, config_path=config)
    second = inspect_legacy_state(service, config_path=config)
    plan = build_migration_plan(first, available_bytes=10**9)

    assert first.source_manifest == second.source_manifest
    assert first.source_fingerprint == second.source_fingerprint
    assert plan.plan_version == 3 and len(plan.plan_hash) == 64
    assert plan == build_migration_plan(second, available_bytes=10**9)
    assert {item.source_identity for item in plan.source_manifest} == {
        "config/live-clipper.toml",
        "service/runs.json",
        "service/service.json",
    }
    serialized = plan.stable_json() + repr(plan) + repr(first)
    assert "sk-sentinel-secret" not in serialized
    assert not (service / "venus.sqlite3").exists()
    assert not (tmp_path / "migration-backups").exists()
    assert _snapshot(tmp_path) == before
    with pytest.raises(TypeError):
        plan.project_preview["name"] = "mutated"


def test_choices_are_whitelisted_normalized_and_change_plan_hash(tmp_path):
    config, service, source, output = _tree(tmp_path)
    inspected = inspect_legacy_state(service, config_path=config)
    default = build_migration_plan(inspected, available_bytes=10**9)
    assert default.project_preview["trigger_mode"] == "manual"
    assert default.project_preview["schedule_mode"] is None
    assert default.project_preview["daily_time"] is None
    assert "trigger_mode" in default.requires_user_choices

    daily = build_migration_plan(
        inspected,
        choices={
            "project_name": "迁移项目",
            "source_directory": str(source),
            "output_directory": str(output),
            "trigger_mode": "scheduled",
            "schedule_mode": "daily",
            "daily_time": "09:30",
            "interval_minutes": 60,
        },
        available_bytes=10**9,
    )
    assert daily.project_preview["daily_time"] == "09:30"
    assert daily.project_preview["interval_minutes"] is None
    assert daily.plan_hash != default.plan_hash
    interval = build_migration_plan(
        inspected,
        choices={"trigger_mode": "scheduled", "schedule_mode": "interval", "interval_minutes": 60},
        available_bytes=10**9,
    )
    assert interval.project_preview["daily_time"] is None
    with pytest.raises(ValueError, match="unsupported migration choice"):
        build_migration_plan(inspected, choices={"project_config": {}}, available_bytes=10**9)
    with pytest.raises(ValueError, match="interval_minutes"):
        build_migration_plan(
            inspected,
            choices={"trigger_mode": "scheduled", "schedule_mode": "interval", "interval_minutes": 61},
            available_bytes=10**9,
        )


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("toml", "legacy_config_invalid"),
        ("json", "legacy_metadata_invalid"),
        ("permission", "legacy_source_unreadable"),
        ("oversize", "legacy_source_too_large"),
        ("symlink", "legacy_source_unsafe"),
    ],
)
def test_source_rejections_are_stable_and_do_not_scan_arbitrary_files(tmp_path, kind, code, monkeypatch):
    config, service, _source, _output = _tree(tmp_path)
    target = service / "runs.json"
    if kind == "toml":
        config.write_text("[[invalid", encoding="utf-8")
    elif kind == "json":
        target.write_text("{invalid", encoding="utf-8")
    elif kind == "permission":
        target.chmod(0)
    elif kind == "oversize":
        monkeypatch.setattr(migration, "MAX_SOURCE_FILE_BYTES", 3)
    else:
        target.unlink()
        target.symlink_to(tmp_path / "outside.json")
        (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    (service / "unknown-secret.txt").write_text("DO-NOT-READ", encoding="utf-8")
    with pytest.raises(LegacySourceError) as error:
        inspect_legacy_state(service, config_path=config)
    assert error.value.code == code
    assert "DO-NOT-READ" not in str(error.value)


def test_source_change_during_fd_read_is_rejected(tmp_path, monkeypatch):
    config, service, _source, _output = _tree(tmp_path)
    target = service / "runs.json"
    original_read = os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        result = original_read(descriptor, size)
        if result and not changed and os.fstat(descriptor).st_ino == target.stat().st_ino:
            changed = True
            target.write_bytes(target.read_bytes() + b" ")
        return result

    monkeypatch.setattr(migration.os, "read", changing_read)
    with pytest.raises(LegacySourceError) as error:
        inspect_legacy_state(service, config_path=config)
    assert error.value.code == "legacy_source_changed"


def test_backup_space_and_resource_readiness_are_facts_not_side_effects(tmp_path):
    config, service, _source, _output = _tree(tmp_path, weekly=False)
    inspected = inspect_legacy_state(service, config_path=config)
    insufficient = build_migration_plan(inspected, available_bytes=0)
    enough = build_migration_plan(inspected, available_bytes=10**9)
    assert insufficient.backup_summary["source_bytes"] == sum(item.size for item in inspected.source_manifest)
    assert insufficient.backup_summary["space_status"] == "insufficient"
    assert not insufficient.readiness_summary["can_start"]
    assert enough.backup_summary["space_status"] == "ready"
    assert set(enough.resource_summary) == {"asr", "ai"}
    assert enough.resource_summary["ai"]["credential_present"] is True
    assert enough.resource_summary["asr"]["status"] == "problem"
    assert enough.plan_hash != insufficient.plan_hash
    assert hashlib.sha256(enough.stable_json().encode()).hexdigest()
