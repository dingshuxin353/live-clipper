from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


def _parse_flat_yaml_mapping(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        mapping[key.strip()] = value.strip()
    return mapping


def _yaml_mapping_section(text: str, section_name: str) -> dict[str, str]:
    lines = text.splitlines()
    start = lines.index(f"{section_name}:") + 1
    section: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith((" ", "\t")):
            break
        if line.strip():
            section.append(line)
    return _parse_flat_yaml_mapping("\n".join(section))


def test_desktop_updater_provider_config_is_complete_and_secret_free():
    update_path = Path("desktop/build/app-update.yml")
    update_text = update_path.read_text(encoding="utf-8")
    update_config = _parse_flat_yaml_mapping(update_text)
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))

    assert update_config == {
        "provider": "github",
        "owner": "dingshuxin353",
        "repo": "live-clipper",
        "updaterCacheDirName": "live-clipper-desktop-updater",
    }
    assert update_text.endswith("\n")
    assert "\r" not in update_text
    assert update_config["updaterCacheDirName"] == f"{package['name']}-updater"
    lowered = update_text.lower()
    for forbidden in ("token", "secret", "password", "private", "${", "$(", "process.env"):
        assert forbidden not in lowered


def test_builder_places_updater_config_at_resources_root_and_matches_publish():
    builder_text = Path("desktop/electron-builder.yml").read_text(encoding="utf-8")
    update_config = _parse_flat_yaml_mapping(
        Path("desktop/build/app-update.yml").read_text(encoding="utf-8")
    )
    publish = _yaml_mapping_section(builder_text, "publish")

    extra_resources = builder_text.split("extraResources:", 1)[1].split("afterPack:", 1)[0]
    assert re.search(
        r"(?m)^  - from: build/app-update\.yml$\n^    to: app-update\.yml$",
        extra_resources,
    )
    assert "to: build/app-update.yml" not in extra_resources
    assert "to: assets/app-update.yml" not in extra_resources
    assert "to: app.asar/app-update.yml" not in extra_resources
    assert {
        key: publish[key]
        for key in ("provider", "owner", "repo")
    } == {
        key: update_config[key]
        for key in ("provider", "owner", "repo")
    }


def test_project_dependencies_include_socks_proxy_support():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = metadata["project"]["dependencies"]

    assert any(dependency.startswith("socksio") for dependency in dependencies)


def test_react_renderer_package_and_frozen_build_contract():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package_data = metadata["tool"]["setuptools"]["package-data"]["live_clipper"]
    build_script = Path("desktop/scripts/build-backend.sh").read_text(encoding="utf-8")
    package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads(Path("frontend/package-lock.json").read_text(encoding="utf-8"))

    assert {"web_static/react/*.html", "web_static/react/assets/*"}.issubset(package_data)
    assert package["engines"] == {"node": ">=24 <25", "npm": ">=11 <12"}
    assert set(package["dependencies"]) == {"react", "react-dom"}
    assert lock["lockfileVersion"] == 3
    assert "npm --prefix frontend ci" in build_script
    assert "npm --prefix frontend run check" in build_script
    assert "git diff --exit-code -- src/live_clipper/web_static/react" in build_script
    assert build_script.index("npm --prefix frontend ci") < build_script.index(".venv/bin/pyinstaller")


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


def test_automatic_tag_release_workflow_is_disabled():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "permissions: {}" in workflow
    assert "Automatic tag releases are disabled." in workflow
    for forbidden in (
        "push:",
        "pull_request:",
        "release:",
        "schedule:",
        "repository_dispatch:",
        "actions/checkout",
        "electron-builder",
        "notarytool",
        "gh release",
        "--publish",
        "APPLE_",
        "CSC_",
        "GH_TOKEN",
        "secrets.",
    ):
        assert forbidden not in workflow


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
