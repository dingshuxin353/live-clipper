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
    assert package["dependencies"] == {
        "@astryxdesign/core": "0.1.9",
        "@astryxdesign/theme-stone": "0.1.9",
        "@stylexjs/stylex": "0.19.0",
        "react": "19.2.8",
        "react-dom": "19.2.8",
    }
    assert package["scripts"]["theme:build"] == "node scripts/build-venus-stone-overrides.mjs"
    assert package["scripts"]["build"].startswith("npm run theme:build")
    assert lock["lockfileVersion"] == 3
    for dependency, version in package["dependencies"].items():
        assert lock["packages"][""]["dependencies"][dependency] == version
    assert "npm --prefix frontend ci" in build_script
    assert "npm --prefix frontend run check" in build_script
    assert "git diff --exit-code -- src/live_clipper/web_static/react" in build_script
    assert build_script.index("npm --prefix frontend ci") < build_script.index(".venv/bin/pyinstaller")


def test_astryx_stone_theme_is_generated_offline_and_not_recompiled():
    package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))
    main = Path("frontend/src/main.tsx").read_text(encoding="utf-8")
    script = Path("frontend/scripts/build-venus-stone-overrides.mjs").read_text(
        encoding="utf-8"
    )
    generated = Path("frontend/src/theme/venus-stone-overrides.css").read_text(
        encoding="utf-8"
    )
    notice = Path("THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    imports = [
        '@astryxdesign/core/reset.css"',
        '@astryxdesign/core/astryx.css"',
        '@astryxdesign/theme-stone/theme.css"',
        './theme/venus-stone-overrides.css"',
        './styles.css"',
    ]
    assert [main.index(item) for item in imports] == sorted(main.index(item) for item in imports)
    assert 'stoneTheme } from "@astryxdesign/theme-stone/built"' in main
    assert '<Theme theme={stoneTheme} mode="light">' in main
    assert 'from "@astryxdesign/core/theme"' in script
    assert 'accent: "#4A3A72"' in script
    assert "defineTheme" not in script
    assert generated.count('[data-astryx-theme="stone"]') == 1
    assert len(re.findall(r"^\s+--color-[a-z-]+:", generated, flags=re.MULTILINE)) == 5
    assert len(re.findall(r"^\s+--font-[a-z-]+:", generated, flags=re.MULTILINE)) == 3
    assert "http://" not in generated and "https://" not in generated
    for dependency, version in package["dependencies"].items():
        assert dependency in notice or dependency in {"react", "react-dom"}
        assert version not in {"latest", "next", "canary"}


def test_project_pins_lightweight_model_hub_dependencies_and_freezes_them():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    build_script = Path("desktop/scripts/build-backend.sh").read_text(encoding="utf-8")

    assert "huggingface-hub==1.24.0" in dependencies
    assert "modelscope-hub==0.1.8" in dependencies
    assert not any(dependency.split("=", 1)[0] == "modelscope" for dependency in dependencies)
    assert "--collect-all huggingface_hub" in build_script
    assert "--collect-all modelscope_hub" in build_script


def test_release_versions_are_frozen_at_0_3_4():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    desktop_package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    desktop_lock = json.loads(Path("desktop/package-lock.json").read_text(encoding="utf-8"))
    frontend_package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))
    frontend_lock = json.loads(Path("frontend/package-lock.json").read_text(encoding="utf-8"))

    release_versions = {
        pyproject["project"]["version"],
        desktop_package["version"],
        desktop_lock["version"],
        desktop_lock["packages"][""]["version"],
        frontend_package["version"],
        frontend_lock["version"],
        frontend_lock["packages"][""]["version"],
    }

    assert release_versions == {"0.3.4"}
    assert "## 0.3.4 - 2026-08-09" in Path("CHANGELOG.md").read_text(encoding="utf-8")


def test_electron_runtime_is_supported_secure_release():
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    lock = json.loads(Path("desktop/package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]

    assert package["version"] == lock["version"] == root["version"] == "0.3.4"
    assert package["scripts"]["postinstall"] == "install-electron"
    assert package["dependencies"] == {"electron-updater": "^6.3.0"}
    assert package["devDependencies"] == {
        "electron": "43.2.0",
        "electron-builder": "^26.0.0",
        "ffmpeg-static": "^5.2.0",
    }
    assert root["dependencies"] == package["dependencies"]
    assert root["devDependencies"] == package["devDependencies"]
    assert lock["packages"]["node_modules/electron"]["version"] == "43.2.0"
    assert lock["packages"]["node_modules/electron-builder"]["version"] == "26.15.3"
    assert lock["packages"]["node_modules/electron-updater"]["version"] == "6.8.9"
    assert lock["packages"]["node_modules/ffmpeg-static"]["version"] == "5.3.0"
    assert not any(
        marker in package["devDependencies"]["electron"].lower()
        for marker in ("alpha", "beta", "nightly")
    )


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


def test_ci_workflow_checks_current_desktop_entrypoints_with_node_24():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'node-version: "24"' in workflow
    assert "node --check desktop/main.js" in workflow
    assert "node --check desktop/preload.js" in workflow
    assert "src/live_clipper/web_static/onboarding.js" not in workflow


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
    assert 'node-version: "24"' in workflow
    assert '.venv/bin/pip install ".[mlx]" "pyinstaller>=6.10"' in workflow
    assert ".venv/bin/python scripts/ci/assert_backend_bundle.py" in workflow
    assert "DEBUG: electron-notarize:*" in workflow
    assert "gh release view" in workflow
    assert workflow.index("npm run build:backend") < workflow.index("scripts/ci/assert_backend_bundle.py")
    assert workflow.index("scripts/ci/assert_backend_bundle.py") < workflow.index("npx electron-builder --mac --publish always")
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


def test_local_release_requires_backend_bundle_contract_before_publish():
    script = Path("desktop/scripts/release-local.sh").read_text(encoding="utf-8")

    assert "../.venv/bin/python ../scripts/ci/assert_backend_bundle.py" in script
    assert script.index("npm run build:backend") < script.index("assert_backend_bundle.py")
    assert script.index("assert_backend_bundle.py") < script.index("npx electron-builder --mac --publish always")
