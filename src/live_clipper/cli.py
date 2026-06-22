from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from .automation import DEFAULT_NAS_DIR, check_automation_runs, start_latest_recording_job
from .build_codex_brief import (
    build_codex_brief_file,
    build_codex_review_markdown,
    build_selected_clips_template,
)
from .cheap_model_client import CheapModelClient, CheapModelServiceError
from .codex_selection import validate_selected_clips_file
from .config import load_settings
from .correct_transcript import correct_transcript_file
from .merge_candidates import merge_candidates_file
from .models import CorrectedTranscript
from .pipeline import cleanup_local_artifacts, record_pipeline_metadata, stage_source_file
from .refine_candidates import refine_candidates_file
from .render_clips import render_selected_clips
from .scan_windows import scan_windows_file
from .smoke import run_local_smoke
from .status import build_run_status
from .transcribe import transcribe_audio, transcript_sentences_from_raw
from .utils import ensure_dir, read_json, write_json
from .video import extract_audio
from .web import WebPaths, run_web_server
from .windows import write_windows_file


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def emit_progress(message: str) -> None:
    print(message, flush=True)


def resolve_glossary_path(glossary_dir: Path = Path("glossary")) -> Path:
    real_path = glossary_dir / "common_terms.json"
    if real_path.exists():
        return real_path
    return glossary_dir / "common_terms.example.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-clipper")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    scan = subparsers.add_parser("scan", help="Run pipeline up to cheap-model candidate generation.")
    scan.add_argument("video_path", type=Path)
    scan.add_argument("--output-dir", type=Path, default=None)
    scan.add_argument("--resume", action="store_true", help="Reuse existing intermediate files in the output directory.")
    scan.add_argument(
        "--skip-transcript-correction",
        action="store_true",
        help="Use raw ASR segments as transcript.json and skip cheap-model transcript correction.",
    )

    refine = subparsers.add_parser("refine", help="Use Agnes to re-rank merged candidates before final review.")
    refine.add_argument("run_dir", type=Path)
    refine.add_argument("--top-n", type=int, default=25)

    brief = subparsers.add_parser("brief", help="Build a compact Codex review package.")
    brief.add_argument("run_dir", type=Path)
    brief.add_argument("--source", choices=["merged", "refined"], default="merged")

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
) -> Path:
    settings = load_settings()
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
        scan_windows_file(windows_path, cheap_candidates_path, client, resume=resume)
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
) -> Path:
    local_source_path = stage_source_file(source_path, input_dir=input_dir)
    run_dir = run_scan(
        local_source_path,
        output_dir,
        resume=True,
        skip_transcript_correction=not correct_transcript,
    )
    record_pipeline_metadata(run_dir, source_path, local_source_path)
    if refine:
        run_refine(run_dir, top_n=top_n)
        run_brief(run_dir, source="refined")
    else:
        run_brief(run_dir, source="merged")
    build_run_status(run_dir)
    emit_progress(f"[流水线] 阶段完成: 已生成候选包, 下一步审阅 codex_brief.json 并写入 selected_clips.json")
    return run_dir


def run_refine(run_dir: Path, *, top_n: int = 25) -> Path:
    settings = load_settings()
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
    )
    return output_path


def run_brief(run_dir: Path, *, source: str = "merged") -> Path:
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
    brief = build_codex_brief_file(
        candidates_path,
        run_dir / "transcript.json",
        run_dir / "codex_brief.json",
        source_name=metadata["source_name"],
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
    if args.command == "doctor":
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
            run_pipeline(
                args.source_path,
                input_dir=args.input_dir,
                output_dir=args.output_dir,
                correct_transcript=args.correct_transcript,
                refine=args.refine,
                top_n=args.top_n,
            )
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n进度已经写入断点文件。请重新运行同一条 pipeline 命令继续。"
            ) from None
    elif args.command == "scan":
        try:
            run_scan(
                args.video_path,
                args.output_dir,
                resume=args.resume,
                skip_transcript_correction=args.skip_transcript_correction,
            )
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n进度已经写入断点文件。请使用同一条命令加 --resume 继续。"
            ) from None
    elif args.command == "refine":
        try:
            run_refine(args.run_dir, top_n=args.top_n)
        except CheapModelServiceError as exc:
            raise SystemExit(
                f"{exc}\n请用相同参数重新运行 refine，继续重试 Agnes 复评。"
            ) from None
    elif args.command == "brief":
        run_brief(args.run_dir, source=args.source)
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
