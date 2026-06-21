from __future__ import annotations

import subprocess

import pytest

from live_clipper.video import extract_audio


def test_extract_audio_invokes_ffmpeg_for_16k_mono_wav(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    output = tmp_path / "run" / "audio.wav"
    source.write_bytes(b"fake-video")
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wav")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = extract_audio(source, output)

    assert result == output
    assert output.read_bytes() == b"wav"
    assert calls == [[
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(output),
    ]]


def test_extract_audio_reports_missing_ffmpeg(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    output = tmp_path / "run" / "audio.wav"
    source.write_bytes(b"fake-video")

    def fake_run(cmd, check):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg is required"):
        extract_audio(source, output)
