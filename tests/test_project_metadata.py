from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_dependencies_include_socks_proxy_support():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = metadata["project"]["dependencies"]

    assert any(dependency.startswith("socksio") for dependency in dependencies)


def test_desktop_version_matches_python_version():
    import json
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    package_lock = json.loads(Path("desktop/package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == pyproject["project"]["version"]
    assert package_lock["packages"][""]["version"] == package["version"]
