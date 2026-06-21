"""Video and audio extraction helpers."""

from __future__ import annotations

from pathlib import Path
import subprocess

from .utils import ensure_dir


def extract_audio(video_path: Path, output_wav_path: Path) -> Path:
    ensure_dir(output_wav_path.parent)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(output_wav_path),
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required on PATH to extract audio") from exc
    return output_wav_path
