"""Codex automation helpers for scheduled NAS recording workflows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .status import build_run_status
from .utils import ensure_dir, read_json, write_json


DEFAULT_NAS_DIR = Path("/Volumes/homes/weixiaodan12/录播")
DEFAULT_STATE_DIR = Path("work") / "automation_state"
DEFAULT_LOG_DIR = Path("work") / "automation_logs"
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_id_for_source(source_path: Path) -> str:
    return source_path.stem


def _tail_text(path: Path, *, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def find_latest_recording(
    source_dir: Path = DEFAULT_NAS_DIR,
    *,
    since_hours: int = 36,
    min_age_minutes: int = 10,
) -> Path | None:
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)

    now = datetime.now()
    earliest_mtime = now - timedelta(hours=since_hours)
    latest_allowed_mtime = now - timedelta(minutes=min_age_minutes)
    candidates = []
    for path in source_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if earliest_mtime <= mtime <= latest_allowed_mtime:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def start_latest_recording_job(
    source_dir: Path = DEFAULT_NAS_DIR,
    *,
    input_dir: Path = Path("input"),
    output_root: Path = Path("output"),
    state_dir: Path = DEFAULT_STATE_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    since_hours: int = 36,
    min_age_minutes: int = 10,
    refine: bool = True,
    top_n: int = 25,
    correct_transcript: bool = False,
) -> dict[str, Any]:
    source_path = find_latest_recording(
        source_dir,
        since_hours=since_hours,
        min_age_minutes=min_age_minutes,
    )
    if source_path is None:
        report = {
            "ok": True,
            "started": False,
            "reason": "没有发现满足时间窗口且已稳定的录像文件",
            "source_dir": str(source_dir),
            "checked_at": _now_utc(),
        }
        write_json(ensure_dir(state_dir) / "last_start_attempt.json", report)
        return report

    run_id = _run_id_for_source(source_path)
    run_dir = output_root / run_id
    state_path = ensure_dir(state_dir) / f"{run_id}.json"
    log_path = ensure_dir(log_dir) / f"{run_id}.log"
    if state_path.exists():
        state = read_json(state_path)
        pid = state.get("pid")
        if isinstance(pid, int) and _pid_is_running(pid):
            return {
                "ok": True,
                "started": False,
                "reason": "已有后台任务正在运行",
                "state_path": str(state_path),
                "pid": pid,
                "run_dir": str(run_dir),
            }

    status = build_run_status(run_dir, write_report=False)
    if status["exists"] and status["files"]["codex_brief.json"]["exists"]:
        return {
            "ok": True,
            "started": False,
            "reason": "候选包已经存在，避免重复启动",
            "run_dir": str(run_dir),
            "next_step": status["next_step"],
        }

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
        "--top-n",
        str(top_n),
    ]
    if refine:
        command.append("--refine")
    if correct_transcript:
        command.append("--correct-transcript")

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    state = {
        "run_id": run_id,
        "phase": "running",
        "requires_codex": False,
        "source_path": str(source_path),
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "pid": process.pid,
        "command": command,
        "started_at": _now_utc(),
        "updated_at": _now_utc(),
    }
    write_json(state_path, state)
    return {
        "ok": True,
        "started": True,
        "pid": process.pid,
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "state_path": str(state_path),
    }


def _write_codex_task(run_dir: Path, *, phase: str, log_path: Path | None = None) -> Path:
    if phase == "needs_codex_selection":
        body = "\n".join([
            "# Codex 任务：审阅直播切片候选",
            "",
            "请读取本目录下的 `codex_brief.json` 和 `refined_candidates.json`，选择适合发布的直播切片。",
            "",
            "输出要求：",
            "- 写入 `selected_clips.json`。",
            "- 优先选择开头快、上下文完整、能独立成立的片段。",
            "- 如片段中有明显废话或等待，可用 `remove_ranges` 精修。",
            "",
            "完成后下一步命令：",
            "",
            "```bash",
            f".venv/bin/live-clipper render {run_dir / 'selected_clips.json'}",
            "```",
        ])
    elif phase == "failed_needs_codex":
        body = "\n".join([
            "# Codex 任务：诊断流水线失败",
            "",
            "后台流水线没有生成 `codex_brief.json`，请检查日志和断点文件，判断能否 resume。",
            "",
            f"- 任务目录：`{run_dir}`",
            f"- 日志文件：`{log_path}`" if log_path else "- 日志文件：未记录",
            "",
            "优先处理方式：",
            "- 不要删除 NAS 原始录像。",
            "- 能断点续跑就给出或执行 resume 命令。",
            "- 如果是 Agnes 网络错误，优先重试原命令。",
        ])
    elif phase == "cleanup_ready":
        body = "\n".join([
            "# Codex 任务：确认本地大文件清理",
            "",
            "成片已经渲染完成，请先执行 cleanup 预演，确认只会删除本地 input 副本和中间音频。",
            "",
            "```bash",
            f".venv/bin/live-clipper cleanup {run_dir}",
            f".venv/bin/live-clipper cleanup {run_dir} --confirm",
            "```",
        ])
    else:
        body = f"# Codex 任务\n\n当前阶段：`{phase}`\n"

    task_path = run_dir / "codex_task.md"
    task_path.write_text(body, encoding="utf-8")
    return task_path


def check_automation_runs(
    output_root: Path = Path("output"),
    *,
    state_dir: Path = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    requires_codex = []
    if not output_root.exists():
        return {
            "ok": True,
            "requires_codex": False,
            "message": "output 目录不存在，暂无任务",
            "runs": runs,
        }

    state_by_run_id: dict[str, dict[str, Any]] = {}
    if state_dir.exists():
        for state_path in state_dir.glob("*.json"):
            if state_path.name == "last_start_attempt.json":
                continue
            state = read_json(state_path)
            state_by_run_id[state_path.stem] = state

    for run_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        status = build_run_status(run_dir)
        state = state_by_run_id.get(run_dir.name, {})
        log_path = Path(state["log_path"]) if state.get("log_path") else None
        pid = state.get("pid")
        running = isinstance(pid, int) and _pid_is_running(pid)
        files = status["files"]
        phase = "running" if running else "unknown"
        needs_codex = False
        task_path = None

        if files["selected_clips.json"]["exists"] and files["clips"]["count"] > 0:
            phase = "cleanup_ready"
            needs_codex = True
            task_path = _write_codex_task(run_dir, phase=phase, log_path=log_path)
        elif files["codex_brief.json"]["exists"] and not files["selected_clips.json"]["exists"]:
            phase = "needs_codex_selection"
            needs_codex = True
            task_path = _write_codex_task(run_dir, phase=phase, log_path=log_path)
        elif not running and state and not files["codex_brief.json"]["exists"]:
            phase = "failed_needs_codex"
            needs_codex = True
            task_path = _write_codex_task(run_dir, phase=phase, log_path=log_path)
        elif not running and files["selected_clips.json"]["exists"]:
            phase = "ready_to_render"
        elif not running:
            phase = "waiting_or_manual"

        run_report = {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "phase": phase,
            "requires_codex": needs_codex,
            "next_step": status["next_step"],
            "pid": pid,
            "running": running,
            "log_path": str(log_path) if log_path else None,
            "codex_task_file": str(task_path) if task_path else None,
            "log_tail": _tail_text(log_path) if log_path and needs_codex and phase == "failed_needs_codex" else "",
        }
        runs.append(run_report)
        if needs_codex:
            requires_codex.append(run_report)

        if state:
            state.update({
                "phase": phase,
                "requires_codex": needs_codex,
                "codex_task_file": str(task_path) if task_path else None,
                "updated_at": _now_utc(),
            })
            write_json(state_dir / f"{run_dir.name}.json", state)

    return {
        "ok": True,
        "requires_codex": bool(requires_codex),
        "message": "发现需要 Codex 处理的任务" if requires_codex else "暂无需要 Codex 处理的任务",
        "runs": runs,
        "codex_tasks": requires_codex,
    }
