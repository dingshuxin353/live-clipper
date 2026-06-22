from __future__ import annotations

import subprocess

import pytest

from live_clipper.models import CorrectedTranscript, SelectedClip, TranscriptSentence
from live_clipper.render_clips import render_selected_clips
from live_clipper.utils import read_json, write_json


def test_render_selected_clips_writes_edl_subtitle_invokes_ffmpeg_and_reports_progress(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "week_023"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"video")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": "source.mp4",
    })
    write_json(run_dir / "transcript.json", CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="字幕"),
    ]).model_dump())
    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
        ).model_dump()
    ])
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)
        output_path = cmd[-1]
        with open(output_path, "wb") as handle:
            handle.write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rendered = render_selected_clips(selection_path)
    output = capsys.readouterr().out

    assert rendered == [run_dir / "clips" / "clip-1.mp4"]
    assert "[渲染] 开始: 共 1 条入选片段" in output
    assert "[渲染] 1/1 clip-1: 10.00s - 20.00s, 原始时长 10.00s" in output
    assert f"[渲染] 1/1 clip-1: 完成 -> {run_dir / 'clips' / 'clip-1.mp4'}" in output
    assert f"[渲染] 全部完成: 1 条成片 -> {run_dir / 'clips'}" in output
    edl_item = read_json(run_dir / "edit_decision_list.json")[0]
    assert edl_item["clip_id"] == "clip-1"
    assert edl_item["remove_ranges_applied"] is False
    assert edl_item["warnings"] == []
    assert (run_dir / "subtitles" / "clip-1.srt").exists()
    assert calls == [[
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "10.000",
        "-to",
        "20.000",
        "-i",
        str(source_video),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(run_dir / "clips" / "clip-1.mp4"),
    ]]


def test_render_selected_clips_applies_remove_ranges_with_concat(tmp_path, monkeypatch):
    run_dir = tmp_path / "week_023"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"video")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": "source.mp4",
    })
    write_json(run_dir / "transcript.json", CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="字幕"),
    ]).model_dump())
    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
            remove_ranges=[(12, 13)],
        ).model_dump()
    ])
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)
        output_path = cmd[-1]
        with open(output_path, "wb") as handle:
            handle.write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    render_selected_clips(selection_path)

    edl_item = read_json(run_dir / "edit_decision_list.json")[0]
    assert edl_item["remove_ranges"] == [[12.0, 13.0]]
    assert edl_item["remove_ranges_applied"] is True
    assert edl_item["warnings"] == []
    assert all(cmd[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "error"] for cmd in calls)
    assert calls[0][calls[0].index("-ss") + 1] == "10.000"
    assert calls[0][calls[0].index("-to") + 1] == "12.000"
    assert calls[1][calls[1].index("-ss") + 1] == "13.000"
    assert calls[1][calls[1].index("-to") + 1] == "20.000"
    assert "-f" in calls[2]
    assert "concat" in calls[2]


def test_render_selected_clips_reencodes_removed_range_segments_with_reset_timestamps(tmp_path, monkeypatch):
    run_dir = tmp_path / "week_023"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"video")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": "source.mp4",
    })
    write_json(run_dir / "transcript.json", CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="字幕"),
    ]).model_dump())
    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
            remove_ranges=[(12, 13)],
        ).model_dump()
    ])
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)
        output_path = cmd[-1]
        with open(output_path, "wb") as handle:
            handle.write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    render_selected_clips(selection_path)

    segment_commands = calls[:2]
    assert all("-c" not in cmd for cmd in segment_commands)
    assert all(cmd[cmd.index("-vf") + 1] == "setpts=PTS-STARTPTS" for cmd in segment_commands)
    assert all(cmd[cmd.index("-af") + 1] == "asetpts=PTS-STARTPTS" for cmd in segment_commands)
    assert all(cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero" for cmd in segment_commands)


def test_render_selected_clips_reports_missing_run_metadata(tmp_path):
    selection_path = tmp_path / "week_023" / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
        ).model_dump()
    ])

    with pytest.raises(FileNotFoundError, match="run_metadata.json"):
        render_selected_clips(selection_path)


def test_render_selected_clips_reports_missing_source_video(tmp_path):
    run_dir = tmp_path / "week_023"
    source_video = tmp_path / "missing.mp4"
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": "missing.mp4",
    })
    write_json(run_dir / "transcript.json", CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="字幕"),
    ]).model_dump())
    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
        ).model_dump()
    ])

    with pytest.raises(FileNotFoundError, match="missing.mp4"):
        render_selected_clips(selection_path)


def test_render_selected_clips_reports_missing_ffmpeg(tmp_path, monkeypatch):
    run_dir = tmp_path / "week_023"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"video")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": "source.mp4",
    })
    write_json(run_dir / "transcript.json", CorrectedTranscript(sentences=[
        TranscriptSentence(start=10, end=12, text="字幕"),
    ]).model_dump())
    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="clip-1",
            source_start=10,
            source_end=20,
            title="标题",
        ).model_dump()
    ])

    def fake_run(cmd, check):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg is required"):
        render_selected_clips(selection_path)
