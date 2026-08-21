"""Render selected clips with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import CorrectedTranscript, SelectedClip
from .subtitles import write_srt_file
from .utils import ensure_dir, read_json, write_json


def emit_progress(message: str) -> None:
    print(message, flush=True)


def kept_segments_for_clip(clip: SelectedClip) -> list[tuple[float, float]]:
    segments: list[tuple[float, float]] = []
    cursor = clip.source_start
    for remove_start, remove_end in sorted(clip.remove_ranges):
        if cursor < remove_start:
            segments.append((cursor, remove_start))
        cursor = max(cursor, remove_end)
    if cursor < clip.source_end:
        segments.append((cursor, clip.source_end))
    return segments


def run_ffmpeg(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required on PATH to render clips") from exc


def add_quicktime_mp4_options(cmd: list[str]) -> None:
    cmd.extend([
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
    ])


def render_segment(
    source_video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    reset_timestamps: bool = False,
) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source_video_path),
    ]
    if reset_timestamps:
        cmd.extend([
            "-vf",
            "setpts=PTS-STARTPTS",
            "-af",
            "asetpts=PTS-STARTPTS",
            "-avoid_negative_ts",
            "make_zero",
        ])
    add_quicktime_mp4_options(cmd)
    cmd.append(str(output_path))
    run_ffmpeg(cmd)


def concat_segments(segment_paths: list[Path], concat_list_path: Path, output_path: Path) -> None:
    def quote_path(path: Path) -> str:
        return path.resolve().as_posix().replace("'", "'\\''")

    concat_list_path.write_text(
        "".join(f"file '{quote_path(path)}'\n" for path in segment_paths),
        encoding="utf-8",
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(output_path),
        ]
    )


def render_selected_clips(selection_path: Path) -> list[Path]:
    run_dir = selection_path.parent
    metadata = read_json(run_dir / "run_metadata.json")
    source_video_path = Path(metadata["source_video_path"])
    if not source_video_path.exists():
        raise FileNotFoundError(source_video_path)
    transcript = CorrectedTranscript.model_validate(read_json(run_dir / "transcript.json"))
    selected = [SelectedClip.model_validate(item) for item in read_json(selection_path)]
    clips_dir = ensure_dir(run_dir / "clips")
    subtitles_dir = ensure_dir(run_dir / "subtitles")
    rendered_paths: list[Path] = []
    edl: list[dict[str, object]] = []

    emit_progress(f"[渲染] 开始: 共 {len(selected)} 条入选片段")
    for clip_index, clip in enumerate(selected, start=1):
        output_path = clips_dir / f"{clip.clip_id}.mp4"
        subtitle_path = subtitles_dir / f"{clip.clip_id}.srt"
        duration = clip.source_end - clip.source_start
        emit_progress(
            f"[渲染] {clip_index}/{len(selected)} {clip.clip_id}: "
            f"{clip.source_start:.2f}s - {clip.source_end:.2f}s, 原始时长 {duration:.2f}s"
        )
        write_srt_file(transcript, clip, subtitle_path)
        warnings: list[str] = []
        remove_ranges_applied = bool(clip.remove_ranges)
        edl.append(
            {
                "clip_id": clip.clip_id,
                "source_start": clip.source_start,
                "source_end": clip.source_end,
                "title": clip.title,
                "remove_ranges": clip.remove_ranges,
                "remove_ranges_applied": remove_ranges_applied,
                "warnings": warnings,
                "output_path": str(output_path),
                "subtitle_path": str(subtitle_path),
            }
        )
        if clip.remove_ranges:
            emit_progress(
                f"[渲染] {clip_index}/{len(selected)} {clip.clip_id}: "
                f"检测到 {len(clip.remove_ranges)} 段 remove_ranges, 将分段渲染后拼接"
            )
            parts_dir = clips_dir / ".parts" / clip.clip_id
            if parts_dir.exists():
                shutil.rmtree(parts_dir)
            ensure_dir(parts_dir)
            segment_paths = []
            for index, (start, end) in enumerate(kept_segments_for_clip(clip), start=1):
                segment_path = parts_dir / f"{index:03d}.mp4"
                emit_progress(
                    f"[渲染] {clip_index}/{len(selected)} {clip.clip_id}: "
                    f"渲染保留片段 {index}: {start:.2f}s - {end:.2f}s"
                )
                render_segment(source_video_path, start, end, segment_path, reset_timestamps=True)
                segment_paths.append(segment_path)
            concat_segments(segment_paths, parts_dir / "concat.txt", output_path)
        else:
            render_segment(source_video_path, clip.source_start, clip.source_end, output_path)
        rendered_paths.append(output_path)
        emit_progress(f"[渲染] {clip_index}/{len(selected)} {clip.clip_id}: 完成 -> {output_path}")

    write_json(run_dir / "edit_decision_list.json", edl)
    emit_progress(f"[渲染] 全部完成: {len(rendered_paths)} 条成片 -> {clips_dir}")
    return rendered_paths
