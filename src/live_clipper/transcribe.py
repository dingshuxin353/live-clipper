"""ASR integration for timestamped transcripts."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import requests

from . import asr_models
from .config import Settings
from .models import TranscriptSentence
from .utils import write_json

try:
    import mlx_whisper
except ImportError:
    mlx_whisper = None


DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_ASR_LANGUAGE = "zh"
REPETITION_LIMIT = 5
SEEK_UNITS_PER_SECOND = 100.0
REPAIR_WINDOW_SECONDS = 30.0
REPAIR_BOUNDARY_TOLERANCE = 0.02


def _segment_text(segment: Any) -> str:
    if not isinstance(segment, dict):
        return ""
    return str(segment.get("text", "")).strip()


def _segment_seek(segment: Any) -> int | None:
    if not isinstance(segment, dict):
        return None
    try:
        return int(segment["seek"])
    except (KeyError, OverflowError, TypeError, ValueError):
        return None


def _pathological_seeks(segments: Any) -> set[int]:
    if not isinstance(segments, list):
        return set()
    matched: set[int] = set()
    current_text = ""
    current_segments: list[dict[str, Any]] = []

    def finish_group() -> None:
        if len(current_segments) >= REPETITION_LIMIT:
            matched.update(
                seek
                for segment in current_segments
                if (seek := _segment_seek(segment)) is not None
            )

    for segment in segments:
        text = _segment_text(segment)
        if not text:
            continue
        elif text == current_text:
            current_segments.append(segment)
        else:
            finish_group()
            current_text = text
            current_segments = [segment]
    finish_group()
    return matched


def _has_pathological_repetition(segments: Any) -> bool:
    if not isinstance(segments, list):
        return False
    current_text = ""
    count = 0
    for segment in segments:
        text = _segment_text(segment)
        if not text:
            continue
        elif text == current_text:
            count += 1
        else:
            current_text = text
            count = 1
        if count >= REPETITION_LIMIT:
            return True
    return False


def _segment_times(segment: Any) -> tuple[float, float] | None:
    if not isinstance(segment, dict):
        return None
    try:
        start = float(segment["start"])
        end = float(segment["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end):
        return None
    return start, end


def _replacement_segments(
    repair: Any,
    *,
    window_start: float,
    boundary: float,
) -> list[dict[str, Any]]:
    if not isinstance(repair, dict) or not isinstance(repair.get("segments"), list):
        return []
    replacements = []
    for segment in repair["segments"]:
        text = _segment_text(segment)
        times = _segment_times(segment)
        if not text or times is None:
            continue
        start, end = times
        if (
            start >= end
            or start < window_start - REPAIR_BOUNDARY_TOLERANCE
            or end > boundary + REPAIR_BOUNDARY_TOLERANCE
        ):
            continue
        replacements.append(dict(segment))
    return replacements


def _has_final_repetition(segments: list[dict[str, Any]]) -> bool:
    current_text = ""
    count = 0
    for segment in segments:
        text = _segment_text(segment)
        if not text:
            continue
        if text == current_text:
            count += 1
        else:
            current_text = text
            count = 1
        if count >= REPETITION_LIMIT:
            return True
    return False


def _validate_repaired_segments(segments: list[dict[str, Any]]) -> None:
    previous_start = -math.inf
    for segment in segments:
        if not _segment_text(segment):
            continue
        times = _segment_times(segment)
        if times is None:
            raise RuntimeError("MLX transcription repair failed: invalid segment timestamps")
        start, end = times
        if start >= end or start < previous_start:
            raise RuntimeError("MLX transcription repair failed: invalid segment time order")
        previous_start = start
    if _has_final_repetition(segments):
        raise RuntimeError("MLX transcription repair failed: repeated segments remain")


def _repair_repeated_mlx_segments(
    result: dict[str, Any],
    *,
    audio_path: Path,
    model: str,
    language: str | None,
) -> dict[str, Any]:
    original_segments = result.get("segments")
    has_repetition = _has_pathological_repetition(original_segments)
    repair_seeks = sorted(_pathological_seeks(original_segments))
    if not has_repetition:
        return result
    if not repair_seeks:
        raise RuntimeError("MLX transcription repair failed: repeated segments have no valid seek")
    assert isinstance(original_segments, list)
    if not all(isinstance(segment, dict) for segment in original_segments):
        raise RuntimeError("MLX transcription repair failed: segments must be objects")

    all_seeks = sorted({
        seek
        for segment in original_segments
        if (seek := _segment_seek(segment)) is not None
    })
    merged = [dict(segment) for segment in original_segments]
    for seek in repair_seeks:
        window_start = seek / SEEK_UNITS_PER_SECOND
        decode_end = window_start + REPAIR_WINDOW_SECONDS
        next_seek = next((candidate for candidate in all_seeks if candidate > seek), None)
        boundary = next_seek / SEEK_UNITS_PER_SECOND if next_seek is not None else decode_end
        repair = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model,
            language=language,
            condition_on_previous_text=False,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
            clip_timestamps=f"{window_start},{decode_end}",
        )
        merged = [segment for segment in merged if _segment_seek(segment) != seek]
        merged.extend(
            _replacement_segments(
                repair,
                window_start=window_start,
                boundary=boundary,
            )
        )

    def sort_key(segment: dict[str, Any]) -> tuple[float, float]:
        return _segment_times(segment) or (math.inf, math.inf)

    merged.sort(key=sort_key)
    for index, segment in enumerate(merged):
        segment["id"] = index
    _validate_repaired_segments(merged)
    repaired = dict(result)
    repaired["segments"] = merged
    repaired["text"] = "".join(str(segment.get("text", "")) for segment in merged)
    return repaired


def transcribe_audio(audio_path: Path, output_json_path: Path, settings: Settings) -> dict[str, Any]:
    backend = settings.asr_backend or "mlx_whisper"
    if backend == "mlx_whisper":
        if mlx_whisper is None:
            raise RuntimeError("Install the mlx extra to use ASR_BACKEND=mlx_whisper: pip install 'live-clipper[mlx]'")
        model = settings.asr_model or DEFAULT_ASR_MODEL
        local_model = asr_models.local_path_for(model)
        if local_model is not None:
            model = str(local_model)
        language = None if (settings.asr_language or "zh") == "auto" else (settings.asr_language or "zh")
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model,
            language=language,
            condition_on_previous_text=False,
        )
        result = _repair_repeated_mlx_segments(
            result,
            audio_path=audio_path,
            model=model,
            language=language,
        )
    elif backend == "openai":
        result = transcribe_audio_openai(audio_path, settings)
    else:
        raise ValueError(f"Unsupported ASR_BACKEND: {backend}")
    write_json(output_json_path, result)
    return result


def transcribe_audio_openai(audio_path: Path, settings: Settings) -> dict[str, Any]:
    if not settings.asr_api_key:
        raise ValueError("ASR_API_KEY is required when ASR_BACKEND=openai")
    api_base = (settings.asr_api_base or "https://api.openai.com/v1").rstrip("/")
    model = settings.asr_model or "whisper-1"
    with audio_path.open("rb") as audio_file:
        response = requests.post(
            f"{api_base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.asr_api_key}"},
            data={
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
                **(
                    {"language": settings.asr_language}
                    if settings.asr_language and settings.asr_language not in {"auto", DEFAULT_ASR_LANGUAGE}
                    else {}
                ),
            },
            files={"file": (audio_path.name, audio_file)},
            timeout=300,
        )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("ASR transcription response must be a JSON object")
    return result


def transcript_sentences_from_raw(raw_transcript: dict[str, Any]) -> list[TranscriptSentence]:
    sentences: list[TranscriptSentence] = []
    for segment in raw_transcript.get("segments", []):
        text = str(segment["text"]).strip()
        if not text:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        if start >= end:
            continue
        sentences.append(
            TranscriptSentence(
                start=start,
                end=end,
                text=text,
            )
        )
    return sentences
