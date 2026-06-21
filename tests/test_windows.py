from __future__ import annotations

import pytest

from live_clipper.models import CorrectedTranscript, TranscriptSentence
from live_clipper.windows import split_transcript_into_windows, write_windows_file
from live_clipper.utils import read_json


def test_split_transcript_into_overlapping_windows():
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=0, end=10, text="开场"),
        TranscriptSentence(start=230, end=250, text="第一段边界"),
        TranscriptSentence(start=260, end=270, text="第二段"),
        TranscriptSentence(start=470, end=490, text="第三段边界"),
    ])

    windows = split_transcript_into_windows(transcript, window_seconds=240, overlap_seconds=30)

    assert [window.id for window in windows] == ["w0001", "w0002", "w0003"]
    assert [(window.start, window.end) for window in windows] == [
        (0.0, 240.0),
        (210.0, 450.0),
        (420.0, 490.0),
    ]
    assert [sentence.text for sentence in windows[0].sentences] == ["开场", "第一段边界"]
    assert [sentence.text for sentence in windows[1].sentences] == ["第一段边界", "第二段"]
    assert [sentence.text for sentence in windows[2].sentences] == ["第三段边界"]


def test_write_windows_file_serializes_windows(tmp_path):
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=0, end=10, text="开场"),
    ])
    output_path = tmp_path / "windows.json"

    windows = write_windows_file(transcript, output_path)

    assert len(windows) == 1
    assert read_json(output_path)[0]["id"] == "w0001"


def test_split_transcript_rejects_invalid_window_parameters():
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=0, end=10, text="开场"),
    ])

    with pytest.raises(ValueError, match="window_seconds"):
        split_transcript_into_windows(transcript, window_seconds=0, overlap_seconds=0)

    with pytest.raises(ValueError, match="overlap_seconds"):
        split_transcript_into_windows(transcript, window_seconds=60, overlap_seconds=60)
