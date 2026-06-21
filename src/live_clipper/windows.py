"""Transcript windowing utilities."""

from __future__ import annotations

from pathlib import Path

from .models import CorrectedTranscript, TranscriptWindow
from .utils import write_json


def split_transcript_into_windows(
    transcript: CorrectedTranscript,
    *,
    window_seconds: int = 240,
    overlap_seconds: int = 30,
) -> list[TranscriptWindow]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than 0")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("overlap_seconds must be non-negative and smaller than window_seconds")
    if not transcript.sentences:
        return []

    final_end = max(sentence.end for sentence in transcript.sentences)
    step = window_seconds - overlap_seconds
    windows: list[TranscriptWindow] = []
    start = 0.0
    index = 1

    while start < final_end:
        end = min(start + window_seconds, final_end)
        sentences = [
            sentence
            for sentence in transcript.sentences
            if sentence.start < end and sentence.end > start
        ]
        if sentences:
            windows.append(
                TranscriptWindow(
                    id=f"w{index:04d}",
                    start=float(start),
                    end=float(end),
                    sentences=sentences,
                )
            )
            index += 1
        start += step

    return windows


def write_windows_file(
    transcript: CorrectedTranscript,
    output_path: Path,
    *,
    window_seconds: int = 240,
    overlap_seconds: int = 30,
) -> list[TranscriptWindow]:
    windows = split_transcript_into_windows(
        transcript,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    write_json(output_path, [window.model_dump() for window in windows])
    return windows
