from __future__ import annotations

from live_clipper.models import CorrectedTranscript, SelectedClip, TranscriptSentence
from live_clipper.subtitles import write_srt_file


def test_write_srt_file_uses_clip_relative_timestamps(tmp_path):
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=8, end=9, text="太早"),
        TranscriptSentence(start=10, end=12.5, text="第一句"),
        TranscriptSentence(start=13, end=15, text="第二句"),
        TranscriptSentence(start=31, end=32, text="太晚"),
    ])
    clip = SelectedClip(clip_id="clip-1", source_start=10, source_end=30, title="标题")
    output_path = tmp_path / "clip-1.srt"

    write_srt_file(transcript, clip, output_path)

    assert output_path.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:02,500\n"
        "第一句\n\n"
        "2\n"
        "00:00:03,000 --> 00:00:05,000\n"
        "第二句\n"
    )


def test_write_srt_file_maps_timestamps_after_remove_ranges(tmp_path):
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="保留前段"),
        TranscriptSentence(start=12.5, end=13.5, text="删除内容"),
        TranscriptSentence(start=14, end=16, text="保留后段"),
    ])
    clip = SelectedClip(
        clip_id="clip-1",
        source_start=10,
        source_end=18,
        title="标题",
        remove_ranges=[(12, 14)],
    )
    output_path = tmp_path / "clip-1.srt"

    write_srt_file(transcript, clip, output_path)

    assert output_path.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "保留前段\n\n"
        "2\n"
        "00:00:02,000 --> 00:00:04,000\n"
        "保留后段\n"
    )
