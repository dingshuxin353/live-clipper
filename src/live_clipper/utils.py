"""Shared utility helpers."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def self_command(*args: str) -> list[str]:
    """Build a command that re-invokes this program with *args*.

    In a normal environment this is ``python -m live_clipper ...``. In a
    PyInstaller-frozen binary ``sys.executable`` is the app binary itself,
    which accepts CLI subcommands directly and does not understand ``-m``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "live_clipper", *args]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_required_text(path: Path, description: str) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: Any) -> Path:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_failure_log(prefix: str, data: Any) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return write_json(Path("work") / "logs" / f"{prefix}_{timestamp}.json", data)
