"""MCP-facing tools as thin adapters over the local service core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import service
from .codex_selection import validate_selected_clips_file
from .config import Settings, load_settings
from .pipeline import cleanup_plan
from .utils import read_json, write_json

REVIEW_PACKAGE_FILES = [
    "codex_brief.json",
    "codex_review.md",
    "selected_clips.template.json",
    "refined_candidates.json",
]

TOOL_SCHEMAS = {
    "get_service_status": {"type": "object", "properties": {}, "required": []},
    "list_runs": {
        "type": "object",
        "properties": {
            "phase": {"type": "string"},
            "source_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": [],
    },
    "get_run_detail": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    "get_run_log": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}, "lines": {"type": "integer", "minimum": 1}},
        "required": ["run_id"],
    },
    "get_review_package": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    "scan_now": {"type": "object", "properties": {}, "required": []},
    "start_run_for_source": {
        "type": "object",
        "properties": {"source_path": {"type": "string"}, "source_id": {"type": "string"}},
        "required": ["source_path"],
    },
    "write_selected_clips": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "selected_clips": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["run_id", "selected_clips"],
    },
    "render_run": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    "preview_cleanup": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    "delete_clip": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "clip_filename": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["run_id", "clip_filename", "reason"],
    },
    "cleanup_confirm": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["run_id", "reason"],
    },
    "delete_local_source": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["run_id", "reason"],
    },
}


def get_tool_manifest() -> dict[str, Any]:
    return {
        "ok": True,
        "tools": [
            {
                "name": name,
                "input_schema": schema,
            }
            for name, schema in TOOL_SCHEMAS.items()
        ],
    }


def _validate_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return _error("unknown_tool", f"Unknown MCP tool: {tool_name}")
    missing = [field for field in schema.get("required", []) if field not in arguments]
    if missing:
        return _error("invalid_arguments", f"Missing required argument(s): {', '.join(missing)}")
    return None


def call_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    error = _validate_arguments(tool_name, arguments)
    if error is not None:
        return error
    if tool_name == "get_service_status":
        return get_service_status(service_dir=service_dir)
    if tool_name == "list_runs":
        return list_runs(
            phase=arguments.get("phase"),
            source_id=arguments.get("source_id"),
            limit=int(arguments.get("limit", 20)),
            service_dir=service_dir,
        )
    if tool_name == "get_run_detail":
        return get_run_detail(str(arguments["run_id"]), service_dir=service_dir)
    if tool_name == "get_run_log":
        return get_run_log(str(arguments["run_id"]), lines=int(arguments.get("lines", 200)), service_dir=service_dir)
    if tool_name == "get_review_package":
        return get_review_package(str(arguments["run_id"]), service_dir=service_dir)
    if tool_name == "scan_now":
        return scan_now(settings=settings, service_dir=service_dir)
    if tool_name == "start_run_for_source":
        return start_run_for_source(
            str(arguments["source_path"]),
            source_id=arguments.get("source_id"),
            settings=settings,
            service_dir=service_dir,
        )
    if tool_name == "write_selected_clips":
        return write_selected_clips(
            str(arguments["run_id"]),
            list(arguments["selected_clips"]),
            service_dir=service_dir,
        )
    if tool_name == "render_run":
        return render_run(str(arguments["run_id"]), settings=settings, service_dir=service_dir)
    if tool_name == "preview_cleanup":
        return preview_cleanup(str(arguments["run_id"]), settings=settings, service_dir=service_dir)
    if tool_name == "delete_clip":
        return delete_clip(
            str(arguments["run_id"]),
            str(arguments["clip_filename"]),
            reason=str(arguments["reason"]),
            service_dir=service_dir,
        )
    if tool_name == "cleanup_confirm":
        return cleanup_confirm(
            str(arguments["run_id"]),
            reason=str(arguments["reason"]),
            settings=settings,
            service_dir=service_dir,
        )
    if tool_name == "delete_local_source":
        return delete_local_source(
            str(arguments["run_id"]),
            reason=str(arguments["reason"]),
            settings=settings,
            service_dir=service_dir,
        )
    return _error("unknown_tool", f"Unknown MCP tool: {tool_name}")


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(error_code: str, message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, **payload}


def _confirmation_required(confirmation: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "confirmation_required",
        "status": "confirmation_required",
        "confirmation_id": confirmation["id"],
        "message": "用户需要在 Web 控制台确认后才会执行删除。",
        "confirmation": confirmation,
    }


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else load_settings()


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _find_run_or_error(run_id: str, service_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    run = service.find_run(run_id, service_dir)
    if run is None:
        return None, _error("run_not_found", f"Run not found: {run_id}")
    return run, None


def _count_json_items(path: Path) -> int:
    if not path.exists():
        return 0
    data = read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        items = data.get("candidates")
        return len(items) if isinstance(items, list) else 0
    return 0


def _file_status(run_dir: Path, filename: str) -> dict[str, Any]:
    path = run_dir / filename
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
    }


def get_service_status(*, service_dir: Path = service.DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    status = service.get_service_status(service_dir=service_dir)
    status["pending_confirmation_count"] = service.pending_confirmation_count(service_dir)
    return status


def list_runs(
    *,
    phase: str | None = None,
    source_id: str | None = None,
    limit: int = 20,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    runs = service.load_runs(service_dir)
    if phase is not None:
        runs = [run for run in runs if run.get("phase") == phase]
    if source_id is not None:
        runs = [run for run in runs if run.get("source_id") == source_id]
    summaries = [
        {
            "run_id": run.get("run_id"),
            "source_id": run.get("source_id"),
            "phase": run.get("phase"),
            "run_dir": run.get("run_dir"),
            "source_path": run.get("source_path"),
            "updated_at": run.get("updated_at"),
            "last_error": run.get("last_error"),
        }
        for run in runs[:limit]
    ]
    return _ok(runs=summaries, count=len(summaries), total=len(runs))


def get_run_detail(run_id: str, *, service_dir: Path = service.DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    run_dir = Path(str(run["run_dir"]))
    clips_dir = run_dir / "clips"
    rendered_clips = sorted(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    files = {
        filename: _file_status(run_dir, filename)
        for filename in [
            "codex_brief.json",
            "codex_review.md",
            "selected_clips.template.json",
            "selected_clips.json",
            "merged_candidates.json",
            "refined_candidates.json",
        ]
    }
    return _ok(
        run=run,
        files=files,
        candidates_count=_count_json_items(run_dir / "codex_brief.json"),
        selected_count=_count_json_items(run_dir / "selected_clips.json"),
        rendered_clip_count=len(rendered_clips),
        rendered_clips=[str(path) for path in rendered_clips],
        events=service.read_event_tail(service_dir, run_id=run_id),
    )


def get_run_log(
    run_id: str,
    *,
    lines: int = 200,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    log_path = Path(str(run.get("log_path", "")))
    if not log_path.exists():
        return _ok(run_id=run_id, log="", log_path=str(log_path))
    log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _ok(run_id=run_id, log="\n".join(log_lines[-lines:]), log_path=str(log_path))


def get_review_package(run_id: str, *, service_dir: Path = service.DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    run_dir = Path(str(run["run_dir"]))
    files: dict[str, dict[str, Any]] = {}
    for filename in REVIEW_PACKAGE_FILES:
        path = run_dir / filename
        file_payload: dict[str, Any] = {"path": str(path), "exists": path.exists()}
        if path.exists() and path.suffix == ".json":
            file_payload["content"] = read_json(path)
        elif path.exists():
            file_payload["text"] = path.read_text(encoding="utf-8")
        files[filename] = file_payload
    return _ok(run_id=run_id, run_dir=str(run_dir), files=files)


def scan_now(
    *,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    try:
        report = service.run_service_once(_settings(settings), service_dir=service_dir)
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        return _error("internal_error", str(exc))
    service.append_event(service_dir, "mcp_scan_now", report=report)
    return report


def start_run_for_source(
    source_path: str,
    *,
    source_id: str | None = None,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    resolved_settings = _settings(settings)
    source_config = resolved_settings.recording_source_default
    if source_id is not None and source_id != source_config.source_id:
        return _error("path_rejected", f"Unknown source_id: {source_id}")
    path = Path(source_path)
    if source_config.source_dir is None or not _path_is_relative_to(path, source_config.source_dir):
        return _error("path_rejected", f"Source path is outside configured source_dir: {source_path}")
    stable_sources = {candidate.resolve() for candidate in service.scan_recording_source(source_config)}
    if path.resolve() not in stable_sources:
        return _error("source_not_stable", f"Source is not stable or not eligible for processing: {source_path}")
    try:
        run = service.start_run_for_source(path, settings=resolved_settings, service_dir=service_dir)
    except ValueError as exc:
        if str(exc) == "duplicate_run":
            return _error("duplicate_run", f"Run already exists for source: {source_path}")
        return _error("internal_error", str(exc))
    service.append_event(service_dir, "mcp_start_run_for_source", run_id=run["run_id"], source_path=str(path))
    return _ok(run=run)


def write_selected_clips(
    run_id: str,
    selected_clips: list[dict[str, Any]],
    *,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    run_dir = Path(str(run["run_dir"]))
    candidates_path = run_dir / "merged_candidates.json"
    selection_path = run_dir / "selected_clips.json"
    temp_path = run_dir / "selected_clips.mcp.tmp.json"
    try:
        write_json(temp_path, selected_clips)
        validated = validate_selected_clips_file(temp_path, candidates_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return _error("selection_validation_failed", str(exc))
    temp_path.replace(selection_path)
    service.append_event(service_dir, "selected_clips_written", run_id=run_id, selection_path=str(selection_path))
    return _ok(run_id=run_id, selected_count=len(validated), selection_path=str(selection_path))


def render_run(
    run_id: str,
    *,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    run_dir = Path(str(run["run_dir"]))
    selection_path = run_dir / "selected_clips.json"
    if not selection_path.exists():
        return _error("invalid_phase", f"Run is missing selected_clips.json: {run_id}")
    service.append_event(service_dir, "mcp_render_started", run_id=run_id, run_dir=str(run_dir))
    rendered_paths = service.render_selected_clips(selection_path)
    run["phase"] = "rendered"
    run["updated_at"] = service.now_utc()
    service.replace_run(run, service_dir)
    service.append_event(service_dir, "mcp_render_completed", run_id=run_id, rendered_paths=[str(path) for path in rendered_paths])
    return _ok(run_id=run_id, rendered_paths=[str(path) for path in rendered_paths])


def preview_cleanup(
    run_id: str,
    *,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    input_dir = _settings(settings).recording_source_default.input_dir
    cleanup = service.cleanup_local_artifacts(Path(str(run["run_dir"])), input_dir=input_dir, confirm=False)
    service.append_event(service_dir, "cleanup_preview_created", run_id=run_id, run_dir=run["run_dir"], created_by="mcp")
    return _ok(run_id=run_id, cleanup=cleanup)


def delete_clip(
    run_id: str,
    clip_filename: str,
    *,
    reason: str,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    clips_dir = Path(str(run["run_dir"])) / "clips"
    target = clips_dir / clip_filename
    if Path(clip_filename).name != clip_filename or not _path_is_relative_to(target, clips_dir) or target.suffix.lower() != ".mp4":
        return _error("path_rejected", f"Clip path is not allowed: {clip_filename}")
    if not target.exists():
        return _error("path_rejected", f"Clip does not exist: {clip_filename}")
    confirmation = service.create_confirmation(
        action="delete_clip",
        run_id=run_id,
        target_path=target,
        reason=reason,
        risk_level="low",
        validation={
            "must_be_relative_to": str(clips_dir),
            "allowed_suffixes": [".mp4"],
        },
        service_dir=service_dir,
    )
    return _confirmation_required(confirmation)


def cleanup_confirm(
    run_id: str,
    *,
    reason: str,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    input_dir = _settings(settings).recording_source_default.input_dir
    targets = cleanup_plan(Path(str(run["run_dir"])), input_dir=input_dir)
    confirmation = service.create_confirmation(
        action="cleanup_confirm",
        run_id=run_id,
        target_path=Path(str(run["run_dir"])),
        reason=reason,
        risk_level="medium",
        validation={
            "cleanup_targets": targets,
            "requires_rendered_clips": True,
            "confirm_must_be_true_at_execution_time": True,
        },
        service_dir=service_dir,
    )
    return _confirmation_required(confirmation)


def delete_local_source(
    run_id: str,
    *,
    reason: str,
    settings: Settings | None = None,
    service_dir: Path = service.DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    run, error = _find_run_or_error(run_id, service_dir)
    if error is not None:
        return error
    assert run is not None
    input_dir = _settings(settings).recording_source_default.input_dir
    local_source = Path(str(run.get("local_source_path") or ""))
    if not local_source.exists() or not _path_is_relative_to(local_source, input_dir):
        return _error("path_rejected", f"Local source is outside input_dir or missing: {local_source}")
    confirmation = service.create_confirmation(
        action="delete_local_source",
        run_id=run_id,
        target_path=local_source,
        reason=reason,
        risk_level="medium",
        validation={
            "must_be_relative_to": str(input_dir),
            "requires_rendered_clips": True,
        },
        service_dir=service_dir,
    )
    return _confirmation_required(confirmation)
