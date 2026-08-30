from __future__ import annotations

from pathlib import Path

import pytest

from live_clipper import asr_models, onboarding_resources, service
from live_clipper.config import Settings
from live_clipper.onboarding_coordinator import OnboardingCoordinator, OnboardingError


def _coordinator(tmp_path: Path, *, settings: Settings | None = None) -> tuple[OnboardingCoordinator, Path]:
    source = tmp_path / "recordings"
    source.mkdir()
    configured = settings or Settings(cheap_model_api_key="ai-secret", cheap_model_name="ai-model", asr_model="local-model")
    return (
        OnboardingCoordinator(
            service_dir=tmp_path / "work" / "service",
            config_path=tmp_path / "live-clipper.toml",
            env_path=tmp_path / ".env",
            input_dir=tmp_path / "input",
            output_root=tmp_path / "output",
            settings_loader=lambda: configured,
        ),
        source,
    )


def _project_patch(source: Path, output: Path) -> dict[str, object]:
    return {
        "name": "首项目",
        "source_directory": str(source),
        "output_directory": str(output),
        "trigger_mode": "manual",
    }


def test_unknown_fields_are_rejected_before_session_mutation(tmp_path: Path) -> None:
    coordinator, _source = _coordinator(tmp_path)
    with pytest.raises(OnboardingError) as error:
        coordinator.start({"unexpected": True})
    assert error.value.code == "validation_failed"


def test_project_draft_only_accepts_m1_fields_and_builds_v2(tmp_path: Path) -> None:
    coordinator, source = _coordinator(tmp_path)
    coordinator.start({})
    _status, patched = coordinator.patch_session(
        {
            "request_id": "draft",
            "expected_revision": 1,
            "current_step": "project",
            "patch": {"project": _project_patch(source, tmp_path / "output")},
        }
    )
    assert patched["session"]["revision"] == 2
    with pytest.raises(OnboardingError) as error:
        coordinator.patch_session(
            {
                "request_id": "advanced",
                "expected_revision": 2,
                "current_step": "project",
                "patch": {"project": {"review_strategy": "manual"}},
            }
        )
    assert error.value.code == "validation_failed"
    name, config, _raw = coordinator._project_from_draft(patched["session"]["draft"])
    assert name == "首项目"
    assert config["schema_version"] == 2
    assert config["processing"]["review_strategy"] == "ai_auto"
    assert config["resources"]["review_ref"] == config["resources"]["analysis_ref"]


def test_finish_failure_keeps_single_project_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, source = _coordinator(tmp_path)
    monkeypatch.setattr(asr_models, "local_path_for", lambda _model: tmp_path / "installed")
    coordinator.start({})
    _status, patched = coordinator.patch_session(
        {
            "request_id": "draft",
            "expected_revision": 1,
            "current_step": "project",
            "patch": {"project": _project_patch(source, tmp_path / "output")},
        }
    )
    monkeypatch.setattr(onboarding_resources, "test_ai_service", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": False, "error_code": "service_not_ready", "message": "未启动"})
    status, pending = coordinator.finish({"request_id": "finish", "expected_revision": patched["session"]["revision"]})
    assert status == 202
    assert pending["session"]["state"] == "activation_pending"
    project_id = pending["project"]["project_id"]
    assert coordinator.snapshot()["session"]["first_project"]["project_id"] == project_id
    monkeypatch.setattr(service, "ensure_service_ready", lambda *args, **kwargs: {"ok": True, "ready": True})
    status, completed = coordinator.retry({"request_id": "finish", "expected_revision": pending["session"]["revision"]})
    assert status == 200
    assert completed["session"]["state"] == "completed"
    assert completed["session"]["first_project"]["project_id"] == project_id
