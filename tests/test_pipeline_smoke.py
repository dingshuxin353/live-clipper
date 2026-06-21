from __future__ import annotations

import subprocess

from live_clipper import cli
from live_clipper.config import Settings
from live_clipper.models import SelectedClip
from live_clipper.utils import read_json, write_json


class FakeCheapModelClient:
    def __init__(self, settings):
        self.settings = settings

    def complete_json(self, system_prompt, user_payload, max_tokens=2048):
        if "glossary" in user_payload:
            return {
                "sentences": [
                    {
                        "start": sentence["start"],
                        "end": sentence["end"],
                        "text": sentence["text"].replace("ThemPad", "ffmpeg"),
                        "speaker": sentence.get("speaker"),
                    }
                    for sentence in user_payload["sentences"]
                ],
                "corrections": [
                    {
                        "start": 0.0,
                        "end": 4.0,
                        "original_text": "ThemPad",
                        "corrected_text": "ffmpeg",
                        "reason": "glossary",
                        "confidence": 0.9,
                    }
                ],
            }
        return {
            "window_id": user_payload["id"],
            "candidates": [
                {
                    "start": 0.0,
                    "end": 4.0,
                    "score": 8.0,
                    "clip_type": "insight",
                    "hook": "直播切片测试",
                    "core_value": "验证流水线串通",
                    "reason": "内容完整",
                    "risk": None,
                    "suggested_context_before": 0,
                    "suggested_context_after": 0,
                }
            ],
        }


def test_scan_brief_render_pipeline_smoke(tmp_path, monkeypatch):
    source_video = tmp_path / "source.mp4"
    run_dir = tmp_path / "run"
    source_video.write_bytes(b"video")

    def fake_extract_audio(video_path, output_wav_path):
        output_wav_path.parent.mkdir(parents=True, exist_ok=True)
        output_wav_path.write_bytes(b"wav")
        return output_wav_path

    def fake_transcribe_audio(audio_path, output_json_path, settings):
        raw = {
            "text": "我们用 ThemPad 渲染",
            "language": "zh",
            "segments": [
                {"start": 0.0, "end": 4.0, "text": "我们用 ThemPad 渲染"},
            ],
        }
        write_json(output_json_path, raw)
        return raw

    def fake_run(cmd, check):
        output_path = cmd[-1]
        with open(output_path, "wb") as handle:
            handle.write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(cli, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(cli, "CheapModelClient", FakeCheapModelClient)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))
    monkeypatch.setattr("live_clipper.render_clips.subprocess.run", fake_run)

    cli.run_scan(source_video, run_dir)
    cli.run_brief(run_dir)
    write_json(run_dir / "selected_clips.json", [
        SelectedClip(
            clip_id="w0001-c001",
            source_start=0.0,
            source_end=4.0,
            title="测试片段",
        ).model_dump()
    ])
    cli.run_render(run_dir / "selected_clips.json")

    expected_files = [
        "run_metadata.json",
        "audio.wav",
        "transcript_raw.json",
        "transcript.json",
        "windows.json",
        "cheap_candidates.json",
        "merged_candidates.json",
        "codex_brief.json",
        "selected_clips.json",
        "edit_decision_list.json",
        "subtitles/w0001-c001.srt",
        "clips/w0001-c001.mp4",
    ]
    for relative_path in expected_files:
        assert (run_dir / relative_path).exists(), relative_path

    assert read_json(run_dir / "transcript.json")["sentences"][0]["text"] == "我们用 ffmpeg 渲染"
    assert read_json(run_dir / "codex_brief.json")["candidates"][0]["id"] == "w0001-c001"
