from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_TEMPLATE, load_settings
from .utils import ensure_dir

EditableConfig = dict[str, dict[str, Any]]

PATH_FIELDS = {
    "paths": {"input_dir", "output_root", "work_dir", "glossary_path"},
    "recording_source_default": {"source_dir", "input_dir", "output_root"},
}
NUMBER_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("recording_source_default", "since_hours"): (1, 720),
    ("recording_source_default", "min_age_minutes"): (1, 1440),
    ("recording_source_default", "stable_check_seconds"): (5, 600),
    ("llm", "timeout_seconds"): (30, 3600),
    ("llm", "request_attempts"): (1, 10),
    ("llm", "retry_delay_seconds"): (0, 60),
    ("service", "scan_interval_minutes"): (1, 1440),
    ("web", "port"): (1024, 65535),
}
INTEGER_FIELDS = {
    ("recording_source_default", "since_hours"),
    ("recording_source_default", "min_age_minutes"),
    ("recording_source_default", "stable_check_seconds"),
    ("llm", "timeout_seconds"),
    ("llm", "request_attempts"),
    ("service", "scan_interval_minutes"),
    ("web", "port"),
}
BOOLEAN_FIELDS = {
    ("service", "enabled"),
    ("service", "auto_render_after_selection"),
}
ENV_VAR_FIELDS = {
    ("llm", "api_key_env"),
    ("asr", "api_key_env"),
    ("asr", "hf_token_env"),
}
SERVICE_RESTART_SECTIONS = {"paths", "recording_source_default", "llm", "asr", "service"}
ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def load_editable_config(*, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    raw_result = _load_raw_config(config_path)
    if not raw_result["ok"]:
        return raw_result
    raw = raw_result["config"]
    config = _editable_from_raw(raw)
    return {
        "ok": True,
        "config_path": str(config_path),
        "exists": config_path.exists(),
        "loaded_at": _timestamp(),
        "config": config,
        "env_status": _env_status(config),
        "warnings": _warnings_for(config, base_dir=config_path.parent),
    }


def validate_editable_config(
    draft: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    base_dir = base_dir or config_path.parent or Path.cwd()
    clean = _clean_editable(draft)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    _validate_paths(clean, base_dir, errors, warnings)
    _validate_numbers(clean, errors)
    _validate_enums(clean, errors)
    _validate_env_vars(clean, errors)

    current_result = _load_raw_config(config_path)
    current = _editable_from_raw(current_result["config"]) if current_result["ok"] else _editable_from_raw({})
    changes = _diff_editable(current, clean)
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "changes": changes,
        "requires_service_restart": _changes_require_service_restart(changes),
        "requires_web_restart": _changes_require_web_restart(changes),
        "env_status": _env_status(clean),
    }


def save_editable_config(
    draft: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    backup_root: Path = Path("work") / "config_backups",
    base_dir: Path | None = None,
) -> dict[str, Any]:
    raw_result = _load_raw_config(config_path)
    if not raw_result["ok"]:
        return raw_result

    validation = validate_editable_config(draft, config_path=config_path, base_dir=base_dir)
    if not validation["ok"]:
        return {"ok": False, "saved": False, **validation}

    merged = _merge_editable(raw_result["config"], _clean_editable(draft))
    rendered = _dump_toml(merged)
    backup_path: Path | None = None
    original_text = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    if config_path.exists():
        backup_path = _backup_config(config_path, backup_root)

    ensure_dir(config_path.parent)
    temp_name: str | None = None
    try:
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
        load_settings(config_path)
    except Exception as exc:  # noqa: BLE001 - restore config and return a user-facing error.
        if original_text is not None:
            config_path.write_text(original_text, encoding="utf-8")
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink(missing_ok=True)
        return {
            "ok": False,
            "saved": False,
            "message": f"配置写入后无法读取，已保留旧配置：{exc}",
            "error": str(exc),
        }

    return {
        "ok": True,
        "saved": True,
        "config_path": str(config_path),
        "backup_path": str(backup_path) if backup_path else None,
        "changes": validation["changes"],
        "requires_service_restart": validation["requires_service_restart"],
        "requires_web_restart": validation["requires_web_restart"],
        "message": _saved_message(validation),
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


def _editable_from_raw(raw: dict[str, Any]) -> EditableConfig:
    recording_source = raw.get("recording_source", {})
    recording_default = recording_source.get("default", {}) if isinstance(recording_source, dict) else {}
    web = raw.get("web", {}) if isinstance(raw.get("web"), dict) else {}
    return {
        "paths": _pick(raw.get("paths", {}), ["input_dir", "output_root", "work_dir", "glossary_path"]),
        "recording_source_default": _pick(
            recording_default,
            ["source_dir", "input_dir", "output_root", "since_hours", "min_age_minutes", "stable_check_seconds"],
        ),
        "llm": _pick(
            raw.get("llm", {}),
            ["provider_label", "api_base", "api_key_env", "model", "timeout_seconds", "request_attempts", "retry_delay_seconds"],
        ),
        "asr": _pick(raw.get("asr", {}), ["backend", "model", "language", "api_base", "api_key_env", "hf_token_env"]),
        "service": _pick(raw.get("service", {}), ["enabled", "scan_interval_minutes", "auto_render_after_selection", "cleanup_mode"]),
        "web": {
            **_pick(web, ["host", "port"]),
            "access_token_configured": bool(web.get("access_token")),
        },
    }


def _pick(value: Any, keys: list[str]) -> dict[str, Any]:
    mapping = value if isinstance(value, dict) else {}
    return {key: _jsonable(mapping[key]) for key in keys if key in mapping}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def _clean_editable(draft: dict[str, Any]) -> EditableConfig:
    clean = _editable_from_raw(tomllib.loads(DEFAULT_CONFIG_TEMPLATE))
    for section, fields in draft.items():
        if section not in clean or not isinstance(fields, dict):
            continue
        for key, value in fields.items():
            if key == "access_token_configured":
                continue
            if key in clean[section] or (section, key) in NUMBER_RANGES or (section, key) in BOOLEAN_FIELDS:
                clean[section][key] = value
    return clean


def _validate_paths(
    config: EditableConfig,
    base_dir: Path,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    source_value = config["recording_source_default"].get("source_dir")
    source_path = _resolve_path(source_value, base_dir) if source_value else None
    if source_path and (not source_path.exists() or not source_path.is_dir()):
        errors.append(_error("recording_source_default.source_dir", "录播源目录不存在，请确认 NAS 已挂载，或重新选择目录。"))

    input_path = _resolve_path(config["recording_source_default"].get("input_dir"), base_dir)
    output_path = _resolve_path(config["recording_source_default"].get("output_root"), base_dir)
    base_input = _resolve_path(config["paths"].get("input_dir"), base_dir)
    base_output = _resolve_path(config["paths"].get("output_root"), base_dir)

    for field, path in [
        ("recording_source_default.input_dir", input_path),
        ("recording_source_default.output_root", output_path),
        ("paths.input_dir", base_input),
        ("paths.output_root", base_output),
    ]:
        if not path.exists():
            warnings.append({"field": field, "message": f"{field} 指向的目录不存在，保存后运行时会按需创建。"})

    if input_path == output_path or base_input == base_output:
        errors.append(_error("paths.input_dir", "输入目录和输出目录不能相同，请选择两个不同目录。"))
    if source_path:
        for field, path in [
            ("recording_source_default.input_dir", input_path),
            ("recording_source_default.output_root", output_path),
            ("paths.input_dir", base_input),
            ("paths.output_root", base_output),
        ]:
            if _is_relative_to(path, source_path):
                errors.append(_error(field, "输入/输出目录不能放在录播源目录内部，避免把 NAS 原始录播当成本地工作目录。"))


def _validate_numbers(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    for (section, key), (minimum, maximum) in NUMBER_RANGES.items():
        raw = config.get(section, {}).get(key)
        field = f"{section}.{key}"
        if raw == "" or raw is None:
            errors.append(_error(field, f"{field} 必须填写数字。"))
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            errors.append(_error(field, f"{field} 必须填写数字。"))
            continue
        if (section, key) in INTEGER_FIELDS and not number.is_integer():
            errors.append(_error(field, f"{field} 必须填写整数。"))
            continue
        if number < minimum or number > maximum:
            errors.append(_error(field, f"{field} 必须在 {int(minimum)} 到 {int(maximum)} 之间。"))


def _validate_enums(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    cleanup_mode = config["service"].get("cleanup_mode")
    if cleanup_mode != "preview_only":
        errors.append(_error("service.cleanup_mode", "清理模式目前只允许 preview_only，避免误删文件。"))
    if config["asr"].get("backend") not in {"mlx_whisper", "openai"}:
        errors.append(_error("asr.backend", "ASR 后端只能选择 mlx_whisper 或 openai。"))


def _validate_env_vars(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    for section, key in ENV_VAR_FIELDS:
        value = str(config[section].get(key) or "")
        if not ENV_VAR_RE.match(value):
            errors.append(_error(f"{section}.{key}", "环境变量名只能使用大写字母、数字和下划线，并且不能以数字开头。"))


def _merge_editable(raw: dict[str, Any], clean: EditableConfig) -> dict[str, Any]:
    merged = deepcopy(raw)
    for section in ["paths", "llm", "asr", "service"]:
        merged.setdefault(section, {})
        merged[section].update(_coerce_section(section, clean[section]))
    merged.setdefault("recording_source", {})
    merged["recording_source"].setdefault("default", {})
    merged["recording_source"]["default"].update(_coerce_section("recording_source_default", clean["recording_source_default"]))
    merged.setdefault("web", {})
    merged["web"].update(_coerce_section("web", {key: clean["web"][key] for key in ("host", "port") if key in clean["web"]}))
    return merged


def _coerce_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    coerced = {}
    for key, value in values.items():
        if key == "access_token_configured":
            continue
        if (section, key) in INTEGER_FIELDS:
            coerced[key] = int(float(value))
        elif (section, key) in NUMBER_RANGES:
            coerced[key] = float(value)
        elif (section, key) in BOOLEAN_FIELDS:
            coerced[key] = bool(value)
        elif section in PATH_FIELDS and key in PATH_FIELDS[section]:
            coerced[key] = str(value or "")
        else:
            coerced[key] = value
    return coerced


def _diff_editable(old: EditableConfig, new: EditableConfig) -> list[dict[str, Any]]:
    changes = []
    for section, fields in new.items():
        for key, value in fields.items():
            if key == "access_token_configured":
                continue
            old_value = old.get(section, {}).get(key)
            if old_value != value:
                changes.append({"field": f"{section}.{key}", "old": old_value, "new": value})
    return changes


def _changes_require_service_restart(changes: list[dict[str, Any]]) -> bool:
    return any(str(change["field"]).split(".", 1)[0] in SERVICE_RESTART_SECTIONS for change in changes)


def _changes_require_web_restart(changes: list[dict[str, Any]]) -> bool:
    return any(change["field"] in {"web.host", "web.port"} for change in changes)


def _env_status(config: EditableConfig) -> dict[str, bool]:
    names = []
    for section, key in ENV_VAR_FIELDS:
        value = config.get(section, {}).get(key)
        if value:
            names.append(str(value))
    return {name: bool(os.getenv(name)) for name in dict.fromkeys(names)}


def _warnings_for(config: EditableConfig, *, base_dir: Path) -> list[dict[str, str]]:
    validation = validate_editable_config(config, base_dir=base_dir)
    return list(validation.get("warnings", []))


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
        "# Generated by the Web 配置 page. API keys should stay in environment variables or .env.",
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
    return json.dumps(str(value), ensure_ascii=False)


def _resolve_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else base_dir / path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _error(field: str, message: str) -> dict[str, str]:
    return {"field": field, "message": message}


def _saved_message(validation: dict[str, Any]) -> str:
    if validation.get("requires_service_restart"):
        return "配置已保存。为了让常驻服务使用新配置，请重启服务。"
    return "配置已保存。"


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
