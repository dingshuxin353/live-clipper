"""Local web console for live-clipper runs."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import asr_models, config_editor, jobs, mcp_tools, onboarding, review_automation, scheduler, service
from .automation import DEFAULT_LOG_DIR, DEFAULT_STATE_DIR, _pid_is_running, check_automation_runs
from .config import RecordingSourceDefaultConfig, ServiceConfig, Settings, load_settings
from .pipeline import cleanup_local_artifacts, cleanup_plan
from .render_clips import render_selected_clips
from .status import build_run_status
from .utils import read_json, write_json

STATIC_DIR = Path(__file__).parent / "web_static"


@dataclass(frozen=True)
class WebPaths:
    output_root: Path = Path("output")
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    input_dir: Path = Path("input")
    service_dir: Path = service.DEFAULT_SERVICE_DIR
    config_path: Path = Path("live-clipper.toml")


def _safe_read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _count_candidates(path: Path) -> int:
    data = _safe_read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            return len(candidates)
    return 0


def _tail_text(path: Path, *, max_lines: int = 200) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _settings_for_paths(paths: WebPaths) -> Settings:
    try:
        loaded = load_settings(paths.config_path)
    except Exception:
        loaded = Settings()
    return Settings(
        cheap_model_api_key=loaded.cheap_model_api_key,
        asr_api_key=loaded.asr_api_key,
        hf_token=loaded.hf_token,
        paths=loaded.paths,
        recording_source=loaded.recording_source,
        service=loaded.service if loaded.service else ServiceConfig(),
        recording_source_default=RecordingSourceDefaultConfig(
            source_id=loaded.recording_source_default.source_id,
            source_dir=loaded.recording_source_default.source_dir,
            input_dir=paths.input_dir,
            output_root=paths.output_root,
            since_hours=loaded.recording_source_default.since_hours,
            min_age_minutes=loaded.recording_source_default.min_age_minutes,
            stable_check_seconds=loaded.recording_source_default.stable_check_seconds,
        ),
        asr=loaded.asr,
        llm=loaded.llm,
        prompts=loaded.prompts,
        privacy=loaded.privacy,
        web=loaded.web,
        review=loaded.review,
        render=loaded.render,
        scheduler=loaded.scheduler,
        review_automation=loaded.review_automation,
    )


def _service_runs_available(paths: WebPaths) -> bool:
    return (paths.service_dir / "runs.json").exists()


def _structured_error(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, "error": message}


def _load_state(paths: WebPaths, run_id: str) -> dict[str, Any]:
    state = _safe_read_json(paths.state_dir / f"{run_id}.json")
    return state if isinstance(state, dict) else {}


def _log_path_for_run(paths: WebPaths, run_id: str, state: dict[str, Any]) -> Path:
    if isinstance(state.get("log_path"), str):
        return Path(state["log_path"])
    return paths.log_dir / f"{run_id}.log"


def _phase_for(status: dict[str, Any], running: bool) -> str:
    files = status["files"]
    if files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) > 0:
        return "cleanup_ready"
    if files["selected_clips.json"]["exists"]:
        return "ready_to_render"
    if files["codex_brief.json"]["exists"]:
        return "needs_codex_selection"
    if running:
        return "running"
    if status["exists"]:
        return "waiting_or_manual"
    return "missing"


def _run_summary(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    status = build_run_status(run_dir, write_report=False)
    state = _load_state(paths, run_dir.name)
    pid = state.get("pid")
    running = isinstance(pid, int) and _pid_is_running(pid)
    phase = _phase_for(status, running)
    files = status["files"]
    refined_count = _count_candidates(run_dir / "refined_candidates.json")
    merged_count = _count_candidates(run_dir / "merged_candidates.json")
    selected_count = _count_candidates(run_dir / "selected_clips.json")
    log_path = _log_path_for_run(paths, run_dir.name, state)
    updated_at = state.get("updated_at")
    if not updated_at and run_dir.exists():
        updated_at = run_dir.stat().st_mtime

    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "source_name": _source_name(run_dir),
        "phase": phase,
        "next_step": status["next_step"],
        "requires_codex": phase in {"needs_codex_selection", "cleanup_ready"},
        "running": running,
        "pid": pid,
        "candidate_count": refined_count or merged_count,
        "selected_count": selected_count,
        "clip_count": files["clips"].get("count", 0),
        "log_path": str(log_path) if log_path.exists() else None,
        "updated_at": updated_at,
    }


def _source_name(run_dir: Path) -> str:
    metadata = _safe_read_json(run_dir / "run_metadata.json")
    if isinstance(metadata, dict):
        return str(metadata.get("source_name") or metadata.get("source_video_path") or run_dir.name)
    return run_dir.name


def _run_looks_stuck(run: dict[str, Any], stuck_after_minutes: int) -> bool:
    """判断某个 run 是否「停在处理中且疑似卡死」，用于前端提示。"""
    if stuck_after_minutes <= 0:
        return False
    if run.get("phase") != "processing":
        return False
    updated_at = run.get("updated_at")
    started = _coerce_timestamp(updated_at) or _coerce_timestamp(run.get("created_at"))
    if started is None:
        return False
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - started >= timedelta(minutes=stuck_after_minutes)


def _coerce_timestamp(value: Any):
    from datetime import UTC, datetime

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def build_runs_index(paths: WebPaths | None = None) -> dict[str, Any]:
    paths = paths or WebPaths()
    stuck_after_minutes = _settings_for_paths(paths).service.stuck_after_minutes
    if _service_runs_available(paths):
        runs = mcp_tools.list_runs(service_dir=paths.service_dir)["runs"]
        for run in runs:
            run["source_name"] = Path(str(run.get("source_path") or run.get("run_id"))).name
            run["candidate_count"] = _count_candidates(Path(str(run["run_dir"])) / "codex_brief.json")
            run["selected_count"] = _count_candidates(Path(str(run["run_dir"])) / "selected_clips.json")
            clips_dir = Path(str(run["run_dir"])) / "clips"
            run["clip_count"] = len(sorted(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
            run["requires_codex"] = run.get("phase") == "needs_review"
            run["stuck"] = _run_looks_stuck(run, stuck_after_minutes)
        return {
            "ok": True,
            "runs": runs,
            "requires_codex": any(run["requires_codex"] for run in runs),
        }
    runs = []
    if paths.output_root.exists():
        for run_dir in sorted(paths.output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if run_dir.is_dir():
                summary = _run_summary(run_dir, paths)
                summary["stuck"] = _run_looks_stuck(summary, stuck_after_minutes)
                runs.append(summary)
    return {
        "ok": True,
        "runs": runs,
        "requires_codex": any(run["requires_codex"] for run in runs),
    }


def build_clip_list(run_dir: Path) -> list[dict[str, Any]]:
    clips_dir = run_dir / "clips"
    if not clips_dir.exists():
        return []
    clips = []
    for path in sorted(clips_dir.glob("*.mp4")):
        clips.append({
            "name": path.name,
            "path": str(path),
            "url": f"/media/runs/{quote(run_dir.name)}/clips/{quote(path.name)}",
            "bytes": path.stat().st_size,
            "updated_at": path.stat().st_mtime,
        })
    return clips


def _cleanup_preview(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    try:
        targets = cleanup_plan(run_dir, input_dir=paths.input_dir)
    except FileNotFoundError:
        targets = []
    return {
        "targets": targets,
        "deletable_bytes": sum(target["bytes"] for target in targets if target.get("deletable")),
    }


def _step(label: str, file_status: dict[str, Any], *, agnes: bool = False) -> dict[str, Any]:
    done = bool(file_status["exists"])
    return {
        "label": label,
        "done": done,
        "state": "done" if done else "pending",
        "agnes": agnes,
        "path": file_status.get("path"),
        "count": file_status.get("count"),
    }


def _steps_from_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    files = status["files"]
    steps = [
        _step("NAS 录制检测", files["run_metadata.json"]),
        _step("本地复制", files["run_metadata.json"]),
        _step("ASR 语音识别", files["transcript.json"]),
        _step("Agnes 扫描", files["merged_candidates.json"], agnes=True),
        _step("Agnes 精炼", files["refined_candidates.json"], agnes=True),
        _step("Codex 选择", files["selected_clips.json"]),
        _step("渲染导出", files["clips"]),
        _step("清理归档", files["run_metadata.json"]),
    ]
    if not steps[5]["done"] and files["codex_brief.json"]["exists"]:
        steps[5]["state"] = "waiting"
    if files["selected_clips.json"]["exists"] and not files["clips"].get("count", 0):
        steps[6]["state"] = "active"
    return steps


def _ai_review_for_run(run_id: str, paths: WebPaths) -> dict[str, Any] | None:
    """若最近一次 AI 审阅针对的是本 run，则返回其状态/错误，供前端展示。"""
    summary = review_automation.read_last_review_summary(paths.service_dir)
    if summary.get("last_run_id") != run_id:
        return None
    return {
        "status": summary.get("last_status"),
        "error": summary.get("last_error"),
        "at": summary.get("last_run_at"),
    }


def build_run_detail(run_id: str, paths: WebPaths | None = None, *, log_lines: int = 200) -> dict[str, Any]:
    paths = paths or WebPaths()
    service_run = service.find_run(run_id, paths.service_dir)
    if service_run is not None:
        detail = mcp_tools.get_run_detail(run_id, service_dir=paths.service_dir)
        if not detail.get("ok"):
            return detail
        run = dict(detail["run"])
        run_dir = Path(str(run["run_dir"]))
        run["source_name"] = Path(str(run.get("source_path") or run_id)).name
        run["candidate_count"] = detail.get("candidates_count", 0)
        run["selected_count"] = detail.get("selected_count", 0)
        run["clip_count"] = detail.get("rendered_clip_count", 0)
        run["requires_codex"] = run.get("phase") == "needs_review"
        cleanup = _cleanup_preview(run_dir, paths)
        return {
            "ok": True,
            "run": run,
            "steps": [],
            "files": detail["files"],
            "clips": build_clip_list(run_dir),
            "cleanup": cleanup,
            "state": service_run,
            "events": detail.get("events", []),
            "ai_review": _ai_review_for_run(run_id, paths),
            "active_job": jobs.active_job_for(paths.service_dir, run_id, "ai_review"),
            "actions": {
                "can_check": True,
                "can_render": (run_dir / "selected_clips.json").exists() and not detail.get("rendered_clip_count"),
                "can_cleanup_preview": (run_dir / "selected_clips.json").exists(),
                "can_cleanup": bool(detail.get("rendered_clip_count")),
                "can_delete_local_source": bool(service_run.get("local_source_path")) and bool(detail.get("rendered_clip_count")),
                "can_ai_review": run.get("phase") == "needs_review" and not (run_dir / "selected_clips.json").exists(),
            },
            "log": mcp_tools.get_run_log(run_id, lines=log_lines, service_dir=paths.service_dir),
        }
    run_dir = paths.output_root / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return {"ok": False, "error": f"任务不存在: {run_id}"}
    status = build_run_status(run_dir, write_report=False)
    run = _run_summary(run_dir, paths)
    state = _load_state(paths, run_id)
    log_path = _log_path_for_run(paths, run_id, state)
    files = status["files"]
    cleanup = _cleanup_preview(run_dir, paths)
    can_delete_local_source = any(
        target.get("kind") == "local_source_video" and target.get("deletable")
        for target in cleanup["targets"]
    )
    return {
        "ok": True,
        "run": run,
        "steps": _steps_from_status(status),
        "files": files,
        "clips": build_clip_list(run_dir),
        "cleanup": cleanup,
        "state": state,
        "ai_review": _ai_review_for_run(run_id, paths),
        "active_job": jobs.active_job_for(paths.service_dir, run_id, "ai_review"),
        "actions": {
            "can_check": True,
            "can_render": files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) == 0,
            "can_cleanup_preview": files["selected_clips.json"]["exists"],
            "can_cleanup": files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) > 0,
            "can_delete_local_source": can_delete_local_source and files["clips"].get("count", 0) > 0,
            "can_ai_review": run.get("phase") == "needs_codex_selection" and not files["selected_clips.json"]["exists"],
        },
        "log": {
            "path": str(log_path) if log_path.exists() else None,
            "tail": _tail_text(log_path, max_lines=log_lines) if log_path.exists() else "",
        },
    }


def handle_api_request(
    method: str,
    request_path: str,
    paths: WebPaths | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], Any]:
    paths = paths or WebPaths()
    parsed = urlparse(request_path)
    parsed_path = parsed.path
    query = parse_qs(parsed.query)
    parts = [unquote(part) for part in parsed_path.split("/") if part]
    try:
        if method == "GET" and parts == ["api", "service"]:
            return _json_response(_build_service_payload(paths))
        if method == "POST" and parts == ["api", "service", "start"]:
            if service.embedded_service_active():
                return _json_response(service.resume_embedded_service())
            return _json_response(service.start_service(_settings_for_paths(paths), service_dir=paths.service_dir))
        if method == "POST" and parts == ["api", "service", "stop"]:
            if service.embedded_service_active():
                return _json_response(service.pause_embedded_service())
            return _json_response(service.stop_service(service_dir=paths.service_dir))
        if method == "POST" and parts == ["api", "service", "scan-now"]:
            return _json_response(mcp_tools.scan_now(settings=_settings_for_paths(paths), service_dir=paths.service_dir))
        if method == "GET" and parts == ["api", "runs"]:
            payload = build_runs_index(paths)
            phase = query.get("phase", [None])[0]
            if phase:
                payload["runs"] = [run for run in payload["runs"] if run.get("phase") == phase]
            return _json_response(payload)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "runs"]:
            detail = build_run_detail(parts[2], paths)
            if detail.get("ok") is False and "error_code" not in detail:
                detail = _structured_error("run_not_found", str(detail.get("error", f"Run not found: {parts[2]}")))
            return _json_response(detail, status=200 if detail.get("ok") else 404)
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "log":
            log = mcp_tools.get_run_log(parts[2], service_dir=paths.service_dir)
            if not log.get("ok") and not _service_runs_available(paths):
                detail = build_run_detail(parts[2], paths)
                log = detail.get("log", {"ok": True, "tail": ""}) if detail.get("ok") else detail
            return _json_response(log, status=200 if log.get("ok", True) else 404)
        if method == "GET" and parts == ["api", "confirmations"]:
            return _json_response(_build_confirmations_payload(paths))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "confirmations"] and parts[3] == "approve":
            payload = service.approve_confirmation(parts[2], settings=_settings_for_paths(paths), service_dir=paths.service_dir)
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "confirmations"] and parts[3] == "reject":
            payload = service.reject_confirmation(parts[2], reason=(body or {}).get("reason"), service_dir=paths.service_dir)
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and parts == ["api", "confirmations", "batch-approve"]:
            payload = service.approve_confirmations(
                list((body or {}).get("ids", [])),
                settings=_settings_for_paths(paths),
                service_dir=paths.service_dir,
            )
            return _json_response(payload)
        if method == "POST" and parts == ["api", "confirmations", "batch-reject"]:
            payload = service.reject_confirmations(
                list((body or {}).get("ids", [])),
                reason=(body or {}).get("reason"),
                service_dir=paths.service_dir,
            )
            return _json_response(payload)
        if method == "GET" and parts == ["api", "events"]:
            return _json_response({"ok": True, "events": service.read_event_tail(paths.service_dir, max_events=200)})
        if method == "GET" and parts == ["api", "asr", "models"]:
            settings = _settings_for_paths(paths)
            source = getattr(settings.asr, "model_source", asr_models.DEFAULT_MODEL_SOURCE) if settings.asr else asr_models.DEFAULT_MODEL_SOURCE
            current_backend = settings.asr.backend if settings.asr else None
            current_model = settings.asr.model if settings.asr else None
            return _json_response(
                {
                    "ok": True,
                    "models": asr_models.list_models(
                        paths.service_dir,
                        download_source=source,
                        current_backend=current_backend,
                        current_model=current_model,
                    ),
                    "download_source": source,
                    "current_backend": current_backend,
                    "current_model": current_model,
                    "models_root": str(asr_models.models_root()),
                }
            )
        if method == "POST" and parts == ["api", "asr", "models", "download"]:
            model_id = str((body or {}).get("model") or "")
            if model_id not in asr_models.registry_ids():
                return _json_response(_structured_error("unknown_model", f"未知模型: {model_id}"), status=400)
            requested_source = (body or {}).get("source")
            if requested_source is None:
                settings = _settings_for_paths(paths)
                source = getattr(settings.asr, "model_source", asr_models.DEFAULT_MODEL_SOURCE) if settings.asr else asr_models.DEFAULT_MODEL_SOURCE
            else:
                source = str(requested_source)
            if source == "hf-mirror":
                return _json_response(
                    _structured_error("unsupported_model_source", asr_models.HF_MIRROR_REMOVED_MESSAGE),
                    status=400,
                )
            if source not in asr_models.source_ids():
                return _json_response(_structured_error("unknown_model_source", f"未知模型下载源: {source}"), status=400)
            job = jobs.start_job(
                paths.service_dir,
                kind=asr_models.DOWNLOAD_JOB_KIND,
                run_id=model_id,
                fn=lambda: asr_models.download_model(model_id, source),
            )
            return _json_response({"ok": True, "job": job}, status=202)
        if method == "POST" and parts == ["api", "asr", "models", "select"]:
            model_id = str((body or {}).get("model") or "")
            if model_id not in asr_models.registry_ids():
                return _json_response(_structured_error("unknown_model", f"未知模型: {model_id}"), status=400)
            entry = asr_models.model_entry(model_id)
            settings = _settings_for_paths(paths)
            current_backend = settings.asr.backend if settings.asr else None
            current_model = settings.asr.model if settings.asr else None
            if asr_models.local_path_for(model_id) is None:
                return _json_response(
                    _structured_error("model_not_ready", "模型尚未完整安装或已损坏"),
                    status=409,
                )
            if os.getenv("ASR_MODEL") is not None or os.getenv("ASR_BACKEND") is not None:
                return _json_response(
                    _structured_error(
                        "asr_overridden_by_environment",
                        "ASR 配置正被环境变量覆盖，请先移除 ASR_MODEL / ASR_BACKEND",
                    ),
                    status=409,
                )
            saved = config_editor.save_asr_model_selection(
                entry["backend"],
                model_id,
                config_path=paths.config_path,
                backup_root=paths.config_path.parent / "work" / "config_backups",
            )
            if not saved.get("ok"):
                return _json_response(
                    {
                        **_structured_error("config_save_failed", str(saved.get("message") or "配置保存失败")),
                        "saved": False,
                        "current_backend": current_backend,
                        "current_model": current_model,
                    },
                    status=400,
                )
            try:
                reload_result = _restart_service_from_config(paths)
            except Exception as exc:  # noqa: BLE001 - selection is saved even when service reload raises.
                reload_result = {"ok": False, "error": str(exc)}
            response = {
                "ok": bool(reload_result.get("ok")),
                "saved": True,
                "current_backend": entry["backend"],
                "current_model": model_id,
                "reload": reload_result,
            }
            if not reload_result.get("ok"):
                return _json_response(
                    {
                        **response,
                        "error_code": "service_reload_failed",
                        "message": "模型已保存，但服务重载失败",
                        "error": "模型已保存，但服务重载失败",
                    },
                    status=500,
                )
            return _json_response(response)
        if method == "POST" and parts == ["api", "asr", "models", "delete"]:
            model_id = str((body or {}).get("model") or "")
            if model_id not in asr_models.registry_ids():
                return _json_response(_structured_error("unknown_model", f"未知模型: {model_id}"), status=400)
            if jobs.active_job_for(paths.service_dir, model_id, asr_models.DOWNLOAD_JOB_KIND):
                return _json_response(
                    _structured_error("model_download_active", "模型正在下载，不能删除"),
                    status=409,
                )
            settings = _settings_for_paths(paths)
            if (
                settings.asr
                and settings.asr.backend == asr_models.model_entry(model_id)["backend"]
                and settings.asr.model == model_id
            ):
                return _json_response(
                    _structured_error("current_model_in_use", "请先切换到另一款已安装模型"),
                    status=409,
                )
            return _json_response({"ok": True, **asr_models.delete_model(model_id, service_dir=paths.service_dir)})
        if method == "GET" and parts == ["api", "settings"]:
            return _json_response(_build_settings_payload(paths))
        if method == "GET" and parts == ["api", "config"]:
            payload = config_editor.load_editable_config(config_path=paths.config_path)
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and parts == ["api", "config", "validate"]:
            payload = config_editor.validate_editable_config(
                (body or {}).get("config", {}),
                config_path=paths.config_path,
                base_dir=paths.config_path.parent,
            )
            return _json_response(payload)
        if method == "POST" and parts == ["api", "config"]:
            payload = config_editor.save_editable_config(
                (body or {}).get("config", {}),
                config_path=paths.config_path,
                backup_root=paths.config_path.parent / "work" / "config_backups",
                base_dir=paths.config_path.parent,
            )
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and parts == ["api", "config", "restart-service"]:
            return _json_response(_restart_service_from_config(paths))
        if method == "GET" and parts == ["api", "onboarding"]:
            return _json_response(onboarding.onboarding_status(_settings_for_paths(paths), paths.service_dir))
        if method == "POST" and parts == ["api", "onboarding", "test-source"]:
            return _json_response(onboarding.test_recording_source(str((body or {}).get("source_dir") or "")))
        if method == "POST" and parts == ["api", "onboarding", "test-llm"]:
            data = body or {}
            return _json_response(
                onboarding.test_llm(
                    str(data.get("api_base") or ""),
                    str(data.get("api_key") or ""),
                    str(data.get("model") or ""),
                )
            )
        if method == "POST" and parts == ["api", "onboarding", "complete"]:
            return _json_response(
                onboarding.complete_onboarding(body or {}, config_path=paths.config_path, service_dir=paths.service_dir)
            )
        if method == "GET" and parts == ["api", "scheduler"]:
            return _json_response(scheduler.get_scheduler_status(_settings_for_paths(paths), service_dir=paths.service_dir))
        if method == "GET" and parts == ["api", "scheduler", "events"]:
            return _json_response({"ok": True, "events": scheduler.read_scheduler_events(paths.service_dir)})
        if method == "GET" and parts == ["api", "review-automation"]:
            return _json_response(review_automation.get_review_automation_status(_settings_for_paths(paths), service_dir=paths.service_dir))
        if method == "POST" and parts == ["api", "review-automation", "check"]:
            return _json_response(review_automation.check_environment(_settings_for_paths(paths)))
        if method == "POST" and parts == ["api", "review-automation", "run-due"]:
            payload = review_automation.run_due_ai_reviews(_settings_for_paths(paths), service_dir=paths.service_dir)
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and parts == ["api", "scheduler", "jobs"]:
            payload = _upsert_scheduler_job(paths, (body or {}).get("job", {}))
            return _json_response(payload, status=200 if payload.get("ok") else 400)
        if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "scheduler", "jobs"] and parts[4] == "run-now":
            payload = _run_scheduler_job_now(paths, parts[3])
            return _json_response(payload, status=200 if payload.get("ok") else 404)
        if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "scheduler", "jobs"] and parts[4] == "pause":
            return _json_response(scheduler.pause_job(parts[3], service_dir=paths.service_dir))
        if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "scheduler", "jobs"] and parts[4] == "resume":
            return _json_response(scheduler.resume_job(parts[3], service_dir=paths.service_dir))
        if method == "POST" and parts == ["api", "automation", "check"]:
            return _json_response(check_automation_runs(paths.output_root, state_dir=paths.state_dir))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "render":
            if service.find_run(parts[2], paths.service_dir):
                return _json_response(mcp_tools.render_run(parts[2], settings=_settings_for_paths(paths), service_dir=paths.service_dir))
            return _json_response(_render_run(paths.output_root / parts[2]))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "ai-review":
            run_id = parts[2]
            if service.find_run(run_id, paths.service_dir) is None:
                return _json_response(_structured_error("run_not_found", f"任务不存在: {run_id}"), status=404)
            settings = _settings_for_paths(paths)
            service_dir = paths.service_dir
            job = jobs.start_job(
                service_dir,
                kind="ai_review",
                run_id=run_id,
                fn=lambda: review_automation.run_ai_review_for_run(run_id, settings, service_dir=service_dir),
            )
            return _json_response({"ok": True, "job": job}, status=202)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = jobs.read_job(paths.service_dir, parts[2])
            if job is None:
                return _json_response(_structured_error("job_not_found", "任务不存在"), status=404)
            return _json_response({"ok": True, "job": job})
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cleanup-preview":
            if service.find_run(parts[2], paths.service_dir):
                return _json_response(mcp_tools.preview_cleanup(parts[2], settings=_settings_for_paths(paths), service_dir=paths.service_dir))
            return _json_response(_cleanup_run(paths.output_root / parts[2], paths, confirm=False))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cleanup-confirm":
            if service.find_run(parts[2], paths.service_dir):
                return _json_response(mcp_tools.cleanup_confirm(parts[2], reason="web cleanup confirmation", settings=_settings_for_paths(paths), service_dir=paths.service_dir))
            return _json_response(_cleanup_run(paths.output_root / parts[2], paths, confirm=True))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "delete-local-source":
            if service.find_run(parts[2], paths.service_dir):
                return _json_response(mcp_tools.delete_local_source(parts[2], reason="web local source deletion", settings=_settings_for_paths(paths), service_dir=paths.service_dir))
            return _json_response(_delete_local_source(paths.output_root / parts[2], paths))
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "clips" and parts[5] == "delete":
            if service.find_run(parts[2], paths.service_dir):
                return _json_response(mcp_tools.delete_clip(parts[2], parts[4], reason="web clip deletion", service_dir=paths.service_dir))
            return _json_response(_delete_clip(paths.output_root / parts[2], parts[4]))
    except Exception as exc:  # noqa: BLE001 - local UI should surface actionable failures.
        return _json_response(_structured_error("internal_error", str(exc)), status=500)
    return _json_response(_structured_error("route_not_found", "API 路由不存在"), status=404)


def _json_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], Any]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, payload


def _build_service_payload(paths: WebPaths) -> dict[str, Any]:
    payload = mcp_tools.get_service_status(service_dir=paths.service_dir)
    settings = _settings_for_paths(paths)
    payload["source_summary"] = {
        "source_id": settings.recording_source_default.source_id,
        "source_dir": str(settings.recording_source_default.source_dir)
        if settings.recording_source_default.source_dir
        else None,
        "input_dir": str(settings.recording_source_default.input_dir),
        "output_root": str(settings.recording_source_default.output_root),
    }
    return payload


def _build_confirmations_payload(paths: WebPaths) -> dict[str, Any]:
    confirmations = service.load_confirmations(paths.service_dir)
    return {
        "ok": True,
        "confirmations": confirmations,
        "pending_count": sum(1 for confirmation in confirmations if confirmation.get("status") == "pending"),
    }


def _build_settings_payload(paths: WebPaths) -> dict[str, Any]:
    settings = _settings_for_paths(paths)
    return {
        "ok": True,
        "settings": {
            "service": {
                "scan_interval_minutes": settings.service.scan_interval_minutes,
                "auto_render_after_selection": settings.service.auto_render_after_selection,
                "cleanup_mode": settings.service.cleanup_mode,
            },
            "recording_source": {
                "source_id": settings.recording_source_default.source_id,
                "source_dir": str(settings.recording_source_default.source_dir)
                if settings.recording_source_default.source_dir
                else None,
                "input_dir": str(settings.recording_source_default.input_dir),
                "output_root": str(settings.recording_source_default.output_root),
            },
            "web": {
                "host": "127.0.0.1",
                "port": 8765,
            },
        },
    }


def _restart_service_from_config(paths: WebPaths) -> dict[str, Any]:
    if service.embedded_service_active():
        return {
            "ok": True,
            "restarted": False,
            "reason": "embedded_service_reloads_config_automatically",
            "status": service.get_service_status(service_dir=paths.service_dir),
        }
    status = service.get_service_status(service_dir=paths.service_dir)
    if not status.get("running"):
        return {
            "ok": True,
            "restarted": False,
            "reason": "service_not_running",
            "status": status,
        }
    stopped = service.stop_service(service_dir=paths.service_dir)
    started = service.start_service(load_settings(paths.config_path), service_dir=paths.service_dir)
    return {
        "ok": bool(stopped.get("ok") and started.get("ok")),
        "restarted": True,
        "stop": stopped,
        "start": started,
        "status": service.get_service_status(service_dir=paths.service_dir),
    }


def _upsert_scheduler_job(paths: WebPaths, job_payload: dict[str, Any]) -> dict[str, Any]:
    validation = scheduler.validate_scheduler_job(job_payload)
    if not validation.get("ok"):
        return validation
    editable = config_editor.load_editable_config(config_path=paths.config_path)
    if not editable.get("ok"):
        return editable
    config = editable["config"]
    jobs = [job for job in config.get("scheduler_jobs", []) if job.get("id") != job_payload.get("id")]
    jobs.append(job_payload)
    config["scheduler_jobs"] = jobs
    saved = config_editor.save_editable_config(
        config,
        config_path=paths.config_path,
        backup_root=paths.config_path.parent / "work" / "config_backups",
        base_dir=paths.config_path.parent,
    )
    if not saved.get("ok"):
        return saved
    return scheduler.get_scheduler_status(load_settings(paths.config_path), service_dir=paths.service_dir)


def _run_scheduler_job_now(paths: WebPaths, job_id: str) -> dict[str, Any]:
    settings = _settings_for_paths(paths)
    job = next((job for job in settings.scheduler.jobs if job.id == job_id), None)
    if job is None:
        return _structured_error("scheduler_job_not_found", f"定时任务不存在: {job_id}")
    return scheduler.run_job_now(job, settings, service_dir=paths.service_dir)


def _render_run(run_dir: Path) -> dict[str, Any]:
    selection_path = run_dir / "selected_clips.json"
    if not selection_path.exists():
        return {"ok": False, "error": "缺少 selected_clips.json，暂不能渲染"}
    rendered = render_selected_clips(selection_path)
    return {"ok": True, "rendered": [str(path) for path in rendered]}


def _cleanup_run(run_dir: Path, paths: WebPaths, *, confirm: bool) -> dict[str, Any]:
    report = cleanup_local_artifacts(run_dir, input_dir=paths.input_dir, confirm=confirm)
    return {"ok": True, **report}


def _delete_clip(run_dir: Path, clip_name: str) -> dict[str, Any]:
    clips_dir = run_dir / "clips"
    clip_path = clips_dir / clip_name
    if not _path_is_relative_to(clip_path, clips_dir):
        raise ValueError("切片路径越界，已阻止删除")
    if clip_path.suffix.lower() != ".mp4":
        raise ValueError("只能删除 mp4 成片")
    if not clip_path.exists():
        raise FileNotFoundError(clip_path)
    clip_path.unlink()
    return {"ok": True, "deleted": str(clip_path)}


def _delete_local_source(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    selected_path = run_dir / "selected_clips.json"
    clips = build_clip_list(run_dir)
    if not selected_path.exists() or not clips:
        raise RuntimeError("尚未检测到已渲染成片，暂不允许删除本机原录像副本")

    targets = cleanup_plan(run_dir, input_dir=paths.input_dir)
    for target in targets:
        if target.get("kind") != "local_source_video" or not target.get("deletable"):
            continue
        source_path = Path(target["path"])
        if not _path_is_relative_to(source_path, paths.input_dir):
            raise ValueError("原录像副本不在受控 input 目录内，已阻止删除")
        source_path.unlink(missing_ok=True)
        metadata_path = run_dir / "run_metadata.json"
        metadata = read_json(metadata_path)
        metadata["pipeline"] = {
            **metadata.get("pipeline", {}),
            "local_source_deleted": True,
            "local_source_deleted_path": str(source_path),
        }
        write_json(metadata_path, metadata)
        return {"ok": True, "deleted": str(source_path), "protected_original": target.get("reason")}
    raise RuntimeError("没有找到可删除的本机原录像副本")


class LiveClipperRequestHandler(BaseHTTPRequestHandler):
    paths = WebPaths()
    access_token: str | None = None

    def _authorized(self) -> bool:
        token = self.access_token
        if not token:
            return True
        if self.headers.get("Authorization", "") == f"Bearer {token}":
            return True
        cookie = self.headers.get("Cookie", "")
        return f"lc_token={token}" in cookie

    def _reject_unauthorized(self) -> None:
        body = json.dumps(_structured_error("unauthorized", "缺少或错误的访问令牌")).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/" or self.path.startswith("/static/"):
            self._serve_static(head_only=True)
            return
        if self.path.startswith("/media/"):
            self._serve_media(head_only=True)
            return
        status, headers, _payload = handle_api_request("GET", self.path, self.paths)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/" or self.path.startswith("/static/"):
            self._serve_static()
            return
        if self.path.startswith("/media/"):
            if not self._authorized():
                self._reject_unauthorized()
                return
            self._serve_media()
            return
        self._serve_api("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve_api("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _serve_api(self, method: str) -> None:
        if not self._authorized():
            self._reject_unauthorized()
            return
        body_payload: dict[str, Any] | None = None
        if method == "POST":
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length) if raw_length else 0
            if length:
                raw_body = self.rfile.read(length).decode("utf-8")
                body_payload = json.loads(raw_body) if raw_body else None
        status, headers, payload = handle_api_request(method, self.path, self.paths, body=body_payload)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, *, head_only: bool = False) -> None:
        target = STATIC_DIR / "index.html" if self.path == "/" else _static_path(self.path)
        if target is None or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_media(self, *, head_only: bool = False) -> None:
        clip_path = _media_clip_path(self.path, self.paths)
        if clip_path is None or not clip_path.exists() or not clip_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        file_size = clip_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        if range_header and range_header.startswith("bytes="):
            raw_start, _, raw_end = range_header.removeprefix("bytes=").partition("-")
            start = int(raw_start) if raw_start else 0
            end = int(raw_end) if raw_end else file_size - 1
            end = min(end, file_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        if head_only:
            return
        with clip_path.open("rb") as file:
            file.seek(start)
            self.wfile.write(file.read(length))


def _static_path(request_path: str) -> Path | None:
    parsed = urlparse(request_path).path
    if not parsed.startswith("/static/"):
        return None
    relative = posixpath.normpath(unquote(parsed[len("/static/"):]))
    if relative.startswith("../") or relative == "..":
        return None
    return STATIC_DIR / relative


def _media_clip_path(request_path: str, paths: WebPaths) -> Path | None:
    parsed = urlparse(request_path).path
    parts = [unquote(part) for part in parsed.split("/") if part]
    if len(parts) != 5 or parts[0] != "media" or parts[1] != "runs" or parts[3] != "clips":
        return None
    run_id = parts[2]
    clip_name = parts[4]
    clips_dir = paths.output_root / run_id / "clips"
    clip_path = clips_dir / clip_name
    if not _path_is_relative_to(clip_path, clips_dir):
        return None
    return clip_path


def run_web_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    paths: WebPaths | None = None,
    access_token: str | None = None,
) -> None:
    paths = paths or WebPaths()
    jobs.sweep_interrupted(paths.service_dir)
    handler = type(
        "ConfiguredLiveClipperRequestHandler",
        (LiveClipperRequestHandler,),
        {"paths": paths, "access_token": access_token or None},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"[Web] Venus 控制台已启动: http://{host}:{port}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[Web] 警告: 当前服务允许局域网访问。请勿暴露到公网。", flush=True)
    print("[Web] 本服务用于本机工作台。按 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Web] 已停止本地控制台。", flush=True)
    finally:
        server.server_close()
