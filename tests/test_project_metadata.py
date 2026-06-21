from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_dependencies_include_socks_proxy_support():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = metadata["project"]["dependencies"]

    assert any(dependency.startswith("socksio") for dependency in dependencies)
