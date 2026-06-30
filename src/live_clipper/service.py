"""Local long-running service core for live-clipper."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .automation import SUPPORTED_VIDEO_EXTENSIONS
from .config import RecordingSourceDefaultConfig, Settings
from .pipeline import cleanup_local_artifacts, stage_source_file
from .render_clips import render_selected_clips
from .utils import ensure_dir, read_json, write_json

DEFAULT_SERVICE_DIR = Path("work") / "service"


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def pid_is_running(pid: int) -> bool:
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


def build_run_identity(source_id: str, source_path: Path, *, output_root: Path) -> dict[str, Any]:
    stat = source_path.stat()
    raw = f"{source_id}|{source_path}|{stat.st_size}|{stat.st_mtime_ns}"
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    run_id = f"{source_path.stem}__{fingerprint}"
    return {
        "source_id": source_id,
        "fingerprint": fingerprint,
        "run_id": run_id,
        "run_dir": output_root / source_id / run_id,
    }


def _is_stable_file(path: Path, stable_check_seconds: int) -> bool:
    before = path.stat()
    if stable_check_seconds > 0:
        time.sleep(stable_check_seconds)
    after = path.stat()
    return before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns


def scan_recording_source(config: RecordingSourceDefaultConfig) -> list[Path]:
    if config.source_dir is None:
        return []
    if not config.source_dir.exists():
        raise FileNotFoundError(config.source_dir)

    now = datetime.now()
    earliest_mtime = now - timedelta(hours=config.since_hours)
    latest_allowed_mtime = now - timedelta(minutes=config.min_age_minutes)
    candidates = []
    for path in sorted(config.source_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if not (earliest_mtime <= mtime <= latest_allowed_mtime):
            continue
        if not _is_stable_file(path, config.stable_check_seconds):
            continue
        candidates.append(path)
    return candidates


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


def append_event(service_dir: Path, event_type: str, **payload: Any) -> None:
    ensure_dir(service_dir)
    event = {
        "type": event_type,
        "created_at": now_utc(),
        **payload,
    }
    with _events_path(service_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


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


def _known_fingerprints(runs: list[dict[str, Any]]) -> set[str]:
    return {str(run.get("fingerprint")) for run in runs if run.get("fingerprint")}


def _start_pipeline_process(
    source_path: Path,
    *,
    input_dir: Path,
    run_dir: Path,
    log_path: Path,
) -> int:
    command = [
        sys.executable,
        "-m",
        "live_clipper",
        "pipeline",
        str(source_path),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(run_dir),
    ]
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


def reconcile_run(run: dict[str, Any], settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> bool:
    old_phase = run.get("phase")
    run_dir = Path(str(run["run_dir"]))
    pid = run.get("pid")
    if isinstance(pid, int) and pid_is_running(pid):
        return False
    if isinstance(pid, int):
        run["pid"] = None

    inferred = _phase_from_files(run_dir)
    if inferred == "rendering" and settings.service.auto_render_after_selection:
        run["phase"] = "rendering"
        append_event(service_dir, "render_started", run_id=run["run_id"], run_dir=str(run_dir))
        render_selected_clips(run_dir / "selected_clips.json")
        cleanup_local_artifacts(
            run_dir,
            input_dir=settings.recording_source_default.input_dir,
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


def _start_run_for_source(
    source_path: Path,
    *,
    settings: Settings,
    service_dir: Path,
) -> dict[str, Any]:
    source_config = settings.recording_source_default
    identity = build_run_identity(
        source_config.source_id,
        source_path,
        output_root=source_config.output_root,
    )
    created_at = now_utc()
    run = {
        "run_id": identity["run_id"],
        "source_id": source_config.source_id,
        "source_path": str(source_path),
        "local_source_path": None,
        "run_dir": str(identity["run_dir"]),
        "fingerprint": identity["fingerprint"],
        "phase": "discovered",
        "pid": None,
        "log_path": str(service_dir / "runs" / f"{identity['run_id']}.log"),
        "created_at": created_at,
        "updated_at": created_at,
        "last_error": None,
    }
    append_event(service_dir, "recording_discovered", run_id=run["run_id"], source_path=str(source_path))
    run["phase"] = "staging"
    run["updated_at"] = now_utc()
    append_event(service_dir, "staging_started", run_id=run["run_id"], source_path=str(source_path))
    local_source_path = stage_source_file(source_path, input_dir=source_config.input_dir)
    run["local_source_path"] = str(local_source_path)
    pid = _start_pipeline_process(
        source_path,
        input_dir=source_config.input_dir,
        run_dir=Path(str(run["run_dir"])),
        log_path=Path(str(run["log_path"])),
    )
    run["phase"] = "processing"
    run["pid"] = pid
    run["updated_at"] = now_utc()
    append_event(service_dir, "pipeline_started", run_id=run["run_id"], pid=pid, run_dir=run["run_dir"])
    return run


def start_run_for_source(
    source_path: Path,
    *,
    settings: Settings,
    service_dir: Path = DEFAULT_SERVICE_DIR,
) -> dict[str, Any]:
    validate_service_settings(settings)
    runs = load_runs(service_dir)
    identity = build_run_identity(
        settings.recording_source_default.source_id,
        source_path,
        output_root=settings.recording_source_default.output_root,
    )
    if identity["fingerprint"] in _known_fingerprints(runs):
        raise ValueError("duplicate_run")
    run = _start_run_for_source(source_path, settings=settings, service_dir=service_dir)
    runs.append(run)
    save_runs(runs, service_dir)
    return run


def run_service_once(settings: Settings, *, service_dir: Path = DEFAULT_SERVICE_DIR) -> dict[str, Any]:
    validate_service_settings(settings)
    ensure_dir(service_dir)
    runs = load_runs(service_dir)
    for run in runs:
        reconcile_run(run, settings, service_dir=service_dir)

    started = []
    known = _known_fingerprints(runs)
    for source_path in scan_recording_source(settings.recording_source_default):
        identity = build_run_identity(
            settings.recording_source_default.source_id,
            source_path,
            output_root=settings.recording_source_default.output_root,
        )
        if identity["fingerprint"] in known:
            continue
        run = _start_run_for_source(source_path, settings=settings, service_dir=service_dir)
        runs.append(run)
        started.append(run)
        known.add(run["fingerprint"])

    save_runs(runs, service_dir)
    return {
        "ok": True,
        "known_runs": len(runs),
        "started_runs": len(started),
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
            report = run_service_once(settings, service_dir=service_dir)
            state = _service_state("running", pid, settings)
            state["last_report"] = report
            _write_service_state(service_dir, state)
            time.sleep(settings.service.scan_interval_minutes * 60)
        except KeyboardInterrupt:
            break
        except Exception as exc:  # pragma: no cover - defensive for long-running service
            state = _service_state("running", pid, settings)
            state["last_error"] = str(exc)
            _write_service_state(service_dir, state)
            append_event(service_dir, "service_error", error=str(exc))
            time.sleep(min(settings.service.scan_interval_minutes * 60, 60))
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

    command = [
        sys.executable,
        "-m",
        "live_clipper",
        "service",
        "start",
        "--foreground",
    ]
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
