from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_resources import compatibility_resources, resolve_parameter_snapshot


def test_compatibility_resources_and_snapshot_never_expose_credentials(tmp_path):
    settings = Settings(cheap_model_api_key="sk-never-return", asr_api_key="asr-never-return")
    resources = compatibility_resources(settings)
    assert {item.resource_id for item in resources} >= {"legacy.asr.default", "legacy.analysis.default"}
    assert all("sk-never-return" not in repr(item) for item in resources)
    config = default_project_config(tmp_path / "source", tmp_path / "output")
    snapshot = resolve_parameter_snapshot(config, settings)
    assert snapshot["resources"]["asr_ref"] == "legacy.asr.default"
    assert "sk-never-return" not in repr(snapshot)
    assert "asr-never-return" not in repr(snapshot)
