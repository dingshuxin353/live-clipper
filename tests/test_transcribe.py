from __future__ import annotations

import pytest

from live_clipper.config import Settings
from live_clipper.models import TranscriptSentence
from live_clipper.transcribe import transcribe_audio, transcript_sentences_from_raw


def test_transcribe_audio_writes_mlx_whisper_raw_json(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    calls = []

    def fake_transcribe(path, path_or_hf_repo, language):
        calls.append((path, path_or_hf_repo, language))
        return {
            "text": "你好 Codex",
            "language": "zh",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "你好 Codex"},
            ],
        }

    monkeypatch.setattr("live_clipper.transcribe.mlx_whisper.transcribe", fake_transcribe)

    result = transcribe_audio(
        audio,
        output,
        Settings(asr_backend="mlx_whisper", asr_model="mlx-community/whisper-large-v3-turbo"),
    )

    assert result["text"] == "你好 Codex"
    assert output.exists()
    assert calls == [(str(audio), "mlx-community/whisper-large-v3-turbo", "zh")]


def test_transcribe_audio_writes_openai_compatible_verbose_json(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "text": "你好 Codex",
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": "你好 Codex"},
                ],
            }

    def fake_post(url, headers, data, files, timeout):
        calls.append((url, headers, data, files["file"][0], files["file"][1].read(), timeout))
        return FakeResponse()

    monkeypatch.setattr("live_clipper.transcribe.requests.post", fake_post)

    result = transcribe_audio(
        audio,
        output,
        Settings(
            asr_backend="openai",
            asr_api_base="https://api.openai.com/v1",
            asr_api_key="asr-secret",
            asr_model="whisper-1",
        ),
    )

    assert result["segments"][0]["text"] == "你好 Codex"
    assert output.exists()
    assert calls == [(
        "https://api.openai.com/v1/audio/transcriptions",
        {"Authorization": "Bearer asr-secret"},
        {
            "model": "whisper-1",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        },
        "audio.wav",
        b"wav",
        300,
    )]


def test_transcribe_audio_requires_openai_asr_key(tmp_path):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")

    with pytest.raises(ValueError, match="ASR_API_KEY"):
        transcribe_audio(
            audio,
            output,
            Settings(asr_backend="openai", asr_api_base="https://api.openai.com/v1", asr_model="whisper-1"),
        )


def test_transcript_sentences_from_raw_uses_segment_timestamps():
    raw = {
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "第一句"},
            {"start": 1.5, "end": 3.0, "text": "第二句"},
        ]
    }

    assert transcript_sentences_from_raw(raw) == [
        TranscriptSentence(start=0.0, end=1.5, text="第一句"),
        TranscriptSentence(start=1.5, end=3.0, text="第二句"),
    ]


def test_transcript_sentences_from_raw_skips_empty_text_segments():
    raw = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "   "},
            {"start": 1.0, "end": 2.0, "text": "\n"},
            {"start": 2.0, "end": 3.0, "text": "有效内容"},
        ]
    }

    assert transcript_sentences_from_raw(raw) == [
        TranscriptSentence(start=2.0, end=3.0, text="有效内容"),
    ]


def test_transcript_sentences_from_raw_skips_invalid_time_segments():
    raw = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "有效内容"},
            {"start": 4805.58, "end": 4805.38, "text": "!"},
        ]
    }

    assert transcript_sentences_from_raw(raw) == [
        TranscriptSentence(start=0.0, end=1.0, text="有效内容"),
    ]
