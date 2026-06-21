"""Run status and reporting helpers."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .utils import read_json, write_json


def _count_json_items(path: Path) -> int | None:
    if not path.exists():
        return None
    data = read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        if isinstance(data.get("sentences"), list):
            return len(data["sentences"])
        if isinstance(data.get("segments"), list):
            return len(data["segments"])
        if isinstance(data.get("candidates"), list):
            return len(data["candidates"])
    return None


def _file_status(run_dir: Path, name: str) -> dict[str, Any]:
    path = run_dir / name
    status: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if path.exists():
        status["bytes"] = path.stat().st_size
        count = _count_json_items(path) if path.suffix == ".json" else None
        if count is not None:
            status["count"] = count
    return status


def _infer_next_step(files: dict[str, dict[str, Any]]) -> str:
    if not files["transcript_raw.json"]["exists"]:
        return "运行 scan 生成 ASR 原始转写"
    if not files["transcript.json"]["exists"]:
        return "运行 scan --resume 生成 transcript.json"
    if not files["windows.json"]["exists"]:
        return "运行 scan --resume 生成 windows.json"
    if not files["cheap_candidates.json"]["exists"]:
        return "运行 scan --resume，让 Agnes 粗扫候选片段"
    if not files["merged_candidates.json"]["exists"]:
        return "运行 scan --resume 合并候选片段"
    if files["selected_clips.json"]["exists"] and files["clips"]["exists"] and files["clips"].get("count", 0) > 0:
        return "已完成"
    if files["selected_clips.json"]["exists"]:
        return "运行 render 渲染 selected_clips.json"
    if not files["refined_candidates.json"]["exists"]:
        return "运行 refine，让 Agnes 二次复评候选"
    if not files["codex_brief.json"]["exists"]:
        return "运行 brief --source refined 生成精选候选包"
    if not files["selected_clips.json"]["exists"]:
        return "审阅 codex_brief.json，并写入 selected_clips.json"
    return "已完成"


def _clip_count(run_dir: Path) -> dict[str, Any]:
    clips_dir = run_dir / "clips"
    mp4_files = sorted(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    return {
        "path": str(clips_dir),
        "exists": clips_dir.exists(),
        "count": len(mp4_files),
        "files": [str(path) for path in mp4_files],
    }


def _global_log_summary() -> dict[str, int]:
    counter: Counter[str] = Counter()
    logs_dir = Path("work") / "logs"
    if not logs_dir.exists():
        return {}
    for path in logs_dir.glob("*.json"):
        prefix = path.name.rsplit("_", 1)[0]
        counter[prefix] += 1
    return dict(sorted(counter.items()))


def build_run_status(run_dir: Path, *, write_report: bool = True) -> dict[str, Any]:
    tracked_files = [
        "run_metadata.json",
        "audio.wav",
        "transcript_raw.json",
        "transcript.json",
        "windows.json",
        "cheap_candidates.json",
        "merged_candidates.json",
        "refined_candidates.json",
        "codex_brief.json",
        "selected_clips.json",
        "edit_decision_list.json",
    ]
    files = {name: _file_status(run_dir, name) for name in tracked_files}
    files["clips"] = _clip_count(run_dir)
    report = {
        "run_dir": str(run_dir),
        "exists": run_dir.exists(),
        "files": files,
        "log_summary": _global_log_summary(),
        "next_step": _infer_next_step(files) if run_dir.exists() else "运行 scan 创建新的 run 目录",
    }
    if write_report and run_dir.exists():
        write_json(run_dir / "run_report.json", report)
    return report
