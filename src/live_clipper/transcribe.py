"""ASR integration for timestamped transcripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mlx_whisper
import requests

from .config import Settings
from .models import TranscriptSentence
from .utils import write_json


DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"


def transcribe_audio(audio_path: Path, output_json_path: Path, settings: Settings) -> dict[str, Any]:
    backend = settings.asr_backend or "mlx_whisper"
    if backend == "mlx_whisper":
        model = settings.asr_model or DEFAULT_ASR_MODEL
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model,
            language="zh",
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
