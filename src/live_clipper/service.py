"""Local long-running service core for live-clipper."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .automation import SUPPORTED_VIDEO_EXTENSIONS
from .config import RecordingSourceDefaultConfig, Settings
from .pipeline import cleanup_local_artifacts, cleanup_plan, stage_source_file
from .render_clips import render_selected_clips
from .utils import ensure_dir, read_json, self_command, write_json

DEFAULT_SERVICE_DIR = Path("work") / "service"
PIPELINE_CONFIGURATION_MESSAGE = "请先到「设置 → AI 服务」配置 AI API Key，再开始处理录播。"
CONTENT_HASH_CHUNK_SIZE = 16 * 1024 * 1024
MAX_CONCURRENT_PIPELINES = 1

_EMBEDDED_LOCK = threading.Lock()
_EMBEDDED: dict[str, Any] = {"thread": None, "stop_event": None, "enabled_event": None, "service_dir": None}


class PipelineConfigurationError(ValueError):
    """Raised before a pipeline can create user-visible work without required configuration."""


class SourceChangedDuringHash(RuntimeError):
    """Raised when a recording changes while its content identity is being calculated."""


def require_pipeline_configuration(settings: Settings) -> None:
    if not settings.cheap_model_api_key:
        raise PipelineConfigurationError(PIPELINE_CONFIGURATION_MESSAGE)


def _content_hash_cache_path(service_dir: Path) -> Path:
    return service_dir / "content-hash-cache.json"


def _load_content_hash_cache(service_dir: Path) -> dict[str, Any]:
    path = _content_hash_cache_path(service_dir)
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = read_json(path)
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    return {"version": 1, "entries": entries if isinstance(entries, dict) else {}}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CONTENT_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def content_identity(source_path: Path, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    path = source_path.resolve()
    before = path.stat()
    cache = _load_content_hash_cache(service_dir)
    cache_key = str(path)
    cached = cache["entries"].get(cache_key, {})
    if (
        cached.get("size") == before.st_size
        and cached.get("mtime_ns") == before.st_mtime_ns
        and isinstance(cached.get("content_id"), str)
        and len(cached["content_id"]) == 64
    ):
        return {"content_id": cached["content_id"], "bytes": before.st_size, "cache_hit": True}

    content_id = _sha256_file(path)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise SourceChangedDuringHash(path)
    cache["entries"][cache_key] = {
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "content_id": content_id,
    }
    write_json(_content_hash_cache_path(ensure_dir(service_dir)), cache)
    return {"content_id": content_id, "bytes": after.st_size, "cache_hit": False}


def embedded_service_active() -> bool:
    thread = _EMBEDDED.get("thread")
    return thread is not None and thread.is_alive()


def start_embedded_service(
    settings_loader: Callable[[], Settings],
    *,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    """Run the scan/schedule loop as a thread inside this process (app mode).

    Unlike the detached CLI daemon, the embedded service lives and dies with
    the web backend process, so there is no pid-file self-management and no
    orphan risk. Settings are re-loaded every tick, so config changes made in
    the web UI take effect without a restart.
    """
    with _EMBEDDED_LOCK:
        if embedded_service_active():
            return {"ok": True, "started": False, "reason": "embedded_service_already_running"}
        stop_event = threading.Event()
        enabled_event = threading.Event()
        enabled_event.set()
        thread = threading.Thread(
            target=_embedded_service_loop,
            args=(settings_loader, service_dir, stop_event, enabled_event),
            daemon=True,
            name="live-clipper-embedded-service",
        )
        _EMBEDDED.update(
            {"thread": thread, "stop_event": stop_event, "enabled_event": enabled_event, "service_dir": service_dir}
        )
        thread.start()
    return {"ok": True, "started": True, "embedded": True, "pid": os.getpid()}


def pause_embedded_service() -> dict[str, Any]:
    if not embedded_service_active():
        return {"ok": True, "stopped": False, "reason": "service_not_running"}
    _EMBEDDED["enabled_event"].clear()
    service_dir = _EMBEDDED["service_dir"]
    _write_service_state(service_dir, {"status": "paused", "pid": os.getpid(), "paused_at": now_utc()})
    append_event(service_dir, "service_paused", pid=os.getpid())
    return {"ok": True, "stopped": True, "paused": True, "pid": os.getpid()}


def resume_embedded_service() -> dict[str, Any]:
    if not embedded_service_active():
        return {"ok": False, "error": "embedded_service_not_running"}
    _EMBEDDED["enabled_event"].set()
    service_dir = _EMBEDDED["service_dir"]
    _write_service_state(service_dir, {"status": "running", "pid": os.getpid(), "resumed_at": now_utc()})
    append_event(service_dir, "service_resumed", pid=os.getpid())
    return {"ok": True, "started": True, "resumed": True, "pid": os.getpid()}


def stop_embedded_service(timeout_seconds: float = 5.0) -> dict[str, Any]:
    thread = _EMBEDDED.get("thread")
    if thread is None or not thread.is_alive():
        return {"ok": True, "stopped": False, "reason": "service_not_running"}
    _EMBEDDED["stop_event"].set()
    thread.join(timeout_seconds)
    return {"ok": True, "stopped": True}


def _embedded_service_loop(
    settings_loader: Callable[[], Settings],
    service_dir: Path,
    stop_event: threading.Event,
    enabled_event: threading.Event,
) -> None:
    ensure_dir(service_dir)
    pid = os.getpid()
    _pid_path(service_dir).write_text(f"{pid}\n", encoding="utf-8")
    append_event(service_dir, "service_started", pid=pid, embedded=True)
    while not stop_event.is_set():
        try:
            settings = settings_loader()
            if not enabled_event.is_set():
                stop_event.wait(2)
                continue
            source_configured = settings.recording_source_default.source_dir or settings.recording_source.source_dir
            if source_configured is None:
                state = _service_state("running", pid, settings)
                state["waiting"] = "recording_source_not_configured"
                _write_service_state(service_dir, state)
                stop_event.wait(min(settings.scheduler.tick_seconds, 10))
                continue
            validate_service_settings(settings)
            report = run_service_tick(settings, service_dir=service_dir)
            state = _service_state("running", pid, settings)
            state["last_report"] = report
            _write_service_state(service_dir, state)
            stop_event.wait(settings.scheduler.tick_seconds)
        except Exception as exc:  # pragma: no cover - defensive for long-running loop
            _write_service_state(service_dir, {"status": "running", "pid": pid, "last_error": str(exc)})
            append_event(service_dir, "service_error", error=str(exc))
            stop_event.wait(60)
    _write_service_state(service_dir, {"status": "stopped", "pid": pid, "stopped_at": now_utc()})


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def pid_is_running(pid: int) -> bool:
    """判断 pid 是否为存活进程。

    常驻服务会以子进程方式启动流水线（见 _start_pipeline_process）。子进程结束后，
    在父进程回收（reap）之前会变成僵尸（defunct）进程；此时 os.kill(pid, 0) 仍会
    报告其「存活」，这正是导致 run 永久卡在 "processing" 的根因。

    因此这里先用非阻塞 waitpid 尝试回收：如果它是本进程的子进程且已退出，waitpid
    会返回它的 pid 并清除僵尸，从而可以正确报告「未运行」。若它不是本进程的子进程
    （例如服务重启后），waitpid 抛 ChildProcessError，退回到 os.kill 信号探测。
    """
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # 不是本进程的子进程（如服务已重启），退回信号探测。
        pass
    except OSError:
        # 探测时出现意外错误：视为未运行，避免把 run 永久卡死。
        return False
    else:
        # reaped_pid == pid -> 子进程已退出并被回收；reaped_pid == 0 -> 仍在运行。
        return reaped_pid != pid

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def validate_service_settings(settings: Settings) -> None:
    if settings.service.cleanup_mode != "preview_only":
        raise ValueError("V1 service supports cleanup_mode='preview_only' only")


def build_run_identity(
    source_id: str,
    source_path: Path,
    *,
    output_root: Path,
    input_dir: Path = Path("input"),
    workspace_root: Path | None = None,
    content_id: str | None = None,
) -> dict[str, Any]:
    if content_id is None:
        stat = source_path.stat()
        raw = f"{source_id}|{source_path}|{stat.st_size}|{stat.st_mtime_ns}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    else:
        fingerprint = content_id[:16]
    run_id = f"{source_path.stem}__{fingerprint}"
    if workspace_root is not None:
        workspace_dir = (workspace_root.expanduser() / "runs" / run_id).resolve()
        run_input_dir = workspace_dir / "input"
        run_dir = workspace_dir / "output"
    else:
        workspace_dir = None
        run_input_dir = input_dir
        run_dir = output_root / source_id / run_id
    return {
        "source_id": source_id,
        "fingerprint": fingerprint,
        "run_id": run_id,
        "workspace_dir": workspace_dir,
        "input_dir": run_input_dir,
        "run_dir": run_dir,
    }


def input_dir_for_run(run: dict[str, Any], settings: Settings) -> Path:
    value = run.get("input_dir")
    if value:
        return Path(str(value))
    return settings.recording_source_default.input_dir


def _is_stable_file(path: Path, stable_check_seconds: int) -> bool:
    before = path.stat()
    if stable_check_seconds > 0:
        time.sleep(stable_check_seconds)
    after = path.stat()
    return before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns


def scan_recording_source(config: RecordingSourceDefaultConfig) -> list[Path]:
    return scan_recording_source_report(config)["eligible"]


def scan_recording_source_report(config: RecordingSourceDefaultConfig) -> dict[str, Any]:
    if config.source_dir is None:
        return {"eligible": [], "unsupported_files": 0, "too_new_files": 0, "unstable_files": 0}
    if not config.source_dir.exists():
        raise FileNotFoundError(config.source_dir)

    now = datetime.now()
    latest_allowed_mtime = now - timedelta(minutes=config.min_age_minutes)
    candidates = []
    unsupported_files = 0
    too_new_files = 0
    unstable_files = 0
    for path in sorted(config.source_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            unsupported_files += 1
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime > latest_allowed_mtime:
            too_new_files += 1
            continue
        if not _is_stable_file(path, config.stable_check_seconds):
            unstable_files += 1
            continue
        candidates.append(path)
    return {
        "eligible": candidates,
        "unsupported_files": unsupported_files,
        "too_new_files": too_new_files,
        "unstable_files": unstable_files,
    }


def _runs_path(service_dir: Path) -> Path:
    return service_dir / "runs.json"


def _service_path(service_dir: Path) -> Path:
    return service_dir / "service.json"


def _pid_path(service_dir: Path) -> Path:
    return service_dir / "service.pid"


def _service_log_path(service_dir: Path) -> Path:
    return service_dir / "service.log"


def _events_path(service_dir: Path) -> Path:
    return service_dir / "events.jsonl"


def _confirmations_path(service_dir: Path) -> Path:
    return service_dir / "confirmations.json"


def load_runs(service_dir: Path = DEFAULT_SERVICE_DIR) -> list[dict[str, Any]]:
    path = _runs_path(service_dir)
    if not path.exists():
        return []
    data = read_json(path)
    return list(data.get("runs", []))


def save_runs(runs: list[dict[str, Any]], service_dir: Path = DEFAULT_SERVICE_DIR) -> None:
    write_json(_runs_path(ensure_dir(service_dir)), {"runs": runs})


def find_run(run_id: str, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any] | None:
    return next((run for run in load_runs(service_dir) if run.get("run_id") == run_id), None)


def replace_run(updated_run: dict[str, Any], service_dir: Path = DEFAULT_SERVICE_DIR) -> None:
    runs = load_runs(service_dir)
    for index, run in enumerate(runs):
        if run.get("run_id") == updated_run.get("run_id"):
            runs[index] = updated_run
            save_runs(runs, service_dir)
            return
    runs.append(updated_run)
    save_runs(runs, service_dir)


def load_confirmations(service_dir: Path = DEFAULT_SERVICE_DIR) -> list[dict[str, Any]]:
    path = _confirmations_path(service_dir)
    if not path.exists():
        return []
    data = read_json(path)
    return list(data.get("confirmations", []))


def save_confirmations(confirmations: list[dict[str, Any]], service_dir: Path = DEFAULT_SERVICE_DIR) -> None:
    write_json(_confirmations_path(ensure_dir(service_dir)), {"confirmations": confirmations})


def pending_confirmation_count(service_dir: Path = DEFAULT_SERVICE_DIR) -> int:
    return sum(1 for confirmation in load_confirmations(service_dir) if confirmation.get("status") == "pending")


def find_confirmation(confirmation_id: str, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any] | None:
    return next(
        (confirmation for confirmation in load_confirmations(service_dir) if confirmation.get("id") == confirmation_id),
        None,
    )


def _replace_confirmation(
    updated_confirmation: dict[str, Any],
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> None:
    confirmations = load_confirmations(service_dir)
    for index, confirmation in enumerate(confirmations):
        if confirmation.get("id") == updated_confirmation.get("id"):
            confirmations[index] = updated_confirmation
            save_confirmations(confirmations, service_dir)
            return
    confirmations.append(updated_confirmation)
    save_confirmations(confirmations, service_dir)


def append_event(service_dir: Path, event_type: str, **payload: Any) -> None:
    ensure_dir(service_dir)
    event = {
        "type": event_type,
        "created_at": now_utc(),
        **payload,
    }
    with _events_path(service_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _confirmation_error(error_code: str, message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, **payload}


def _confirmation_ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _clips_exist(run_dir: Path) -> bool:
    clips_dir = run_dir / "clips"
    return clips_dir.exists() and any(clips_dir.glob("*.mp4"))


def _metadata_pipeline(run_dir: Path) -> dict[str, Any]:
    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.exists():
        return {}
    metadata = read_json(metadata_path)
    pipeline = metadata.get("pipeline", {})
    return pipeline if isinstance(pipeline, dict) else {}


def _mark_confirmation_executed(
    confirmation: dict[str, Any],
    *,
    result: dict[str, Any],
    service_dir: Path,
) -> dict[str, Any]:
    updated = {
        **confirmation,
        "status": "approved_executed",
        "executed_at": now_utc(),
        "result": result,
    }
    _replace_confirmation(updated, service_dir)
    append_event(
        service_dir,
        "confirmation_executed",
        confirmation_id=updated["id"],
        run_id=updated.get("run_id"),
        action=updated.get("action"),
        target_path=updated.get("target_path"),
        result=result,
    )
    return updated


def reject_confirmation(
    confirmation_id: str,
    *,
    reason: str | None = None,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    confirmation = find_confirmation(confirmation_id, service_dir)
    if confirmation is None:
        return _confirmation_error("confirmation_not_found", f"Confirmation not found: {confirmation_id}")
    if confirmation.get("status") != "pending":
        return _confirmation_error("invalid_phase", f"Confirmation is not pending: {confirmation_id}")
    updated = {
        **confirmation,
        "status": "rejected",
        "rejected_at": now_utc(),
        "rejection_reason": reason,
    }
    _replace_confirmation(updated, service_dir)
    append_event(
        service_dir,
        "confirmation_rejected",
        confirmation_id=confirmation_id,
        run_id=confirmation.get("run_id"),
        action=confirmation.get("action"),
        target_path=confirmation.get("target_path"),
        reason=reason,
    )
    return _confirmation_ok(confirmation=updated)


def reject_confirmations(
    confirmation_ids: list[str],
    *,
    reason: str | None = None,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    results = [
        {"confirmation_id": confirmation_id, **reject_confirmation(confirmation_id, reason=reason, service_dir=service_dir)}
        for confirmation_id in confirmation_ids
    ]
    return {"ok": all(result.get("ok") for result in results), "results": results}


def approve_confirmation(
    confirmation_id: str,
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    confirmation = find_confirmation(confirmation_id, service_dir)
    if confirmation is None:
        return _confirmation_error("confirmation_not_found", f"Confirmation not found: {confirmation_id}")
    if confirmation.get("status") != "pending":
        return _confirmation_error("invalid_phase", f"Confirmation is not pending: {confirmation_id}")
    run = find_run(str(confirmation.get("run_id")), service_dir)
    if run is None:
        return _confirmation_error("run_not_found", f"Run not found: {confirmation.get('run_id')}")
    action = str(confirmation.get("action"))
    if action == "delete_clip":
        result = _approve_delete_clip(confirmation, run)
    elif action == "cleanup_confirm":
        result = _approve_cleanup_confirm(confirmation, run, settings=settings)
    elif action == "delete_local_source":
        result = _approve_delete_local_source(confirmation, run, settings=settings)
    else:
        return _confirmation_error("invalid_phase", f"Unsupported confirmation action: {action}")
    if not result.get("ok"):
        return result
    updated = _mark_confirmation_executed(confirmation, result=result, service_dir=service_dir)
    return _confirmation_ok(confirmation=updated, result=result)


def approve_confirmations(
    confirmation_ids: list[str],
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    results = [
        {"confirmation_id": confirmation_id, **approve_confirmation(confirmation_id, settings=settings, service_dir=service_dir)}
        for confirmation_id in confirmation_ids
    ]
    return {"ok": all(result.get("ok") for result in results), "results": results}


def _approve_delete_clip(confirmation: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(run["run_dir"]))
    clips_dir = run_dir / "clips"
    target = Path(str(confirmation.get("target_path")))
    validation = confirmation.get("validation", {})
    allowed_root = Path(str(validation.get("must_be_relative_to", clips_dir)))
    allowed_suffixes = set(validation.get("allowed_suffixes", [".mp4"]))
    if (
        not target.exists()
        or not _path_is_relative_to(target, clips_dir)
        or not _path_is_relative_to(target, allowed_root)
        or target.suffix.lower() not in allowed_suffixes
    ):
        return _confirmation_error("path_rejected", f"Clip target is no longer valid: {target}")
    if run.get("phase") not in {"rendered", "needs_review", "rendering"} or not _clips_exist(run_dir):
        return _confirmation_error("invalid_phase", "Run does not have rendered clips")
    target.unlink()
    return _confirmation_ok(action="delete_clip", deleted=[str(target)], target_path=str(target))


def _approve_cleanup_confirm(
    confirmation: dict[str, Any],
    run: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    run_dir = Path(str(run["run_dir"]))
    if not _clips_exist(run_dir):
        return _confirmation_error("invalid_phase", "Run does not have rendered clips")
    validation = confirmation.get("validation", {})
    allowed_paths = {
        str(Path(str(target.get("path"))).resolve())
        for target in validation.get("cleanup_targets", [])
        if target.get("deletable")
    }
    current_targets = cleanup_plan(run_dir, input_dir=input_dir_for_run(run, settings))
    deleted = []
    skipped = []
    for target in current_targets:
        path = Path(str(target["path"]))
        if not target.get("deletable") or str(path.resolve()) not in allowed_paths:
            skipped.append(str(path))
            continue
        path.unlink(missing_ok=True)
        deleted.append(str(path))
    return _confirmation_ok(action="cleanup_confirm", deleted=deleted, skipped=skipped, target_path=str(run_dir))


def _approve_delete_local_source(
    confirmation: dict[str, Any],
    run: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    run_dir = Path(str(run["run_dir"]))
    target = Path(str(confirmation.get("target_path")))
    input_dir = input_dir_for_run(run, settings)
    pipeline = _metadata_pipeline(run_dir)
    original_value = pipeline.get("original_source_path")
    original_source = Path(str(original_value)) if original_value else None
    if not _clips_exist(run_dir):
        return _confirmation_error("invalid_phase", "Run does not have rendered clips")
    if (
        not target.exists()
        or not _path_is_relative_to(target, input_dir)
        or (original_source is not None and target.resolve() == original_source.resolve())
    ):
        return _confirmation_error("path_rejected", f"Local source target is protected or invalid: {target}")
    target.unlink()
    return _confirmation_ok(action="delete_local_source", deleted=[str(target)], target_path=str(target))


def read_event_tail(
    service_dir: Path = DEFAULT_SERVICE_DIR,
    *,
    run_id: str | None = None,
    max_events: int = 50,
) -> list[dict[str, Any]]:
    path = _events_path(service_dir)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if run_id is not None and event.get("run_id") != run_id:
            continue
        events.append(event)
    return events[-max_events:]


def create_confirmation(
    *,
    action: str,
    run_id: str,
    target_path: Path,
    reason: str,
    risk_level: str,
    validation: dict[str, Any],
    service_dir: Path = DEFAULT_SERVICE_DIR,
    created_by: str = "mcp",
) -> dict[str, Any]:
    confirmations = load_confirmations(service_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    confirmation = {
        "id": f"confirm_{timestamp}_{len(confirmations) + 1:03d}",
        "action": action,
        "run_id": run_id,
        "target_path": str(target_path),
        "reason": reason,
        "risk_level": risk_level,
        "created_by": created_by,
        "created_at": now_utc(),
        "status": "pending",
        "validation": validation,
    }
    confirmations.append(confirmation)
    save_confirmations(confirmations, service_dir)
    append_event(
        service_dir,
        "confirmation_created",
        run_id=run_id,
        confirmation_id=confirmation["id"],
        action=action,
        target_path=str(target_path),
        risk_level=risk_level,
        created_by=created_by,
    )
    return confirmation


def _read_pid(service_dir: Path) -> int | None:
    path = _pid_path(service_dir)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _write_service_state(service_dir: Path, state: dict[str, Any]) -> None:
    write_json(_service_path(ensure_dir(service_dir)), state)


def _service_state(status: str, pid: int | None, settings: Settings) -> dict[str, Any]:
    now = now_utc()
    next_scan_at = (
        datetime.now(UTC) + timedelta(minutes=settings.service.scan_interval_minutes)
    ).isoformat()
    return {
        "status": status,
        "pid": pid,
        "started_at": now if status == "running" else None,
        "last_heartbeat_at": now,
        "next_scan_at": next_scan_at if status == "running" else None,
        "config_snapshot": {
            "source_id": settings.recording_source_default.source_id,
            "source_dir": str(settings.recording_source_default.source_dir)
            if settings.recording_source_default.source_dir
            else None,
            "scan_interval_minutes": settings.service.scan_interval_minutes,
        },
        "last_error": None,
    }


def _known_content_ids(runs: list[dict[str, Any]]) -> set[str]:
    return {str(run.get("content_id")) for run in runs if run.get("content_id")}


def migrate_run_content_ids(
    runs: list[dict[str, Any]],
    *,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> int:
    migrated = 0
    for run in runs:
        if run.get("content_id"):
            continue
        local_source = Path(str(run.get("local_source_path") or ""))
        original_source = Path(str(run.get("source_path") or ""))
        source_path = local_source if local_source.is_file() else original_source
        if not source_path.is_file():
            continue
        try:
            identity = content_identity(source_path, service_dir=service_dir)
        except SourceChangedDuringHash:
            continue
        run["content_id"] = identity["content_id"]
        run["source_bytes"] = identity["bytes"]
        run["first_source_path"] = str(run.get("source_path") or source_path)
        run["last_source_path"] = str(run.get("source_path") or source_path)
        run["discovered_at"] = run.get("created_at") or now_utc()
        migrated += 1
        append_event(service_dir, "run_content_id_migrated", run_id=run.get("run_id"))
    return migrated


def _start_pipeline_process(
    source_path: Path,
    *,
    input_dir: Path,
    run_dir: Path,
    log_path: Path,
) -> int:
    command = self_command(
        "pipeline",
        str(source_path),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(run_dir),
    )
    ensure_dir(log_path.parent)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


def _phase_from_files(run_dir: Path) -> str | None:
    clips_dir = run_dir / "clips"
    clips = sorted(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    if clips:
        return "rendered"
    if (run_dir / "selected_clips.json").exists():
        return "rendering"
    if (run_dir / "codex_brief.json").exists():
        return "needs_review"
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """把 run 里的时间戳（ISO 字符串或 Unix 秒）解析为带时区的 datetime；失败返回 None。"""
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


def _run_is_stuck(run: dict[str, Any], settings: Settings) -> bool:
    """run 是否已在当前阶段停留超过 service.stuck_after_minutes 分钟。"""
    threshold_minutes = settings.service.stuck_after_minutes
    if threshold_minutes <= 0:
        return False
    started = _parse_timestamp(run.get("updated_at")) or _parse_timestamp(run.get("created_at"))
    if started is None:
        return False
    return datetime.now(UTC) - started >= timedelta(minutes=threshold_minutes)


def reconcile_run(run: dict[str, Any], settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> bool:
    old_phase = run.get("phase")
    run_dir = Path(str(run["run_dir"]))
    pid = run.get("pid")
    inferred = _phase_from_files(run_dir)

    process_running = isinstance(pid, int) and pid_is_running(pid)
    if process_running:
        # 防御性兜底：进程疑似仍存活，但只要流水线产物已生成（inferred 非空）且该 run
        # 停在 processing 已超过阈值，就不再盲信 pid，让卡死/僵尸进程无法再冻结任务。
        if inferred is None or not _run_is_stuck(run, settings):
            return False
        append_event(
            service_dir,
            "stuck_run_recovered",
            run_id=run["run_id"],
            pid=pid,
            inferred_phase=inferred,
        )

    if isinstance(pid, int):
        run["pid"] = None

    if inferred == "rendering" and settings.service.auto_render_after_selection:
        run["phase"] = "rendering"
        append_event(service_dir, "render_started", run_id=run["run_id"], run_dir=str(run_dir))
        render_selected_clips(run_dir / "selected_clips.json")
        cleanup_local_artifacts(
            run_dir,
            input_dir=input_dir_for_run(run, settings),
            confirm=False,
        )
        run["phase"] = "rendered"
        append_event(service_dir, "render_completed", run_id=run["run_id"], run_dir=str(run_dir))
        append_event(service_dir, "cleanup_preview_created", run_id=run["run_id"], run_dir=str(run_dir))
    elif inferred:
        run["phase"] = inferred
    elif run.get("phase") == "processing":
        run["phase"] = "failed"
        run["last_error"] = "Pipeline stopped before codex_brief.json was created"

    changed = run.get("phase") != old_phase or (isinstance(pid, int) and run.get("pid") is None)
    if changed:
        run["updated_at"] = now_utc()
        append_event(
            service_dir,
            "phase_changed",
            run_id=run["run_id"],
            phase=run["phase"],
            run_dir=str(run_dir),
        )
    return changed


def _create_queued_run(
    source_path: Path,
    *,
    identity: dict[str, Any],
    settings: Settings,
    service_dir: Path,
) -> dict[str, Any]:
    source_config = settings.recording_source_default
    run_identity = build_run_identity(
        source_config.source_id,
        source_path,
        output_root=source_config.output_root,
        input_dir=source_config.input_dir,
        workspace_root=settings.paths.workspace_root,
        content_id=str(identity["content_id"]),
    )
    created_at = now_utc()
    run = {
        "run_id": run_identity["run_id"],
        "source_id": source_config.source_id,
        "source_path": str(source_path),
        "first_source_path": str(source_path),
        "last_source_path": str(source_path),
        "local_source_path": None,
        "workspace_dir": str(run_identity["workspace_dir"]) if run_identity["workspace_dir"] else None,
        "input_dir": str(Path(run_identity["input_dir"]).resolve()),
        "run_dir": str(run_identity["run_dir"]),
        "fingerprint": run_identity["fingerprint"],
        "content_id": identity["content_id"],
        "source_bytes": identity["bytes"],
        "phase": "queued",
        "pid": None,
        "log_path": str(service_dir / "runs" / f"{run_identity['run_id']}.log"),
        "discovered_at": created_at,
        "created_at": created_at,
        "updated_at": created_at,
        "last_error": None,
    }
    append_event(service_dir, "recording_discovered", run_id=run["run_id"], source_path=str(source_path))
    append_event(service_dir, "recording_queued", run_id=run["run_id"])
    return run


def _launch_queued_run(
    run: dict[str, Any],
    *,
    settings: Settings,
    service_dir: Path,
) -> dict[str, Any]:
    require_pipeline_configuration(settings)
    local_source = Path(str(run.get("local_source_path") or ""))
    original_source = Path(str(run.get("source_path") or ""))
    source_path = local_source if local_source.is_file() else original_source
    if not source_path.is_file():
        raise FileNotFoundError("source_unavailable")
    run["phase"] = "staging"
    run["updated_at"] = now_utc()
    append_event(service_dir, "staging_started", run_id=run["run_id"], source_path=str(source_path))
    local_source_path = stage_source_file(source_path, input_dir=Path(str(run["input_dir"])))
    run["local_source_path"] = str(local_source_path)
    pid = _start_pipeline_process(
        source_path,
        input_dir=Path(str(run["input_dir"])),
        run_dir=Path(str(run["run_dir"])),
        log_path=Path(str(run["log_path"])),
    )
    run["phase"] = "processing"
    run["pid"] = pid
    run["updated_at"] = now_utc()
    append_event(service_dir, "pipeline_started", run_id=run["run_id"], pid=pid, run_dir=run["run_dir"])
    return run


def dispatch_queued_runs(
    runs: list[dict[str, Any]],
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> list[dict[str, Any]]:
    require_pipeline_configuration(settings)
    active_count = sum(1 for run in runs if run.get("phase") == "processing")
    capacity = max(0, MAX_CONCURRENT_PIPELINES - active_count)
    started: list[dict[str, Any]] = []
    queued = sorted(
        (run for run in runs if run.get("phase") == "queued"),
        key=lambda run: str(run.get("discovered_at") or run.get("created_at") or ""),
    )
    for run in queued[:capacity]:
        try:
            _launch_queued_run(run, settings=settings, service_dir=service_dir)
        except FileNotFoundError:
            run["phase"] = "failed"
            run["pid"] = None
            run["last_error"] = "原录像和本地副本均不可用，无法启动该任务。"
            run["updated_at"] = now_utc()
            append_event(service_dir, "queued_source_unavailable", run_id=run.get("run_id"))
            continue
        started.append(run)
    return started


def _start_run_for_source(
    source_path: Path,
    *,
    settings: Settings,
    service_dir: Path,
) -> dict[str, Any]:
    require_pipeline_configuration(settings)
    identity = content_identity(source_path, service_dir=service_dir)
    run = _create_queued_run(
        source_path,
        identity=identity,
        settings=settings,
        service_dir=service_dir,
    )
    return _launch_queued_run(run, settings=settings, service_dir=service_dir)


def start_run_for_source(
    source_path: Path,
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    validate_service_settings(settings)
    require_pipeline_configuration(settings)
    runs = load_runs(service_dir)
    migrate_run_content_ids(runs, service_dir=service_dir)
    identity = content_identity(source_path, service_dir=service_dir)
    if identity["content_id"] in _known_content_ids(runs):
        raise ValueError("duplicate_run")
    run = _create_queued_run(
        source_path,
        identity=identity,
        settings=settings,
        service_dir=service_dir,
    )
    runs.append(run)
    dispatch_queued_runs(runs, settings=settings, service_dir=service_dir)
    save_runs(runs, service_dir)
    return run


def retry_failed_run(
    run_id: str,
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    runs = load_runs(service_dir)
    run = next((item for item in runs if item.get("run_id") == run_id), None)
    if run is None:
        raise ValueError("run_not_found")
    if run.get("phase") != "failed":
        raise ValueError("invalid_phase")
    try:
        require_pipeline_configuration(settings)
    except PipelineConfigurationError:
        append_event(service_dir, "pipeline_configuration_blocked", trigger="retry", run_id=run_id)
        raise

    local_source = Path(str(run.get("local_source_path") or ""))
    original_source = Path(str(run.get("source_path") or ""))
    if local_source.is_file():
        source_path = local_source
    elif original_source.is_file():
        source_path = original_source
    else:
        raise FileNotFoundError("source_unavailable")

    run["source_path"] = str(run.get("source_path") or source_path)
    if local_source.is_file():
        run["local_source_path"] = str(local_source)
    run["phase"] = "queued"
    run["pid"] = None
    run["last_error"] = None
    run["retry_count"] = int(run.get("retry_count") or 0) + 1
    run["updated_at"] = now_utc()
    run["discovered_at"] = run.get("discovered_at") or run.get("created_at") or run["updated_at"]
    started = dispatch_queued_runs(runs, settings=settings, service_dir=service_dir)
    save_runs(runs, service_dir)
    append_event(
        service_dir,
        "pipeline_retried" if run in started else "pipeline_retry_queued",
        run_id=run_id,
        pid=run.get("pid"),
        run_dir=str(run.get("run_dir")),
    )
    return run


def run_service_once(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    runs = load_runs(service_dir)
    for run in runs:
        reconcile_run(run, settings, service_dir=service_dir)
    migrated_runs = migrate_run_content_ids(runs, service_dir=service_dir)
    # 先把 reconcile 结果落盘：即使随后扫描录播源失败（如 NAS 未挂载），
    # 也不能丢掉状态推进。
    if runs:
        save_runs(runs, service_dir)
    try:
        require_pipeline_configuration(settings)
    except PipelineConfigurationError:
        append_event(service_dir, "pipeline_configuration_blocked", trigger="scan")
        raise

    discovered: list[dict[str, Any]] = []
    scan_error: str | None = None
    scan_report: dict[str, Any] = {
        "eligible": [],
        "unsupported_files": 0,
        "too_new_files": 0,
        "unstable_files": 0,
    }
    try:
        scan_report = scan_recording_source_report(settings.recording_source_default)
    except FileNotFoundError as exc:
        scan_error = str(exc)
        append_event(service_dir, "recording_source_unavailable", source_dir=str(exc))
    known_by_content = {str(run["content_id"]): run for run in runs if run.get("content_id")}
    duplicate_files = 0
    hash_cache_hits = 0
    hashes_computed = 0
    for source_path in scan_report["eligible"]:
        try:
            identity = content_identity(source_path, service_dir=service_dir)
        except SourceChangedDuringHash:
            scan_report["unstable_files"] += 1
            continue
        if identity["cache_hit"]:
            hash_cache_hits += 1
        else:
            hashes_computed += 1
        existing = known_by_content.get(str(identity["content_id"]))
        if existing is not None:
            existing["last_source_path"] = str(source_path)
            duplicate_files += 1
            continue
        run = _create_queued_run(
            source_path,
            identity=identity,
            settings=settings,
            service_dir=service_dir,
        )
        runs.append(run)
        discovered.append(run)
        known_by_content[str(identity["content_id"])] = run

    started = dispatch_queued_runs(runs, settings=settings, service_dir=service_dir)
    if runs:
        save_runs(runs, service_dir)
    queued_runs = sum(1 for run in runs if run.get("phase") == "queued")
    if scan_error:
        message = f"录像目录不可用：{scan_error}"
    elif discovered:
        message = f"发现 {len(discovered)} 个未处理录像，已开始 {len(started)} 个，排队 {queued_runs} 个。"
    else:
        message = (
            f"没有发现未处理录像：已处理或已排队 {duplicate_files} 个，"
            f"过新 {scan_report['too_new_files']} 个，写入中 {scan_report['unstable_files']} 个。"
        )
    return {
        "ok": True,
        "known_runs": len(runs),
        "discovered_runs": len(discovered),
        "started_runs": len(started),
        "queued_runs": queued_runs,
        "duplicate_files": duplicate_files,
        "too_new_files": scan_report["too_new_files"],
        "unstable_files": scan_report["unstable_files"],
        "unsupported_files": scan_report["unsupported_files"],
        "hash_cache_hits": hash_cache_hits,
        "hashes_computed": hashes_computed,
        "migrated_runs": migrated_runs,
        "scan_error": scan_error,
        "message": message,
        "service_dir": str(service_dir),
    }


def run_service_tick(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    runs = load_runs(service_dir)
    changed_runs = 0
    for run in runs:
        if reconcile_run(run, settings, service_dir=service_dir):
            changed_runs += 1
    queued_started: list[dict[str, Any]] = []
    if any(run.get("phase") == "queued" for run in runs):
        try:
            queued_started = dispatch_queued_runs(runs, settings=settings, service_dir=service_dir)
        except PipelineConfigurationError:
            append_event(service_dir, "pipeline_configuration_blocked", trigger="queue_dispatch")
    save_runs(runs, service_dir)

    from . import scheduler

    scheduler_report = scheduler.tick_scheduler(settings, service_dir=service_dir)
    return {
        "ok": True,
        "known_runs": len(runs),
        "changed_runs": changed_runs,
        "started_queued_runs": len(queued_started),
        "queued_runs": sum(1 for run in runs if run.get("phase") == "queued"),
        "scheduler": scheduler_report,
        "service_dir": str(service_dir),
    }


def service_loop(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> None:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    pid = os.getpid()
    _pid_path(service_dir).write_text(f"{pid}\n", encoding="utf-8")
    _write_service_state(service_dir, _service_state("running", pid, settings))
    append_event(service_dir, "service_started", pid=pid)
    while True:
        try:
            report = run_service_tick(settings, service_dir=service_dir)
            state = _service_state("running", pid, settings)
            state["last_report"] = report
            _write_service_state(service_dir, state)
            time.sleep(settings.scheduler.tick_seconds)
        except KeyboardInterrupt:
            break
        except Exception as exc:  # pragma: no cover - defensive for long-running service
            state = _service_state("running", pid, settings)
            state["last_error"] = str(exc)
            _write_service_state(service_dir, state)
            append_event(service_dir, "service_error", error=str(exc))
            time.sleep(min(settings.scheduler.tick_seconds, 60))
    _write_service_state(service_dir, {"status": "stopped", "pid": pid, "stopped_at": now_utc()})
    append_event(service_dir, "service_stopped", pid=pid)


def start_service(
    settings: Settings,
    *,
    service_dir: Path = DEFAULT_SERVICE_DIR,
    foreground: bool = False,
    once: bool = False,
) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    existing_pid = _read_pid(service_dir)
    if existing_pid is not None and pid_is_running(existing_pid) and not foreground and not once:
        return {
            "ok": True,
            "started": False,
            "reason": "service_already_running",
            "pid": existing_pid,
            "service_dir": str(service_dir),
        }

    if once:
        pid = os.getpid()
        _pid_path(service_dir).write_text(f"{pid}\n", encoding="utf-8")
        _write_service_state(service_dir, _service_state("running", pid, settings))
        report = run_service_once(settings, service_dir=service_dir)
        _write_service_state(service_dir, {"status": "stopped", "pid": pid, "stopped_at": now_utc(), "last_report": report})
        return {
            "ok": True,
            "started": True,
            "once": True,
            "pid": pid,
            "report": report,
            "service_dir": str(service_dir),
        }

    if foreground:
        service_loop(settings, service_dir=service_dir)
        return {
            "ok": True,
            "started": True,
            "foreground": True,
            "pid": os.getpid(),
            "service_dir": str(service_dir),
        }

    command = self_command("service", "start", "--foreground")
    log_path = _service_log_path(service_dir)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _pid_path(service_dir).write_text(f"{process.pid}\n", encoding="utf-8")
    _write_service_state(service_dir, _service_state("running", process.pid, settings))
    append_event(service_dir, "service_started", pid=process.pid)
    return {
        "ok": True,
        "started": True,
        "pid": process.pid,
        "service_dir": str(service_dir),
        "log_path": str(log_path),
    }


def stop_service(*, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    ensure_dir(service_dir)
    pid = _read_pid(service_dir)
    if pid is None:
        _write_service_state(service_dir, {"status": "stopped", "pid": None, "stopped_at": now_utc()})
        return {"ok": True, "stopped": False, "reason": "service_not_running"}
    running = pid_is_running(pid)
    if running:
        os.kill(pid, signal.SIGTERM)
    _write_service_state(service_dir, {"status": "stopped", "pid": pid, "stopped_at": now_utc()})
    append_event(service_dir, "service_stopped", pid=pid)
    return {
        "ok": True,
        "stopped": running,
        "pid": pid,
        "service_dir": str(service_dir),
    }


def get_service_status(*, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    state_path = _service_path(service_dir)
    state = read_json(state_path) if state_path.exists() else {"status": "stopped", "pid": None}
    pid = state.get("pid")
    running = isinstance(pid, int) and pid_is_running(pid)
    runs = load_runs(service_dir)
    counts = dict(sorted(Counter(str(run.get("phase", "unknown")) for run in runs).items()))
    return {
        "ok": True,
        "running": running,
        "service": state,
        "runs": runs,
        "phase_counts": counts,
        "active_run": next((run["run_id"] for run in runs if run.get("phase") == "processing"), None),
        "queued_runs": [run["run_id"] for run in runs if run.get("phase") == "queued"],
        "pending_review_runs": [run["run_id"] for run in runs if run.get("phase") == "needs_review"],
        "rendered_runs": [run["run_id"] for run in runs if run.get("phase") == "rendered"],
        "failed_runs": [run["run_id"] for run in runs if run.get("phase") == "failed"],
        "service_dir": str(service_dir),
    }


def read_service_logs(*, service_dir: Path = DEFAULT_SERVICE_DIR, max_lines: int = 200) -> str:
    path = _service_log_path(service_dir)
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])
