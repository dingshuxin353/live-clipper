"""Subtitle generation helpers."""

from __future__ import annotations

from pathlib import Path

from .models import CorrectedTranscript, SelectedClip
from .utils import ensure_dir


def _format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def kept_segments_with_offsets(clip: SelectedClip) -> list[tuple[float, float, float]]:
    segments: list[tuple[float, float, float]] = []
    cursor = clip.source_start
    output_offset = 0.0
    for remove_start, remove_end in sorted(clip.remove_ranges):
        if cursor < remove_start:
            segments.append((cursor, remove_start, output_offset))
            output_offset += remove_start - cursor
        cursor = max(cursor, remove_end)
    if cursor < clip.source_end:
        segments.append((cursor, clip.source_end, output_offset))
    return segments


def write_srt_file(
    transcript: CorrectedTranscript,
    clip: SelectedClip,
    output_path: Path,
) -> Path:
    ensure_dir(output_path.parent)
    entries: list[str] = []
    index = 1

    for segment_start, segment_end, output_offset in kept_segments_with_offsets(clip):
        for sentence in transcript.sentences:
            if sentence.start < segment_end and sentence.end > segment_start:
                start = output_offset + max(sentence.start, segment_start) - segment_start
                end = output_offset + min(sentence.end, segment_end) - segment_start
                entries.append(
                    f"{index}\n"
                    f"{_format_srt_time(start)} --> {_format_srt_time(end)}\n"
                    f"{sentence.text}"
                )
                index += 1

    body = "\n\n".join(entries)
    if body:
        body += "\n"
    output_path.write_text(body, encoding="utf-8")
    return output_path
