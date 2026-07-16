"""App-mode home directory resolution and preparation.

App mode is the packaged desktop usage. All relative paths in this codebase
are resolved against the process cwd, so app mode simply chdirs into the app
home before doing anything else. CLI usage in a project directory never calls
prepare_app_home and is unaffected.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "Venus"
HOME_ENV_VAR = "LIVE_CLIPPER_HOME"

WORK_SUBDIRS = [
    Path("input"),
    Path("work") / "cache",
    Path("work") / "logs",
    Path("work") / "automation_state",
    Path("work") / "automation_logs",
    Path("work") / "service",
]


def default_app_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".live-clipper"


def default_output_root(home: Path) -> Path:
    # With an explicit home override (tests, portable setups), keep output
    # inside the home so nothing leaks into the real user profile.
    if os.environ.get(HOME_ENV_VAR, "").strip():
        return home / "output"
    if sys.platform == "darwin":
        return Path.home() / "Movies" / APP_DIR_NAME
    return home / "output"


def prepare_app_home(home: Path | None = None) -> Path:
    home = home or default_app_home()
    for subdir in WORK_SUBDIRS:
        (home / subdir).mkdir(parents=True, exist_ok=True)
    default_output_root(home).mkdir(parents=True, exist_ok=True)
    return home
