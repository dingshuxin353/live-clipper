from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaMetadata:
    duration_ms: int
    width: int
    height: int
    container: str
    video_codec: str
    byte_size: int

    def as_storage_dict(self) -> dict[str, int | str]:
        return {
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "container": self.container,
            "video_codec": self.video_codec,
            "byte_size": self.byte_size,
        }


ProbeRunner = Callable[..., subprocess.CompletedProcess[str]]


def probe_media(
    path: str | Path,
    *,
    ffprobe_path: str = "ffprobe",
    runner: ProbeRunner = subprocess.run,
) -> MediaMetadata:
    target = Path(path)
    if not target.is_file():
        raise MediaProbeError("media file is missing")
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height,duration",
        "-of",
        "json",
        str(target),
    ]
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise MediaProbeError("ffprobe is required on PATH") from exc
    if completed.returncode != 0:
        raise MediaProbeError("ffprobe could not validate the rendered media")
    try:
        payload: dict[str, Any] = json.loads(completed.stdout)
        video = next(item for item in payload.get("streams", []) if item.get("codec_type") == "video")
        duration = payload.get("format", {}).get("duration") or video.get("duration")
        duration_ms = round(float(duration) * 1000)
        width = int(video["width"])
        height = int(video["height"])
        container = str(payload.get("format", {}).get("format_name") or "")
        codec = str(video.get("codec_name") or "")
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProbeError("ffprobe returned incomplete media metadata") from exc
    if duration_ms <= 0 or width <= 0 or height <= 0 or not container or not codec:
        raise MediaProbeError("rendered media metadata is not valid")
    return MediaMetadata(duration_ms, width, height, container, codec, target.stat().st_size)
