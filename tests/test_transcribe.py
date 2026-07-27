from __future__ import annotations

import pytest

from live_clipper import asr_models
from live_clipper.config import Settings
from live_clipper.models import TranscriptSentence
from live_clipper.transcribe import transcribe_audio, transcript_sentences_from_raw
from live_clipper.utils import read_json


def test_transcribe_audio_writes_mlx_whisper_raw_json(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    calls = []

    fake_result = {
        "text": "你好 Codex",
        "language": "zh",
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "你好 Codex"},
        ],
    }

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return fake_result

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(
        audio,
        output,
        Settings(asr_backend="mlx_whisper", asr_model="mlx-community/whisper-large-v3-turbo"),
    )

    assert result == fake_result
    assert read_json(output) == fake_result
    assert calls == [{
        "path": str(audio),
        "path_or_hf_repo": "mlx-community/whisper-large-v3-turbo",
        "language": "zh",
        "condition_on_previous_text": False,
    }]


def test_transcribe_audio_uses_configured_auto_language(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return {"segments": []}

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    transcribe_audio(
        audio,
        output,
        Settings(asr_backend="mlx_whisper", asr_model="mlx-community/whisper-large-v3-turbo", asr_language="auto"),
    )

    assert calls == [{
        "path": str(audio),
        "path_or_hf_repo": "mlx-community/whisper-large-v3-turbo",
        "language": None,
        "condition_on_previous_text": False,
    }]


def test_transcribe_prefers_installed_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    local_model = tmp_path / "verified-model"
    audio.write_bytes(b"wav")
    local_model.mkdir()
    monkeypatch.setattr(asr_models, "local_path_for", lambda model_id: local_model)
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return {"segments": []}

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    transcribe_audio(
        audio,
        output,
        Settings(asr_backend="mlx_whisper"),
    )

    assert calls == [{
        "path": str(audio),
        "path_or_hf_repo": str(local_model),
        "language": "zh",
        "condition_on_previous_text": False,
    }]


def _segment(index, text, seek, *, start=None, end=None, **extra):
    return {
        "id": index,
        "seek": seek,
        "start": float(index if start is None else start),
        "end": float(index + 1 if end is None else end),
        "text": text,
        **extra,
    }


def _mlx_settings():
    return Settings(
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    )


def test_transcribe_audio_does_not_repair_four_repeated_segments(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {"text": "四段", "segments": [_segment(index, "相同", 0) for index in range(4)]}
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return first

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(audio, output, _mlx_settings())

    assert result == first
    assert read_json(output) == first
    assert len(calls) == 1


def test_transcribe_audio_ignores_empty_segments_when_detecting_repetition(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {
        "segments": [
            *[_segment(index, "相同", 0) for index in range(3)],
            _segment(3, " ", 0),
            *[_segment(index, "相同", 0) for index in range(4, 7)],
        ]
    }
    repair = {"segments": [_segment(0, "修复后", 0, start=0, end=1)]}
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return first if len(calls) == 1 else repair

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(audio, output, _mlx_settings())

    assert len(calls) == 2
    assert [segment["text"] for segment in result["segments"]] == ["修复后"]
    assert read_json(output) == result


def test_transcribe_audio_repairs_one_seek_and_preserves_next_seek(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {
        "text": "旧文本",
        "language": "zh",
        "segments": [
            _segment(0, "开头", 0, start=0, end=1),
            *[_segment(index, "重复", 1000, start=10 + index, end=11 + index) for index in range(1, 6)],
            _segment(6, "下一窗口", 2000, start=20, end=21),
        ],
    }
    repair = {
        "segments": [
            _segment(0, "修复一", 1000, start=10, end=11, words=[{"word": "修复一"}]),
            _segment(1, "修复二", 1000, start=11, end=12),
        ]
    }
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return first if len(calls) == 1 else repair

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(audio, output, _mlx_settings())

    assert len(calls) == 2
    assert calls[1] == {
        "path": str(audio),
        "path_or_hf_repo": "mlx-community/whisper-large-v3-turbo",
        "language": "zh",
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "hallucination_silence_threshold": 2.0,
        "clip_timestamps": "10.0,40.0",
    }
    assert [segment["text"] for segment in result["segments"]] == [
        "开头",
        "修复一",
        "修复二",
        "下一窗口",
    ]
    assert [segment["id"] for segment in result["segments"]] == list(range(4))
    assert result["segments"][1]["words"] == [{"word": "修复一"}]
    assert result["text"] == "开头修复一修复二下一窗口"
    assert result["language"] == "zh"
    assert read_json(output) == result


def test_transcribe_audio_repairs_each_unique_seek_once_in_seek_order(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {
        "segments": [
            *[_segment(index, "第一组", 300, start=3 + index / 10, end=3.05 + index / 10) for index in range(5)],
            _segment(5, "间隔", 400, start=4, end=4.1),
            *[_segment(6 + index, "第二组", 100, start=1 + index / 10, end=1.05 + index / 10) for index in range(5)],
            _segment(11, "分隔", 100, start=1.6, end=1.7),
            *[_segment(12 + index, "第三组", 100, start=1.8 + index / 100, end=1.81 + index / 100) for index in range(5)],
        ]
    }
    repairs = {
        "1.0,31.0": {"segments": [_segment(0, "一百", 100, start=1, end=1.2)]},
        "3.0,33.0": {"segments": [_segment(0, "三百", 300, start=3, end=3.2)]},
    }
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append({"path": path, **kwargs})
        if len(calls) == 1:
            return first
        return repairs[kwargs["clip_timestamps"]]

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(audio, output, _mlx_settings())

    assert [call["clip_timestamps"] for call in calls[1:]] == ["1.0,31.0", "3.0,33.0"]
    assert [segment["text"] for segment in result["segments"]] == ["一百", "三百", "间隔"]


def test_transcribe_audio_filters_invalid_and_cross_boundary_replacements(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {
        "segments": [
            *[_segment(index, "重复", 1000, start=10 + index / 10, end=10.05 + index / 10) for index in range(5)],
            _segment(5, "下一窗口", 1200, start=12, end=12.2),
        ]
    }
    repair = {
        "segments": [
            _segment(0, "有效", 1000, start=10, end=10.5),
            _segment(1, " ", 1000, start=10.5, end=10.6),
            _segment(2, "零长度", 1000, start=10.7, end=10.7),
            _segment(3, "NaN", 1000, start=float("nan"), end=11),
            _segment(4, "Infinity", 1000, start=11, end=float("inf")),
            _segment(5, "跨界", 1000, start=11.5, end=12.03),
        ]
    }
    calls = 0

    def fake_transcribe(path, **kwargs):
        nonlocal calls
        calls += 1
        return first if calls == 1 else repair

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    result = transcribe_audio(audio, output, _mlx_settings())

    assert [segment["text"] for segment in result["segments"]] == ["有效", "下一窗口"]


def test_transcribe_audio_rejects_failed_repair_without_writing(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {"segments": [_segment(index, "第一遍重复", 0) for index in range(5)]}
    repair = {"segments": [_segment(index, "第二遍仍重复", 0) for index in range(5)]}
    calls = 0

    def fake_transcribe(path, **kwargs):
        nonlocal calls
        calls += 1
        return first if calls == 1 else repair

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(transcribe_module, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    with pytest.raises(RuntimeError, match="repeated segments remain"):
        transcribe_audio(audio, output, _mlx_settings())

    assert not output.exists()


def test_transcribe_audio_rejects_repetition_without_seek(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    output = tmp_path / "transcript_raw.json"
    audio.write_bytes(b"wav")
    first = {
        "segments": [
            {"start": float(index), "end": float(index + 1), "text": "无 seek 重复"}
            for index in range(5)
        ]
    }

    from types import SimpleNamespace

    from live_clipper import transcribe as transcribe_module

    monkeypatch.setattr(
        transcribe_module,
        "mlx_whisper",
        SimpleNamespace(transcribe=lambda path, **kwargs: first),
    )

    with pytest.raises(RuntimeError, match="no valid seek"):
        transcribe_audio(audio, output, _mlx_settings())

    assert not output.exists()


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
