from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_dependencies_include_socks_proxy_support():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = metadata["project"]["dependencies"]

    assert any(dependency.startswith("socksio") for dependency in dependencies)


def test_project_pins_lightweight_model_hub_dependencies_and_freezes_them():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    build_script = Path("desktop/scripts/build-backend.sh").read_text(encoding="utf-8")

    assert "huggingface-hub==1.24.0" in dependencies
    assert "modelscope-hub==0.1.8" in dependencies
    assert not any(dependency.split("=", 1)[0] == "modelscope" for dependency in dependencies)
    assert "--collect-all huggingface_hub" in build_script
    assert "--collect-all modelscope_hub" in build_script


def test_desktop_version_matches_python_version():
    import json
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    package_lock = json.loads(Path("desktop/package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == pyproject["project"]["version"]
    assert package_lock["packages"][""]["version"] == package["version"]


def test_release_recovery_workflow_contract():
    import re

    workflow = Path(".github/workflows/release-recovery.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "release_ref:" in workflow
    assert "timeout-minutes: 360" in workflow
    assert "ref: ${{ inputs.release_ref }}" in workflow
    assert "notarytool history" in workflow
    assert "In Progress" in workflow
    assert "do not create a duplicate" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert '["git", "describe", "--tags", "--exact-match"]' in workflow
    assert "pyproject.toml" in workflow
    assert "desktop/package.json" in workflow
    assert "DEBUG: electron-notarize:*" in workflow
    assert "gh release view" in workflow
    for suffix in ("-arm64.dmg", "-arm64-mac.zip", "-arm64-mac.zip.blockmap", "latest-mac.yml"):
        assert suffix in workflow

    assert re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", workflow) is None
    for secret_name in (
        "CSC_LINK",
        "CSC_KEY_PASSWORD",
        "APPLE_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
        "APPLE_TEAM_ID",
    ):
        assert f"{secret_name}: ${{{{ secrets.{secret_name} }}}}" in workflow
