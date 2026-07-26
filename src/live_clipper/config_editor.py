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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_TEMPLATE, load_settings
from .utils import ensure_dir

EditableConfig = dict[str, Any]

PATH_FIELDS = {
    "paths": {"workspace_root", "input_dir", "output_root", "work_dir", "glossary_path"},
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
    ("scheduler", "tick_seconds"): (5, 300),
    ("review_automation", "max_runs_per_tick"): (1, 20),
    ("review_automation", "timeout_minutes"): (1, 240),
    ("review_automation_local_agent", "command_timeout_minutes"): (1, 240),
    ("review_automation_model", "max_candidates"): (1, 200),
    ("review_automation_model", "temperature"): (0, 2),
    ("review_automation_model", "max_tokens"): (512, 32000),
    ("review_automation_model", "retry_attempts"): (0, 5),
}
INTEGER_FIELDS = {
    ("recording_source_default", "since_hours"),
    ("recording_source_default", "min_age_minutes"),
    ("recording_source_default", "stable_check_seconds"),
    ("llm", "timeout_seconds"),
    ("llm", "request_attempts"),
    ("service", "scan_interval_minutes"),
    ("web", "port"),
    ("scheduler", "tick_seconds"),
    ("review_automation", "max_runs_per_tick"),
    ("review_automation", "timeout_minutes"),
    ("review_automation_local_agent", "command_timeout_minutes"),
    ("review_automation_model", "max_candidates"),
    ("review_automation_model", "max_tokens"),
    ("review_automation_model", "retry_attempts"),
}
BOOLEAN_FIELDS = {
    ("service", "enabled"),
    ("service", "auto_render_after_selection"),
    ("scheduler", "enabled"),
    ("review_automation", "enabled"),
    ("review_automation", "auto_render_after_selection"),
    ("review_automation_local_agent", "include_review_package_inline"),
    ("review_automation_local_agent", "allow_agent_file_writes"),
    ("review_automation_model", "use_llm_config"),
}
ENV_VAR_FIELDS = {
    ("llm", "api_key_env"),
    ("asr", "api_key_env"),
    ("asr", "hf_token_env"),
}
SERVICE_RESTART_SECTIONS = {
    "paths",
    "recording_source_default",
    "llm",
    "asr",
    "service",
    "scheduler",
    "scheduler_jobs",
    "review_automation",
    "review_automation_local_agent",
    "review_automation_model",
}
ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
JOB_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
JOB_TYPES = {"scan_recordings", "review_due_check", "maintenance_check", "ai_review"}
SCHEDULES = {"weekly", "daily", "interval_minutes"}
DAYS_OF_WEEK = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


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
    _validate_scheduler(clean, errors)
    _validate_review_automation(clean, errors)

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


def save_asr_model_selection(
    backend: str,
    model: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    backup_root: Path = Path("work") / "config_backups",
) -> dict[str, Any]:
    if not isinstance(backend, str) or not backend.strip():
        return {"ok": False, "saved": False, "message": "ASR backend 不能为空"}
    if not isinstance(model, str) or not model.strip():
        return {"ok": False, "saved": False, "message": "ASR model 不能为空"}

    raw_result = _load_raw_config(config_path)
    if not raw_result["ok"]:
        return {"saved": False, **raw_result}

    merged = deepcopy(raw_result["config"])
    if not isinstance(merged.get("asr"), dict):
        merged["asr"] = {}
    merged["asr"]["backend"] = backend
    merged["asr"]["model"] = model
    rendered = _dump_toml(merged)
    existed = config_path.exists()
    original_text = config_path.read_text(encoding="utf-8") if existed else None
    backup_path: Path | None = None
    temp_name: str | None = None
    replaced = False

    try:
        if existed:
            backup_path = _backup_config(config_path, backup_root)
        ensure_dir(config_path.parent)
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
        settings = load_settings(config_path)
        if settings.asr.backend != backend or settings.asr.model != model:
            raise ValueError("ASR 模型配置回读不一致")
    except Exception as exc:  # noqa: BLE001 - restore the exact previous config on any write/readback failure.
        if replaced:
            if original_text is None:
                config_path.unlink(missing_ok=True)
            else:
                config_path.write_text(original_text, encoding="utf-8")
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink(missing_ok=True)
        return {
            "ok": False,
            "saved": False,
            "message": f"ASR 模型配置保存失败，已保留旧配置：{exc}",
            "error": str(exc),
        }

    return {
        "ok": True,
        "saved": True,
        "config_path": str(config_path),
        "backup_path": str(backup_path) if backup_path else None,
        "backend": backend,
        "model": model,
    }


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


def _editable_from_raw(raw: dict[str, Any]) -> EditableConfig:
    defaults = tomllib.loads(DEFAULT_CONFIG_TEMPLATE)
    default_asr = defaults.get("asr", {})
    recording_source = raw.get("recording_source", {})
    recording_default = recording_source.get("default", {}) if isinstance(recording_source, dict) else {}
    web = raw.get("web", {}) if isinstance(raw.get("web"), dict) else {}
    scheduler = raw.get("scheduler", {}) if isinstance(raw.get("scheduler"), dict) else {}
    review_automation = raw.get("review_automation", {}) if isinstance(raw.get("review_automation"), dict) else {}
    review_local_agent = (
        review_automation.get("local_agent", {})
        if isinstance(review_automation.get("local_agent"), dict)
        else {}
    )
    review_model = review_automation.get("model", {}) if isinstance(review_automation.get("model"), dict) else {}
    default_scheduler = defaults.get("scheduler", {})
    default_review = defaults.get("review_automation", {})
    default_review_local = default_review.get("local_agent", {}) if isinstance(default_review.get("local_agent"), dict) else {}
    default_review_model = default_review.get("model", {}) if isinstance(default_review.get("model"), dict) else {}
    scheduler_jobs = scheduler.get("jobs")
    if not isinstance(scheduler_jobs, list) or not scheduler_jobs:
        scheduler_jobs = default_scheduler.get("jobs", [])
    asr = {
        **_pick(default_asr, ["backend", "model", "language", "api_base", "api_key_env", "hf_token_env", "model_source"]),
        **_pick(
            raw.get("asr", {}),
            ["backend", "model", "language", "api_base", "api_key_env", "hf_token_env", "model_source"],
        ),
    }
    if asr.get("model_source") == "hf-mirror":
        asr["model_source"] = "modelscope"
    return {
        "paths": _pick(
            raw.get("paths", {}),
            ["workspace_root", "input_dir", "output_root", "work_dir", "glossary_path"],
        ),
        "recording_source_default": _pick(
            recording_default,
            ["source_dir", "input_dir", "output_root", "since_hours", "min_age_minutes", "stable_check_seconds"],
        ),
        "llm": _pick(
            raw.get("llm", {}),
            ["provider_label", "api_base", "api_key_env", "model", "timeout_seconds", "request_attempts", "retry_delay_seconds"],
        ),
        "asr": asr,
        "service": _pick(raw.get("service", {}), ["enabled", "scan_interval_minutes", "auto_render_after_selection", "cleanup_mode"]),
        "web": {
            **_pick(web, ["host", "port"]),
            "access_token_configured": bool(web.get("access_token")),
        },
        "scheduler": {
            **_pick(default_scheduler, ["enabled", "timezone", "tick_seconds", "missed_policy", "state_dir"]),
            **_pick(scheduler, ["enabled", "timezone", "tick_seconds", "missed_policy", "state_dir"]),
        },
        "scheduler_jobs": [_clean_job(job) for job in scheduler_jobs if isinstance(job, dict)],
        "review_automation": {
            **_pick(
                default_review,
                ["enabled", "mode", "max_runs_per_tick", "auto_render_after_selection", "on_failure", "timeout_minutes", "prompt_template"],
            ),
            **_pick(
                review_automation,
                ["enabled", "mode", "max_runs_per_tick", "auto_render_after_selection", "on_failure", "timeout_minutes", "prompt_template"],
            ),
        },
        "review_automation_local_agent": {
            **_pick(
                default_review_local,
                ["provider", "command_timeout_minutes", "include_review_package_inline", "allow_agent_file_writes"],
            ),
            **_pick(
                review_local_agent,
                ["provider", "command_timeout_minutes", "include_review_package_inline", "allow_agent_file_writes"],
            ),
        },
        "review_automation_model": {
            **_pick(
                default_review_model,
                ["provider", "use_llm_config", "model", "max_candidates", "temperature", "max_tokens", "retry_attempts"],
            ),
            **_pick(
                review_model,
                ["provider", "use_llm_config", "model", "max_candidates", "temperature", "max_tokens", "retry_attempts"],
            ),
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
        if section == "scheduler_jobs" and isinstance(fields, list):
            clean["scheduler_jobs"] = [_clean_job(job) for job in fields if isinstance(job, dict)]
            continue
        if section not in clean or not isinstance(fields, dict):
            continue
        for key, value in fields.items():
            if key == "access_token_configured":
                continue
            if key in clean[section] or (section, key) in NUMBER_RANGES or (section, key) in BOOLEAN_FIELDS:
                clean[section][key] = value
    return clean


def _clean_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "name",
        "enabled",
        "type",
        "schedule",
        "day_of_week",
        "time",
        "interval_minutes",
        "skip_if_running",
    ]
    return {key: _jsonable(job[key]) for key in keys if key in job and job[key] is not None}


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

    workspace_value = config["paths"].get("workspace_root")
    if workspace_value:
        workspace_path = _resolve_path(workspace_value, base_dir)
        if not workspace_path.exists():
            warnings.append({
                "field": "paths.workspace_root",
                "message": "任务工作区目录尚不存在，保存后运行时会按需创建。",
            })
        if source_path and (
            workspace_path == source_path
            or _is_relative_to(workspace_path, source_path)
            or _is_relative_to(source_path, workspace_path)
        ):
            errors.append(_error(
                "paths.workspace_root",
                "任务工作区与录播源目录不能相同或互相包含，避免把运行产物再次扫描为原始录像。",
            ))
        return

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
    if config["asr"].get("model_source", "modelscope") not in {"modelscope", "huggingface"}:
        errors.append(_error("asr.model_source", "模型下载源只能选择 modelscope 或 huggingface。"))
    if config["scheduler"].get("missed_policy") not in {"run_once", "skip"}:
        errors.append(_error("scheduler.missed_policy", "missed_policy 只能是 run_once 或 skip。"))


def _validate_env_vars(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    for section, key in ENV_VAR_FIELDS:
        value = str(config[section].get(key) or "")
        if not ENV_VAR_RE.match(value):
            errors.append(_error(f"{section}.{key}", "环境变量名只能使用大写字母、数字和下划线，并且不能以数字开头。"))


def _validate_scheduler(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    timezone = str(config["scheduler"].get("timezone") or "")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        errors.append(_error("scheduler.timezone", "调度时区无效，请填写有效 IANA timezone，例如 Asia/Shanghai。"))

    for index, job in enumerate(config.get("scheduler_jobs", [])):
        prefix = f"scheduler_jobs.{index}"
        job_id = str(job.get("id") or "")
        if not JOB_ID_RE.match(job_id):
            errors.append(_error(f"{prefix}.id", "任务 id 只能使用小写字母、数字、下划线或短横线，最长 64 个字符。"))
        name = str(job.get("name") or "")
        if not (1 <= len(name) <= 40):
            errors.append(_error(f"{prefix}.name", "任务名称必须填写，且不超过 40 个字符。"))
        job_type = str(job.get("type") or "")
        if job_type not in JOB_TYPES:
            errors.append(_error(f"{prefix}.type", "任务类型只能是 scan_recordings、review_due_check、maintenance_check 或 ai_review。"))
        schedule = str(job.get("schedule") or "")
        if schedule not in SCHEDULES:
            errors.append(_error(f"{prefix}.schedule", "频率只能是 weekly、daily 或 interval_minutes。"))
        if schedule == "weekly" and job.get("day_of_week") not in DAYS_OF_WEEK:
            errors.append(_error(f"{prefix}.day_of_week", "星期必须是 mon 到 sun。"))
        if schedule in {"weekly", "daily"} and not _valid_hhmm(str(job.get("time") or "")):
            errors.append(_error(f"{prefix}.time", "时间必须使用 HH:MM 格式。"))
        if schedule == "interval_minutes":
            try:
                interval = int(job.get("interval_minutes"))
            except (TypeError, ValueError):
                interval = 0
            if interval < 5 or interval > 1440:
                errors.append(_error(f"{prefix}.interval_minutes", "间隔分钟数必须在 5 到 1440 之间。"))


def _validate_review_automation(config: EditableConfig, errors: list[dict[str, str]]) -> None:
    review = config["review_automation"]
    local_agent = config["review_automation_local_agent"]
    model = config["review_automation_model"]
    if review.get("mode") not in {"local_agent", "model"}:
        errors.append(_error("review_automation.mode", "AI 审阅方式只能是 local_agent 或 model。"))
    if review.get("on_failure") not in {"keep_needs_review", "mark_failed"}:
        errors.append(_error("review_automation.on_failure", "失败后处理只能是 keep_needs_review 或 mark_failed。"))
    if local_agent.get("provider") not in {"codex_cli", "claude_code"}:
        errors.append(_error("review_automation_local_agent.provider", "本地 Agent 只能选择 codex_cli 或 claude_code。"))
    if local_agent.get("allow_agent_file_writes") is not False:
        errors.append(_error("review_automation_local_agent.allow_agent_file_writes", "P0 不允许 Agent 直接写文件，请保持关闭。"))
    if model.get("provider") != "openai_compatible":
        errors.append(_error("review_automation_model.provider", "模型直连目前只支持 openai_compatible。"))


def _valid_hhmm(value: str) -> bool:
    raw_hour, sep, raw_minute = value.partition(":")
    if sep != ":" or len(raw_hour) != 2 or len(raw_minute) != 2:
        return False
    if not raw_hour.isdigit() or not raw_minute.isdigit():
        return False
    hour = int(raw_hour)
    minute = int(raw_minute)
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _merge_editable(raw: dict[str, Any], clean: EditableConfig) -> dict[str, Any]:
    merged = deepcopy(raw)
    for section in [
        "paths",
        "llm",
        "asr",
        "service",
        "scheduler",
        "review_automation",
    ]:
        merged.setdefault(section, {})
        merged[section].update(_coerce_section(section, clean[section]))
    merged.setdefault("review_automation", {})
    merged["review_automation"]["local_agent"] = _coerce_section(
        "review_automation_local_agent",
        clean["review_automation_local_agent"],
    )
    merged["review_automation"]["model"] = _coerce_section(
        "review_automation_model",
        clean["review_automation_model"],
    )
    merged["scheduler"]["jobs"] = [_coerce_job(job) for job in clean.get("scheduler_jobs", [])]
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
        elif section in PATH_FIELDS and key in PATH_FIELDS[section] or (section, key) == ("scheduler", "state_dir"):
            coerced[key] = str(value or "")
        else:
            coerced[key] = value
    return coerced


def _coerce_job(job: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(job)
    coerced["enabled"] = bool(coerced.get("enabled", True))
    coerced["skip_if_running"] = bool(coerced.get("skip_if_running", True))
    if coerced.get("interval_minutes") not in (None, ""):
        coerced["interval_minutes"] = int(coerced["interval_minutes"])
    return coerced


def _diff_editable(old: EditableConfig, new: EditableConfig) -> list[dict[str, Any]]:
    changes = []
    for section, fields in new.items():
        if section == "scheduler_jobs":
            if old.get("scheduler_jobs") != fields:
                changes.append({"field": "scheduler_jobs", "old": old.get("scheduler_jobs"), "new": fields})
            continue
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
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _resolve_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value or "")).expanduser()
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
