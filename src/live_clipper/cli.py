from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from . import config_editor
from .ai_guide import AI_ASSISTANT_GUIDE
from .app_dirs import default_workspace_root, prepare_app_home
from .automation import DEFAULT_NAS_DIR, check_automation_runs, start_latest_recording_job
from .build_codex_brief import (
    build_codex_brief_file,
    build_codex_review_markdown,
    build_selected_clips_template,
)
from .cheap_model_client import CheapModelClient, CheapModelServiceError
from .codex_selection import validate_selected_clips_file
from .config import load_settings, render_app_config_template, write_default_config
from .correct_transcript import correct_transcript_file
from .merge_candidates import merge_candidates_file
from .models import CorrectedTranscript
from .pipeline import cleanup_local_artifacts, record_pipeline_metadata, stage_source_file
from .prompt_loader import export_prompts
from .refine_candidates import refine_candidates_file
from .render_clips import render_selected_clips
from .scan_windows import scan_windows_file
from .service import get_service_status, read_service_logs, start_embedded_service, start_service, stop_service
from .smoke import run_local_smoke
from .status import build_run_status
from .transcribe import transcribe_audio, transcript_sentences_from_raw
from .utils import ensure_dir, read_json, write_json
from .video import extract_audio
from .web import WebPaths, run_web_server
from .windows import write_windows_file

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

ENV_TEMPLATE = """# live-clipper environment
#
# 不要把真实 API key 粘贴到聊天窗口。请只在本机 .env 文件里填写。

CHEAP_MODEL_API_KEY=
ASR_API_KEY=
HF_TOKEN=
"""


def emit_progress(message: str) -> None:
    print(message, flush=True)


def resolve_glossary_path(glossary_dir: Path = Path("glossary")) -> Path:
    real_path = glossary_dir / "common_terms.json"
    if real_path.exists():
        return real_path
    return glossary_dir / "common_terms.example.json"


def run_config_init(output_path: Path = Path("live-clipper.toml"), *, force: bool = False) -> Path:
    path = write_default_config(output_path, overwrite=force)
    emit_progress(f"[配置] 已写入配置模板: {path}")
    return path


def run_prompts_export(output_dir: Path = Path("prompts.local"), *, force: bool = False) -> list[Path]:
    exported = export_prompts(output_dir, overwrite=force)
    emit_progress(f"[提示词] 已导出 {len(exported)} 个提示词到: {output_dir}")
    return exported


def run_ai_guide(output_path: Path | None = None) -> str:
    text = AI_ASSISTANT_GUIDE.rstrip() + "\n"
    if output_path is None:
        print(text, end="")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        emit_progress(f"[说明] 已写入 AI 使用说明: {output_path}")
    return text


def run_setup(
    *,
    config_path: Path = Path("live-clipper.toml"),
    env_path: Path = Path(".env"),
    prompts_dir: Path = Path("prompts.local"),
    force_config: bool = False,
    force_prompts: bool = False,
) -> dict[str, object]:
    created_dirs = []
    for directory in [
        Path("input"),
        Path("output"),
        Path("work"),
        Path("work") / "cache",
        Path("work") / "logs",
        Path("work") / "automation_state",
        prompts_dir,
    ]:
        ensure_dir(directory)
        created_dirs.append(str(directory))

    config_written = False
    if force_config or not config_path.exists():
        write_default_config(config_path, overwrite=force_config)
        config_written = True
        emit_progress(f"[初始化] 已写入配置模板: {config_path}")
    else:
        emit_progress(f"[初始化] 已保留已有配置: {config_path}")

    env_written = False
    if not env_path.exists():
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        env_written = True
        emit_progress(f"[初始化] 已写入 .env 模板: {env_path}")
    else:
        emit_progress(f"[初始化] 已保留已有 .env: {env_path}")

    prompt_files = sorted(prompts_dir.glob("*.md"))
    prompts_exported = False
    if force_prompts or not prompt_files:
        export_prompts(prompts_dir, overwrite=force_prompts)
        prompts_exported = True
        emit_progress(f"[初始化] 已导出提示词模板: {prompts_dir}")
    else:
        emit_progress(f"[初始化] 已保留已有提示词目录: {prompts_dir}")

    emit_progress("[初始化] 不要把 API key 粘贴到聊天窗口；请只写入本机 .env 文件。")
    emit_progress("[初始化] 下一步建议运行: .venv/bin/live-clipper doctor")
    return {
        "config_path": str(config_path),
        "env_path": str(env_path),
        "prompts_dir": str(prompts_dir),
        "created_dirs": created_dirs,
        "config_written": config_written,
        "env_written": env_written,
        "prompts_exported": prompts_exported,
    }


def run_app(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Desktop app mode: run out of the per-user data directory.

    Everything in this codebase resolves relative paths against cwd, so app
    mode chdirs into the app home once and reuses all existing logic.
    """
    home = prepare_app_home()
    os.chdir(home)
    config_path = home / "live-clipper.toml"
    workspace_root = default_workspace_root(home)
    if not config_path.exists():
        config_path.write_text(render_app_config_template(workspace_root), encoding="utf-8")
        emit_progress(f"[App] 已写入初始配置: {config_path}")
    else:
        migrated = config_editor.ensure_workspace_root(
            config_path=config_path,
            workspace_root=workspace_root,
            backup_root=home / "work" / "config_backups",
        )
        if not migrated["ok"]:
            raise RuntimeError(migrated["message"])
        if migrated["migrated"]:
            emit_progress(f"[App] 已升级任务工作区配置: {migrated['workspace_root']}")
    env_path = home / ".env"
    if not env_path.exists():
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        emit_progress(f"[App] 已写入 .env 模板: {env_path}")
    settings = load_settings()
    emit_progress(f"[App] 数据目录: {home}")
    start_embedded_service(load_settings, service_dir=home / "work" / "service")
    emit_progress("[App] 嵌入式服务已启动（扫描与调度随 App 运行）")
    run_web_server(
        host=host,
        port=port,
        paths=WebPaths(
            output_root=settings.paths.output_root,
            state_dir=settings.paths.state_dir,
            log_dir=Path("work") / "automation_logs",
            input_dir=settings.paths.input_dir,
        ),
        access_token=os.environ.get("LIVE_CLIPPER_WEB_TOKEN") or None,
    )


def _friendly_next_step(next_step: str) -> str:
    if "selected_clips.json" in next_step and "审阅" in next_step:
        return "等待 Codex 或人工选片：阅读 codex_brief.json，写入 selected_clips.json。"
    if "render" in next_step:
        return "可以渲染：运行 render 生成成片。"
    if "refine" in next_step:
        return "可以复评：运行 refine 提升候选质量，或直接 brief 生成审阅包。"
    if next_step == "已完成":
        return "已完成：到 clips 目录查看成片。"
    return next_step


def run_next(output_root: Path = Path("output")) -> dict[str, object]:
    run_dirs = sorted(
        [path for path in output_root.iterdir() if path.is_dir()] if output_root.exists() else [],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    reports = [build_run_status(run_dir, write_report=False) for run_dir in run_dirs]
    actionable = [report for report in reports if report["next_step"] != "已完成"]

    if not reports:
        emit_progress("[下一步] 还没有发现处理任务。请先把视频放进 input/，然后运行 scan。")
    else:
        visible_reports = actionable or reports[:1]
        emit_progress(f"[下一步] 当前发现 {len(actionable)} 个任务需要处理。")
        for report in visible_reports:
            emit_progress(f"[下一步] 任务目录: {report['run_dir']}")
            emit_progress(f"[下一步] 建议动作: {_friendly_next_step(str(report['next_step']))}")

    return {
        "output_root": str(output_root),
        "actionable_count": len(actionable),
        "runs": actionable or reports[:1],
    }


def follow_service_logs(service_dir: Path = Path("work") / "service", poll_seconds: float = 1.0) -> None:
    log_path = service_dir / "service.log"
    offset = 0
    try:
        while True:
            if log_path.exists():
                size = log_path.stat().st_size
                if size < offset:
                    offset = 0
                with log_path.open(encoding="utf-8", errors="replace") as file:
                    file.seek(offset)
                    chunk = file.read()
                    offset = file.tell()
                if chunk:
                    print(chunk, end="", flush=True)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-clipper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config", help="Manage live-clipper configuration.")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_init = config_subparsers.add_parser("init", help="Write a friendly config template.")
    config_init.add_argument("--output", type=Path, default=Path("live-clipper.toml"))
    config_init.add_argument("--force", action="store_true", help="Overwrite an existing config file.")

    prompts = subparsers.add_parser("prompts", help="Manage editable prompt templates.")
    prompts_subparsers = prompts.add_subparsers(dest="prompts_command", required=True)
    prompts_export = prompts_subparsers.add_parser("export", help="Export packaged prompts for local editing.")
    prompts_export.add_argument("--output", type=Path, default=Path("prompts.local"))
    prompts_export.add_argument("--force", action="store_true", help="Overwrite existing prompt files.")

    guide = subparsers.add_parser("guide", help="Print beginner-friendly usage guides.")
    guide_subparsers = guide.add_subparsers(dest="guide_command", required=True)
    guide_ai = guide_subparsers.add_parser("ai", help="Print the Chinese AI assistant setup guide.")
    guide_ai.add_argument("--output", type=Path, default=None, help="Write the guide to a markdown file.")

    setup = subparsers.add_parser("setup", help="Create beginner-friendly local config, folders, and prompt templates.")
    setup.add_argument("--force-config", action="store_true", help="Overwrite live-clipper.toml.")
    setup.add_argument("--force-prompts", action="store_true", help="Overwrite files in prompts.local.")

    app_mode = subparsers.add_parser("app", help="Run in desktop app mode: per-user data directory + web console.")
    app_mode.add_argument("--host", default="127.0.0.1")
    app_mode.add_argument("--port", type=int, default=8765)

    next_step = subparsers.add_parser("next", help="Explain the next action for output runs.")
    next_step.add_argument("--output-root", type=Path, default=Path("output"))

    service = subparsers.add_parser("service", help="Manage the local live-clipper service.")
    service_subparsers = service.add_subparsers(dest="service_command", required=True)
    service_start = service_subparsers.add_parser("start", help="Start the local service.")
    service_start.add_argument("--foreground", action="store_true", help="Run the service in the foreground.")
    service_start.add_argument("--once", action="store_true", help="Run one service iteration and exit.")
    service_subparsers.add_parser("stop", help="Stop the local service.")
    service_status = service_subparsers.add_parser("status", help="Show service health and run state.")
    service_status.add_argument("--json", action="store_true", help="Output JSON only.")
    service_logs = service_subparsers.add_parser("logs", help="Print service logs.")
    service_logs.add_argument("--follow", action="store_true", help="Keep printing log updates.")

    doctor = subparsers.add_parser("doctor", help="Check local deployment readiness.")
    doctor.add_argument("--input-dir", type=Path, default=Path("input"))

    smoke = subparsers.add_parser("smoke", help="Run a local synthetic pipeline smoke test.")
    smoke.add_argument("--output-dir", type=Path, default=Path("work") / "smoke")

    status = subparsers.add_parser("status", help="Report run progress and next step.")
    status.add_argument("run_dir", type=Path)

    pipeline = subparsers.add_parser("pipeline", help="Stage a large source video locally and run scan/brief.")
    pipeline.add_argument("source_path", type=Path)
    pipeline.add_argument("--input-dir", type=Path, default=Path("input"))
    pipeline.add_argument("--output-dir", type=Path, default=None)
    pipeline.add_argument("--correct-transcript", action="store_true", help="Use Agnes transcript correction instead of raw ASR.")
    pipeline.add_argument("--refine", action="store_true", help="Run Agnes refinement before building brief.")
    pipeline.add_argument("--top-n", type=int, default=25)
    pipeline.add_argument("--prompt-dir", type=Path, default=None)

    scan = subparsers.add_parser("scan", help="Run pipeline up to cheap-model candidate generation.")
    scan.add_argument("video_path", type=Path)
    scan.add_argument("--output-dir", type=Path, default=None)
    scan.add_argument("--resume", action="store_true", help="Reuse existing intermediate files in the output directory.")
    scan.add_argument(
        "--skip-transcript-correction",
        action="store_true",
        help="Use raw ASR segments as transcript.json and skip cheap-model transcript correction.",
    )
    scan.add_argument("--prompt-dir", type=Path, default=None)

    refine = subparsers.add_parser("refine", help="Use Agnes to re-rank merged candidates before final review.")
    refine.add_argument("run_dir", type=Path)
    refine.add_argument("--top-n", type=int, default=25)
    refine.add_argument("--prompt-dir", type=Path, default=None)

    brief = subparsers.add_parser("brief", help="Build a compact Codex review package.")
    brief.add_argument("run_dir", type=Path)
    brief.add_argument("--source", choices=["merged", "refined"], default="merged")
    brief.add_argument("--prompt-dir", type=Path, default=None)

    render = subparsers.add_parser("render", help="Render clips from selected_clips.json.")
    render.add_argument("selection_path", type=Path)

    cleanup = subparsers.add_parser("cleanup", help="Clean local large files after clips are rendered.")
    cleanup.add_argument("run_dir", type=Path)
    cleanup.add_argument("--input-dir", type=Path, default=Path("input"))
    cleanup.add_argument("--confirm", action="store_true", help="Actually delete local large files.")
    cleanup.add_argument("--force", action="store_true", help="Allow cleanup before rendered clips are detected.")

    web = subparsers.add_parser("web", help="Start the local web console.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--output-root", type=Path, default=Path("output"))
    web.add_argument("--state-dir", type=Path, default=Path("work") / "automation_state")
    web.add_argument("--log-dir", type=Path, default=Path("work") / "automation_logs")
    web.add_argument("--input-dir", type=Path, default=Path("input"))

    automation = subparsers.add_parser("automation", help="Helpers for Codex scheduled workflows.")
    automation_subparsers = automation.add_subparsers(dest="automation_command", required=True)

    start_latest = automation_subparsers.add_parser(
        "start-latest",
        help="Find the latest NAS recording and start the long pipeline in the background.",
    )
    start_latest.add_argument("--source-dir", type=Path, default=DEFAULT_NAS_DIR)
    start_latest.add_argument("--input-dir", type=Path, default=Path("input"))
    start_latest.add_argument("--output-root", type=Path, default=Path("output"))
    start_latest.add_argument("--since-hours", type=int, default=36)
    start_latest.add_argument("--min-age-minutes", type=int, default=10)
    start_latest.add_argument("--top-n", type=int, default=25)
    start_latest.add_argument("--no-refine", action="store_true", help="Skip Agnes refinement.")
    start_latest.add_argument("--correct-transcript", action="store_true", help="Use Agnes transcript correction.")

    check = automation_subparsers.add_parser(
        "check",
        help="Check output runs and write Codex task files for decision points.",
    )
    check.add_argument("--output-root", type=Path, default=Path("output"))

    return parser


def run_doctor(input_dir: Path = Path("input")) -> dict:
    settings = load_settings()
    video_files = sorted(
        path for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
    ) if input_dir.exists() else []

    ffmpeg_path = shutil.which("ffmpeg")
    glossary_path = resolve_glossary_path()
    checks = [
        {
            "name": "ffmpeg",
            "ok": ffmpeg_path is not None,
            "detail": ffmpeg_path or "ffmpeg was not found on PATH",
        },
        {
            "name": "input_video",
            "ok": bool(video_files),
            "detail": str(video_files[0]) if video_files else f"No supported video files found in {input_dir}",
        },
        {
            "name": "cheap_model_api_key",
            "ok": bool(settings.cheap_model_api_key),
            "detail": "CHEAP_MODEL_API_KEY is set" if settings.cheap_model_api_key else "CHEAP_MODEL_API_KEY is not set",
        },
        {
            "name": "cheap_model",
            "ok": bool(settings.cheap_model_api_base and settings.cheap_model_name),
            "detail": f"{settings.cheap_model_name} @ {settings.cheap_model_api_base}",
        },
        {
            "name": "asr",
            "ok": bool(settings.asr_backend and settings.asr_model),
            "detail": f"{settings.asr_backend} / {settings.asr_model}",
        },
        {
            "name": "glossary",
            "ok": glossary_path.exists(),
            "detail": str(glossary_path) if glossary_path.exists() else "No glossary/common_terms.json or glossary/common_terms.example.json found",
        }
    ]
    required = {"ffmpeg", "input_video", "cheap_model_api_key", "cheap_model", "asr"}
    if settings.asr_backend == "openai":
        checks.append({
            "name": "asr_api_key",
            "ok": bool(settings.asr_api_key),
            "detail": "ASR_API_KEY is set" if settings.asr_api_key else "ASR_API_KEY is required for ASR_BACKEND=openai",
        })
        required.add("asr_api_key")
    checks.append(
        {
            "name": "hf_token",
            "ok": bool(settings.hf_token),
            "detail": "HF_TOKEN is set" if settings.hf_token else "HF_TOKEN is not set; downloads may be slower",
        }
    )
    return {
        "ok": all(check["ok"] for check in checks if check["name"] in required),
        "checks": checks,
    }


def run_scan(
    video_path: Path,
    output_dir: Path | None = None,
    *,
    resume: bool = False,
    skip_transcript_correction: bool = False,
    prompt_dir: Path | None = None,
) -> Path:
    settings = load_settings()
    active_prompt_dir = prompt_dir or settings.prompts.directory
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    run_dir = ensure_dir(output_dir or Path("output") / video_path.stem)
    audio_path = run_dir / "audio.wav"
    raw_transcript_path = run_dir / "transcript_raw.json"
    transcript_path = run_dir / "transcript.json"
    windows_path = run_dir / "windows.json"
    cheap_candidates_path = run_dir / "cheap_candidates.json"
    merged_candidates_path = run_dir / "merged_candidates.json"
    glossary_path = resolve_glossary_path()
    needs_transcript = not (resume and transcript_path.exists())
    needs_raw_transcript = needs_transcript and not (resume and raw_transcript_path.exists())
    needs_audio = needs_raw_transcript and not (resume and audio_path.exists())
    needs_transcript_correction = needs_transcript and not skip_transcript_correction
    needs_cheap_model = needs_transcript_correction or not (resume and cheap_candidates_path.exists())
    if needs_cheap_model and not settings.cheap_model_api_key:
        raise ValueError("CHEAP_MODEL_API_KEY is required before running scan")

    emit_progress(f"[扫描] 输入视频: {video_path}")
    emit_progress(f"[扫描] 输出目录: {run_dir}")
    emit_progress(
        f"[扫描] 运行模式: 断点续跑={'是' if resume else '否'}, "
        f"ASR校对={'跳过' if skip_transcript_correction else 'Agnes'}"
    )
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(video_path),
        "source_name": video_path.name,
        "glossary_path": str(glossary_path),
        "resume": resume,
        "transcript_correction": "skipped" if skip_transcript_correction else "cheap_model",
        "asr": {
            "backend": settings.asr_backend,
            "model": settings.asr_model,
        },
        "cheap_model": {
            "api_base": settings.cheap_model_api_base,
            "model": settings.cheap_model_name,
        },
        "prompts": {
            "source": str(active_prompt_dir) if active_prompt_dir else "packaged",
            "profile": settings.prompts.profile,
        },
    })
    if needs_audio:
        emit_progress("[扫描] 1/6 提取音频: 开始")
        extract_audio(video_path, audio_path)
        emit_progress(f"[扫描] 1/6 提取音频: 完成 -> {audio_path}")
    else:
        emit_progress(f"[扫描] 1/6 提取音频: 复用已有文件 -> {audio_path}")
    if needs_raw_transcript:
        emit_progress(f"[扫描] 2/6 本地ASR识别: 开始 ({settings.asr_backend} / {settings.asr_model})")
        transcribe_audio(audio_path, raw_transcript_path, settings)
        emit_progress(f"[扫描] 2/6 本地ASR识别: 完成 -> {raw_transcript_path}")
    else:
        emit_progress(f"[扫描] 2/6 本地ASR识别: 复用已有文件 -> {raw_transcript_path}")

    client = CheapModelClient(settings) if needs_cheap_model else None
    if not needs_transcript:
        emit_progress(f"[扫描] 3/6 生成文字稿: 复用已有文件 -> {transcript_path}")
        corrected = CorrectedTranscript.model_validate(read_json(transcript_path))
    elif skip_transcript_correction:
        emit_progress("[扫描] 3/6 生成文字稿: 使用原始ASR结果")
        raw = read_json(raw_transcript_path)
        corrected = CorrectedTranscript(
            sentences=transcript_sentences_from_raw(raw),
            corrections=[],
        )
        write_json(transcript_path, corrected.model_dump())
        (run_dir / "transcript.partial.json").unlink(missing_ok=True)
        emit_progress(f"[扫描] 3/6 生成文字稿: 完成 -> {transcript_path}")
    else:
        emit_progress("[扫描] 3/6 Agnes校对文字稿: 开始")
        corrected = correct_transcript_file(
            raw_transcript_path,
            glossary_path,
            transcript_path,
            client,
            resume=resume,
            **({"prompt_dir": active_prompt_dir} if active_prompt_dir else {}),
        )
        emit_progress(f"[扫描] 3/6 Agnes校对文字稿: 完成 -> {transcript_path}")
    if not (resume and windows_path.exists()):
        emit_progress("[扫描] 4/6 生成候选窗口: 开始")
        write_windows_file(corrected, windows_path)
        emit_progress(f"[扫描] 4/6 生成候选窗口: 完成 -> {windows_path}")
    else:
        emit_progress(f"[扫描] 4/6 生成候选窗口: 复用已有文件 -> {windows_path}")
    if not (resume and cheap_candidates_path.exists()):
        emit_progress("[扫描] 5/6 Agnes粗扫候选片段: 开始")
        scan_windows_file(
            windows_path,
            cheap_candidates_path,
            client,
            resume=resume,
            **({"prompt_dir": active_prompt_dir} if active_prompt_dir else {}),
        )
        emit_progress(f"[扫描] 5/6 Agnes粗扫候选片段: 完成 -> {cheap_candidates_path}")
    else:
        emit_progress(f"[扫描] 5/6 Agnes粗扫候选片段: 复用已有文件 -> {cheap_candidates_path}")
    if not (resume and merged_candidates_path.exists()):
        emit_progress("[扫描] 6/6 合并候选片段: 开始")
        merge_candidates_file(cheap_candidates_path, merged_candidates_path)
        emit_progress(f"[扫描] 6/6 合并候选片段: 完成 -> {merged_candidates_path}")
    else:
        emit_progress(f"[扫描] 6/6 合并候选片段: 复用已有文件 -> {merged_candidates_path}")
    emit_progress(f"[扫描] 全部完成: {run_dir}")
    return run_dir


def run_pipeline(
    source_path: Path,
    *,
    input_dir: Path = Path("input"),
    output_dir: Path | None = None,
    correct_transcript: bool = False,
    refine: bool = False,
    top_n: int = 25,
    prompt_dir: Path | None = None,
) -> Path:
    local_source_path = stage_source_file(source_path, input_dir=input_dir)
    scan_kwargs = {"prompt_dir": prompt_dir} if prompt_dir else {}
    run_dir = run_scan(
        local_source_path,
        output_dir,
        resume=True,
        skip_transcript_correction=not correct_transcript,
        **scan_kwargs,
    )
    record_pipeline_metadata(run_dir, source_path, local_source_path)
    if refine:
        refine_kwargs = {"prompt_dir": prompt_dir} if prompt_dir else {}
        run_refine(run_dir, top_n=top_n, **refine_kwargs)
        run_brief(run_dir, source="refined", **refine_kwargs)
    else:
        brief_kwargs = {"prompt_dir": prompt_dir} if prompt_dir else {}
        run_brief(run_dir, source="merged", **brief_kwargs)
    build_run_status(run_dir)
    emit_progress("[流水线] 阶段完成: 已生成候选包, 下一步审阅 codex_brief.json 并写入 selected_clips.json")
    return run_dir


def run_refine(run_dir: Path, *, top_n: int = 25, prompt_dir: Path | None = None) -> Path:
    settings = load_settings()
    active_prompt_dir = prompt_dir or settings.prompts.directory
    if not settings.cheap_model_api_key:
        raise ValueError("CHEAP_MODEL_API_KEY is required before running refine")
    for required_path in [
        run_dir / "merged_candidates.json",
        run_dir / "transcript.json",
    ]:
        if not required_path.exists():
            raise FileNotFoundError(required_path)
    client = CheapModelClient(settings)
    output_path = run_dir / "refined_candidates.json"
    refine_candidates_file(
        run_dir / "merged_candidates.json",
        run_dir / "transcript.json",
        output_path,
        client,
        top_n=top_n,
        **({"prompt_dir": active_prompt_dir} if active_prompt_dir else {}),
    )
    return output_path


def run_brief(run_dir: Path, *, source: str = "merged", prompt_dir: Path | None = None) -> Path:
    settings = load_settings()
    active_prompt_dir = prompt_dir or settings.prompts.directory
    if source == "merged":
        candidates_path = run_dir / "merged_candidates.json"
    elif source == "refined":
        candidates_path = run_dir / "refined_candidates.json"
    else:
        raise ValueError(f"Unsupported brief source: {source}")

    for required_path in [
        run_dir / "run_metadata.json",
        candidates_path,
        run_dir / "transcript.json",
    ]:
        if not required_path.exists():
            raise FileNotFoundError(required_path)
    metadata = read_json(run_dir / "run_metadata.json")
    build_kwargs = {"prompt_dir": active_prompt_dir} if active_prompt_dir else {}
    brief = build_codex_brief_file(
        candidates_path,
        run_dir / "transcript.json",
        run_dir / "codex_brief.json",
        source_name=metadata["source_name"],
        **build_kwargs,
    )
    (run_dir / "codex_review.md").write_text(
        build_codex_review_markdown(
            brief,
            brief_path="codex_brief.json",
            selection_path="selected_clips.json",
        ),
        encoding="utf-8",
    )
    write_json(run_dir / "selected_clips.template.json", build_selected_clips_template(brief))
    return run_dir / "codex_brief.json"


def run_render(selection_path: Path) -> list[Path]:
    run_dir = selection_path.parent
    validate_selected_clips_file(selection_path, run_dir / "merged_candidates.json")
    return render_selected_clips(selection_path)


def run_cleanup(run_dir: Path, *, input_dir: Path = Path("input"), confirm: bool = False, force: bool = False) -> dict:
    report = cleanup_local_artifacts(run_dir, input_dir=input_dir, confirm=confirm, force=force)
    if confirm:
        emit_progress(f"[清理] 已删除 {len(report['deleted'])} 个本地大文件")
    else:
        emit_progress("[清理] 预演模式: 未删除任何文件。确认无误后加 --confirm 执行删除。")
    for target in report["targets"]:
        size_mb = target["bytes"] / 1024 / 1024
        status = "可删除" if target["deletable"] else "受保护"
        emit_progress(f"[清理] {status}: {target['path']} ({size_mb:.1f}MB) - {target['reason']}")
    return report


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "config":
        if args.config_command == "init":
            run_config_init(args.output, force=args.force)
    elif args.command == "prompts":
        if args.prompts_command == "export":
            exported = run_prompts_export(args.output, force=args.force)
            print(json.dumps([str(path) for path in exported], ensure_ascii=False, indent=2))
    elif args.command == "guide":
        if args.guide_command == "ai":
            run_ai_guide(args.output)
    elif args.command == "setup":
        report = run_setup(force_config=args.force_config, force_prompts=args.force_prompts)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "app":
        run_app(host=args.host, port=args.port)
    elif args.command == "next":
        report = run_next(args.output_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "service":
        if args.service_command == "start":
            report = start_service(load_settings(), foreground=args.foreground, once=args.once)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.service_command == "stop":
            report = stop_service()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.service_command == "status":
            report = get_service_status()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.service_command == "logs":
            if args.follow:
                follow_service_logs()
            else:
                print(read_service_logs())
    elif args.command == "doctor":
        report = run_doctor(args.input_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "smoke":
        report = run_local_smoke(args.output_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "status":
        report = build_run_status(args.run_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "pipeline":
        try:
            pipeline_kwargs = {"prompt_dir": args.prompt_dir} if args.prompt_dir else {}
            run_pipeline(
                args.source_path,
                input_dir=args.input_dir,
                output_dir=args.output_dir,
                correct_transcript=args.correct_transcript,
                refine=args.refine,
                top_n=args.top_n,
                **pipeline_kwargs,
            )
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n进度已经写入断点文件。请重新运行同一条 pipeline 命令继续。"
            ) from None
    elif args.command == "scan":
        try:
            scan_kwargs = {"prompt_dir": args.prompt_dir} if args.prompt_dir else {}
            run_scan(
                args.video_path,
                args.output_dir,
                resume=args.resume,
                skip_transcript_correction=args.skip_transcript_correction,
                **scan_kwargs,
            )
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n进度已经写入断点文件。请使用同一条命令加 --resume 继续。"
            ) from None
    elif args.command == "refine":
        try:
            refine_kwargs = {"prompt_dir": args.prompt_dir} if args.prompt_dir else {}
            run_refine(args.run_dir, top_n=args.top_n, **refine_kwargs)
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n请用相同参数重新运行 refine，继续重试 Agnes 复评。"
            ) from None
    elif args.command == "brief":
        brief_kwargs = {"prompt_dir": args.prompt_dir} if args.prompt_dir else {}
        run_brief(args.run_dir, source=args.source, **brief_kwargs)
    elif args.command == "render":
        run_render(args.selection_path)
    elif args.command == "cleanup":
        report = run_cleanup(args.run_dir, input_dir=args.input_dir, confirm=args.confirm, force=args.force)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "web":
        run_web_server(
            host=args.host,
            port=args.port,
            paths=WebPaths(
                output_root=args.output_root,
                state_dir=args.state_dir,
                log_dir=args.log_dir,
                input_dir=args.input_dir,
            ),
            access_token=load_settings().web.access_token,
        )
    elif args.command == "automation":
        if args.automation_command == "start-latest":
            report = start_latest_recording_job(
                args.source_dir,
                input_dir=args.input_dir,
                output_root=args.output_root,
                since_hours=args.since_hours,
                min_age_minutes=args.min_age_minutes,
                refine=not args.no_refine,
                top_n=args.top_n,
                correct_transcript=args.correct_transcript,
            )
        elif args.automation_command == "check":
            report = check_automation_runs(args.output_root)
        else:
            raise SystemExit(f"Unsupported automation command: {args.automation_command}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
