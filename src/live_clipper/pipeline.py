"""Local staging and cleanup helpers for large NAS recordings."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir, read_json, write_json

COPY_CHUNK_SIZE = 16 * 1024 * 1024


def emit_progress(message: str) -> None:
    print(message, flush=True)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def stage_source_file(source_path: Path, input_dir: Path = Path("input")) -> Path:
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    ensure_dir(input_dir)
    destination = input_dir / source_path.name
    if source_path.resolve() == destination.resolve():
        emit_progress(f"[流水线] 源视频已经在本地输入目录: {destination}")
        return destination

    source_size = source_path.stat().st_size
    if destination.exists() and destination.stat().st_size == source_size:
        emit_progress(f"[流水线] 本地源视频已存在且大小一致, 复用: {destination}")
        return destination

    part_path = destination.with_name(f"{destination.name}.part")
    copied = part_path.stat().st_size if part_path.exists() else 0
    if copied > source_size:
        part_path.unlink()
        copied = 0

    mode = "ab" if copied else "wb"
    emit_progress(
        f"[流水线] 复制源视频到本地: {source_path} -> {destination} "
        f"({source_size / 1024 / 1024 / 1024:.2f}GB)"
    )
    if copied:
        emit_progress(f"[流水线] 检测到未完成复制, 从 {copied / 1024 / 1024:.1f}MB 继续")

    next_report_at = copied
    with source_path.open("rb") as source, part_path.open(mode) as destination_file:
        source.seek(copied)
        while True:
            chunk = source.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            destination_file.write(chunk)
            copied += len(chunk)
            if copied >= next_report_at or copied == source_size:
                percent = copied / source_size * 100 if source_size else 100
                emit_progress(f"[流水线] 复制进度: {percent:.1f}% ({copied / 1024 / 1024:.1f}MB)")
                next_report_at = copied + max(source_size // 20, 256 * 1024 * 1024)

    part_path.replace(destination)
    emit_progress(f"[流水线] 复制完成: {destination}")
    return destination


def record_pipeline_metadata(run_dir: Path, original_source_path: Path, local_source_path: Path) -> None:
    metadata_path = run_dir / "run_metadata.json"
    metadata = read_json(metadata_path)
    metadata["pipeline"] = {
        **metadata.get("pipeline", {}),
        "original_source_path": str(original_source_path),
        "local_source_path": str(local_source_path),
        "staged_at": datetime.now(UTC).isoformat(),
    }
    write_json(metadata_path, metadata)


def cleanup_plan(run_dir: Path, *, input_dir: Path = Path("input")) -> list[dict[str, Any]]:
    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(metadata_path)
    metadata = read_json(metadata_path)
    pipeline = metadata.get("pipeline", {})
    source_value = pipeline.get("local_source_path") or metadata.get("source_video_path")
    source_path = Path(source_value) if source_value else None
    original_value = pipeline.get("original_source_path")
    original_source_path = Path(original_value) if original_value else None
    targets: list[dict[str, Any]] = []

    audio_path = run_dir / "audio.wav"
    if audio_path.exists():
        targets.append({
            "kind": "audio",
            "path": str(audio_path),
            "bytes": audio_path.stat().st_size,
            "deletable": True,
            "reason": "ASR 已完成后可删除的大体积中间音频",
        })

    if source_path is not None and source_path.exists():
        source_is_local_copy = (
            _path_is_relative_to(source_path, input_dir)
            and (original_source_path is None or source_path.resolve() != original_source_path.resolve())
        )
        targets.append({
            "kind": "local_source_video",
            "path": str(source_path),
            "bytes": source_path.stat().st_size,
            "deletable": source_is_local_copy,
            "reason": "本地复制的视频源，可在渲染完成后删除" if source_is_local_copy else "不是受保护的本地复制件，默认不会删除",
        })

    return targets


def cleanup_local_artifacts(
    run_dir: Path,
    *,
    input_dir: Path = Path("input"),
    confirm: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    selected_path = run_dir / "selected_clips.json"
    clips_dir = run_dir / "clips"
    clips = sorted(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    if not force and (not selected_path.exists() or not clips):
        raise RuntimeError("尚未检测到已渲染成片。若确认要清理，请加 --force。")

    targets = cleanup_plan(run_dir, input_dir=input_dir)
    deleted: list[str] = []
    skipped: list[str] = []
    for target in targets:
        path = Path(target["path"])
        if not target["deletable"]:
            skipped.append(str(path))
            continue
        if confirm:
            path.unlink(missing_ok=True)
            deleted.append(str(path))
        else:
            skipped.append(str(path))

    metadata_path = run_dir / "run_metadata.json"
    metadata = read_json(metadata_path)
    metadata["pipeline"] = {
        **metadata.get("pipeline", {}),
        "cleanup_checked_at": datetime.now(UTC).isoformat(),
        "cleanup_confirmed": confirm,
        "cleanup_deleted": deleted,
    }
    write_json(metadata_path, metadata)

    return {
        "run_dir": str(run_dir),
        "confirm": confirm,
        "targets": targets,
        "deleted": deleted,
        "skipped": skipped,
    }
