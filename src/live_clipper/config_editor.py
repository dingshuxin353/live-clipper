from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_TEMPLATE, load_settings
from .utils import ensure_dir


def ensure_workspace_root(
    *,
    config_path: Path,
    workspace_root: Path,
    backup_root: Path,
) -> dict[str, Any]:
    """Add the App workspace field without validating or rewriting other settings."""
    raw_result = _load_raw_config(config_path)
    if not raw_result["ok"]:
        return {"migrated": False, **raw_result}

    raw = raw_result["config"]
    paths = raw.get("paths")
    existing = paths.get("workspace_root") if isinstance(paths, dict) else None
    if existing:
        resolved = Path(str(existing)).expanduser()
        (resolved / "runs").mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "migrated": False,
            "workspace_root": str(resolved),
            "backup_path": None,
        }

    resolved = workspace_root.expanduser().resolve()
    merged = deepcopy(raw)
    merged.setdefault("paths", {})
    merged["paths"]["workspace_root"] = str(resolved)
    rendered = _dump_toml(merged)
    original_text = config_path.read_text(encoding="utf-8")
    backup_path: Path | None = None
    temp_name: str | None = None
    replaced = False
    try:
        backup_path = _backup_config(config_path, backup_root)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(config_path.parent),
            prefix=f".{config_path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(rendered)
        Path(temp_name).replace(config_path)
        replaced = True
        loaded = load_settings(config_path)
        if loaded.paths.workspace_root != resolved:
            raise ValueError("任务工作区配置回读不一致")
        (resolved / "runs").mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 - preserve the exact prior App config.
        if replaced:
            config_path.write_text(original_text, encoding="utf-8")
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink(missing_ok=True)
        return {
            "ok": False,
            "migrated": False,
            "message": f"任务工作区迁移失败，已保留旧配置：{exc}",
            "error": str(exc),
        }
    return {
        "ok": True,
        "migrated": True,
        "workspace_root": str(resolved),
        "backup_path": str(backup_path),
    }


def _load_raw_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {"ok": True, "config": tomllib.loads(DEFAULT_CONFIG_TEMPLATE)}
    try:
        return {"ok": True, "config": tomllib.loads(config_path.read_text(encoding="utf-8"))}
    except tomllib.TOMLDecodeError as exc:
        return {
            "ok": False,
            "config_path": str(config_path),
            "exists": True,
            "message": f"配置文件解析失败，请先修复 TOML 格式后再保存：{exc}",
            "error": str(exc),
        }


def _backup_config(config_path: Path, backup_root: Path) -> Path:
    ensure_dir(backup_root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_root / f"live-clipper.{stamp}.toml"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_root / f"live-clipper.{stamp}.{suffix}.toml"
        suffix += 1
    shutil.copy2(config_path, backup_path)
    return backup_path


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = [
        "# live-clipper configuration",
        "# Application settings. Model resources and their credentials are managed separately.",
        "",
    ]
    _write_table(lines, [], data)
    return "\n".join(lines).rstrip() + "\n"


def _write_table(lines: list[str], prefix: list[str], table: dict[str, Any]) -> None:
    scalars = {key: value for key, value in table.items() if not isinstance(value, dict)}
    subtables = {key: value for key, value in table.items() if isinstance(value, dict)}
    if prefix and scalars:
        lines.append(f"[{'.'.join(prefix)}]")
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    if prefix and scalars:
        lines.append("")
    for key, value in subtables.items():
        _write_table(lines, [*prefix, str(key)], value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def application_config(config_path: Path) -> dict[str, Any]:
    """Expose runtime storage only; reading this endpoint never writes configuration."""
    try:
        settings = load_settings(config_path)
        return {"ok": True, "storage": {"work_dir": str(settings.paths.work_dir.resolve())}}
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "message": "数据位置未获取，请检查服务后重试"}
