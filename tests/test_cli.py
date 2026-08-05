from __future__ import annotations

import pytest

from live_clipper import cli
from live_clipper.config import Settings
from live_clipper.utils import read_json, write_json


def test_run_scan_wires_pipeline_and_writes_metadata(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    calls = []

    def fake_extract_audio(source, output):
        calls.append(("extract", source, output))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wav")
        return output

    def fake_transcribe(audio, output, settings):
        calls.append(("transcribe", audio, output))
        write_json(output, {"segments": [{"start": 0, "end": 1, "text": "原文"}]})
        return {"segments": [{"start": 0, "end": 1, "text": "原文"}]}

    def fake_correct(raw, glossary, output, client, *, resume=False):
        calls.append(("correct", raw, glossary, output, resume))
        from live_clipper.models import CorrectedTranscript, TranscriptSentence
        corrected = CorrectedTranscript(sentences=[TranscriptSentence(start=0, end=1, text="正文")])
        write_json(output, corrected.model_dump())
        return corrected

    def fake_windows(transcript, output):
        calls.append(("windows", output))
        write_json(output, [])
        return []

    def fake_scan(windows, output, client, *, resume=False):
        calls.append(("scan_windows", windows, output, resume))
        write_json(output, [])
        return []

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    monkeypatch.setattr(cli, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(cli, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(cli, "correct_transcript_file", fake_correct)
    monkeypatch.setattr(cli, "write_windows_file", fake_windows)
    monkeypatch.setattr(cli, "scan_windows_file", fake_scan)
    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", lambda settings: object())
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir)

    metadata = read_json(output_dir / "run_metadata.json")
    assert metadata["source_video_path"] == str(video_path)
    assert metadata["source_name"] == "source.mp4"
    assert metadata["asr"] == {
        "backend": "mlx_whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
    }
    assert metadata["cheap_model"] == {
        "api_base": "https://apihub.agnes-ai.com/v1",
        "model": "agnes-2.0-flash",
    }
    assert metadata["glossary_path"] == "glossary/common_terms.example.json"
    assert "secret" not in str(metadata)
    assert "api_key" not in str(metadata).lower()
    assert calls[0][0] == "extract"
    assert calls[2][2] == cli.resolve_glossary_path()
    assert calls[-1][0] == "merge"


def test_run_scan_resume_skips_existing_intermediate_files(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    write_json(output_dir / "transcript.json", {"sentences": [{"start": 0, "end": 1, "text": "正文"}], "corrections": []})
    write_json(output_dir / "windows.json", [])
    write_json(output_dir / "cheap_candidates.json", [])
    (output_dir / "audio.wav").write_bytes(b"wav")
    write_json(output_dir / "transcript_raw.json", {"segments": [{"start": 0, "end": 1, "text": "原文"}]})
    calls = []

    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append(("extract", source, output)))
    monkeypatch.setattr(cli, "transcribe_audio", lambda audio, output, settings: calls.append(("transcribe", audio, output)))
    monkeypatch.setattr(cli, "correct_transcript_file", lambda raw, glossary, output, client, *, resume=False: calls.append(("correct", raw, glossary, output, resume)))
    monkeypatch.setattr(cli, "write_windows_file", lambda transcript, output: calls.append(("windows", output)))
    monkeypatch.setattr(cli, "scan_windows_file", lambda windows, output, client, *, resume=False: calls.append(("scan_windows", windows, output, resume)))

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", lambda settings: object())
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True)

    assert calls == [("merge", output_dir / "cheap_candidates.json", output_dir / "merged_candidates.json")]
    assert read_json(output_dir / "run_metadata.json")["resume"] is True


def test_run_scan_resume_can_merge_existing_candidates_without_model_key_or_asr(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    write_json(output_dir / "transcript.json", {
        "sentences": [{"start": 0, "end": 1, "text": "正文"}],
        "corrections": [],
    })
    write_json(output_dir / "windows.json", [])
    write_json(output_dir / "cheap_candidates.json", [])
    calls = []

    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append(("extract", source, output)))
    monkeypatch.setattr(cli, "transcribe_audio", lambda audio, output, settings: calls.append(("transcribe", audio, output)))
    monkeypatch.setattr(cli, "correct_transcript_file", lambda raw, glossary, output, client, *, resume=False: calls.append(("correct", raw, glossary, output, resume)))
    monkeypatch.setattr(cli, "write_windows_file", lambda transcript, output: calls.append(("windows", output)))
    monkeypatch.setattr(cli, "scan_windows_file", lambda windows, output, client, *, resume=False: calls.append(("scan_windows", windows, output, resume)))

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    def fail_client(settings):
        raise AssertionError("cheap model client should not be created")

    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", fail_client)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key=None,
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True)

    assert calls == [("merge", output_dir / "cheap_candidates.json", output_dir / "merged_candidates.json")]
    assert read_json(output_dir / "run_metadata.json")["resume"] is True


def test_run_scan_reports_stage_progress_for_resumed_run(tmp_path, monkeypatch, capsys):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    write_json(output_dir / "transcript.json", {
        "sentences": [{"start": 0, "end": 1, "text": "正文"}],
        "corrections": [],
    })
    write_json(output_dir / "windows.json", [])
    write_json(output_dir / "cheap_candidates.json", [])

    def fake_merge(input_path, output):
        write_json(output, [])
        return []

    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key=None,
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True)

    output = capsys.readouterr().out
    assert f"[扫描] 输入视频: {video_path}" in output
    assert f"[扫描] 输出目录: {output_dir}" in output
    assert f"[扫描] 3/6 生成文字稿: 复用已有文件 -> {output_dir / 'transcript.json'}" in output
    assert f"[扫描] 5/6 Agnes粗扫候选片段: 复用已有文件 -> {output_dir / 'cheap_candidates.json'}" in output
    assert "[扫描] 6/6 合并候选片段: 开始" in output
    assert f"[扫描] 全部完成: {output_dir}" in output


def test_web_command_defaults_to_localhost():
    args = cli.build_parser().parse_args(["web"])

    assert args.host == "127.0.0.1"
    assert args.port == 8765


def test_web_command_allows_explicit_lan_host():
    args = cli.build_parser().parse_args(["web", "--host", "0.0.0.0"])

    assert args.host == "0.0.0.0"


def test_config_init_writes_default_config(tmp_path):
    output_path = tmp_path / "live-clipper.toml"

    result = cli.run_config_init(output_path)

    assert result == output_path
    assert "[paths]" in output_path.read_text(encoding="utf-8")


def test_prompts_export_writes_prompt_files(tmp_path):
    output_dir = tmp_path / "prompts.local"

    exported = cli.run_prompts_export(output_dir)

    assert output_dir / "cheap_scan_window.md" in exported
    assert (output_dir / "codex_select_clips.md").exists()


def test_guide_ai_parser_accepts_output_path(tmp_path):
    output_path = tmp_path / "my-ai-guide.md"

    args = cli.build_parser().parse_args(["guide", "ai", "--output", str(output_path)])

    assert args.command == "guide"
    assert args.guide_command == "ai"
    assert args.output == output_path


def test_run_ai_guide_outputs_chinese_safety_and_codex_tasks(tmp_path, capsys):
    output_path = tmp_path / "my-ai-guide.md"

    text = cli.run_ai_guide(output_path)

    assert output_path.read_text(encoding="utf-8") == text
    assert "不要把 API key" in text
    assert "录制检测任务" in text
    assert "选片与收尾任务" in text
    assert "一次只问" in text
    assert str(output_path) in capsys.readouterr().out


def test_setup_parser_accepts_safe_overwrite_flags():
    args = cli.build_parser().parse_args(["setup", "--force-config", "--force-prompts"])

    assert args.command == "setup"
    assert args.force_config is True
    assert args.force_prompts is True


def test_run_setup_creates_beginner_files_without_collecting_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    report = cli.run_setup()

    assert report["config_path"] == "live-clipper.toml"
    assert report["env_path"] == ".env"
    assert (tmp_path / "live-clipper.toml").exists()
    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith("# live-clipper environment")
    assert "CHEAP_MODEL_API_KEY=" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert (tmp_path / "input").is_dir()
    assert (tmp_path / "output").is_dir()
    assert (tmp_path / "work" / "logs").is_dir()
    assert (tmp_path / "prompts.local" / "codex_select_clips.md").exists()
    assert "不要把 API key 粘贴到聊天窗口" in capsys.readouterr().out


def test_next_parser_accepts_output_root(tmp_path):
    args = cli.build_parser().parse_args(["next", "--output-root", str(tmp_path / "output")])

    assert args.command == "next"
    assert args.output_root == tmp_path / "output"


def test_service_parser_accepts_start_status_and_logs_flags():
    start = cli.build_parser().parse_args(["service", "start", "--foreground", "--once"])
    status = cli.build_parser().parse_args(["service", "status", "--json"])
    logs = cli.build_parser().parse_args(["service", "logs", "--follow"])

    assert start.command == "service"
    assert start.service_command == "start"
    assert start.foreground is True
    assert start.once is True
    assert status.json is True
    assert logs.follow is True


def test_follow_service_logs_prints_existing_content(tmp_path, monkeypatch, capsys):
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "service.log").write_text("one\ntwo\n", encoding="utf-8")

    def stop_after_first_poll(poll_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop_after_first_poll)

    cli.follow_service_logs(service_dir=service_dir, poll_seconds=0)

    assert capsys.readouterr().out == "one\ntwo\n"


def test_run_next_reports_codex_selection_step(tmp_path, capsys):
    run_dir = tmp_path / "output" / "week_023"
    write_json(run_dir / "run_metadata.json", {"source_name": "week_023.mp4"})
    write_json(run_dir / "transcript_raw.json", {"segments": []})
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "refined_candidates.json", [])
    write_json(run_dir / "codex_brief.json", {"candidates": []})

    report = cli.run_next(tmp_path / "output")

    assert report["actionable_count"] == 1
    assert report["runs"][0]["run_dir"] == str(run_dir)
    output = capsys.readouterr().out
    assert "等待 Codex 或人工选片" in output
    assert "selected_clips.json" in output


def test_run_scan_resume_passes_resume_to_window_scan(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    write_json(output_dir / "transcript.json", {
        "sentences": [{"start": 0, "end": 1, "text": "正文"}],
        "corrections": [],
    })
    write_json(output_dir / "windows.json", [])
    calls = []

    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append(("extract", source, output)))
    monkeypatch.setattr(cli, "transcribe_audio", lambda audio, output, settings: calls.append(("transcribe", audio, output)))
    monkeypatch.setattr(cli, "correct_transcript_file", lambda raw, glossary, output, client, *, resume=False: calls.append(("correct", raw, glossary, output, resume)))
    monkeypatch.setattr(cli, "write_windows_file", lambda transcript, output: calls.append(("windows", output)))

    def fake_scan(windows, output, client, *, resume=False):
        calls.append(("scan_windows", windows, output, resume))
        write_json(output, [])
        return []

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    monkeypatch.setattr(cli, "scan_windows_file", fake_scan)
    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", lambda settings: object())
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True)

    assert calls == [
        ("scan_windows", output_dir / "windows.json", output_dir / "cheap_candidates.json", True),
        ("merge", output_dir / "cheap_candidates.json", output_dir / "merged_candidates.json"),
    ]


def test_run_scan_resume_passes_resume_to_transcript_correction(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    output_dir.mkdir()
    (output_dir / "audio.wav").write_bytes(b"wav")
    write_json(output_dir / "transcript_raw.json", {"segments": [{"start": 0, "end": 1, "text": "原文"}]})
    write_json(output_dir / "cheap_candidates.json", [])
    calls = []

    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append(("extract", source, output)))
    monkeypatch.setattr(cli, "transcribe_audio", lambda audio, output, settings: calls.append(("transcribe", audio, output)))

    def fake_correct(raw, glossary, output, client, *, resume=False):
        calls.append(("correct", raw, output, resume))
        from live_clipper.models import CorrectedTranscript, TranscriptSentence
        corrected = CorrectedTranscript(sentences=[TranscriptSentence(start=0, end=1, text="正文")])
        write_json(output, corrected.model_dump())
        return corrected

    def fake_windows(transcript, output):
        calls.append(("windows", output))
        write_json(output, [])
        return []

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    monkeypatch.setattr(cli, "correct_transcript_file", fake_correct)
    monkeypatch.setattr(cli, "write_windows_file", fake_windows)
    monkeypatch.setattr(cli, "scan_windows_file", lambda windows, output, client, *, resume=False: calls.append(("scan_windows", windows, output, resume)))
    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", lambda settings: object())
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True)

    assert ("correct", output_dir / "transcript_raw.json", output_dir / "transcript.json", True) in calls
    assert ("scan_windows", output_dir / "windows.json", output_dir / "cheap_candidates.json", True) not in calls


def test_run_scan_skip_transcript_correction_writes_raw_transcript_without_model_key(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_dir = tmp_path / "run"
    video_path.write_bytes(b"video")
    output_dir.mkdir()
    write_json(output_dir / "transcript_raw.json", {
        "segments": [
            {"start": 0, "end": 1, "text": "原文"},
            {"start": 1, "end": 2, "text": "   "},
        ]
    })
    write_json(output_dir / "cheap_candidates.json", [])
    calls = []

    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append(("extract", source, output)))
    monkeypatch.setattr(cli, "transcribe_audio", lambda audio, output, settings: calls.append(("transcribe", audio, output)))
    monkeypatch.setattr(cli, "correct_transcript_file", lambda raw, glossary, output, client, *, resume=False: calls.append(("correct", raw, output, resume)))

    def fake_windows(transcript, output):
        calls.append(("windows", output, len(transcript.sentences)))
        write_json(output, [])
        return []

    def fake_merge(input_path, output):
        calls.append(("merge", input_path, output))
        write_json(output, [])
        return []

    def fail_client(settings):
        raise AssertionError("cheap model client should not be created")

    monkeypatch.setattr(cli, "write_windows_file", fake_windows)
    monkeypatch.setattr(cli, "scan_windows_file", lambda windows, output, client, *, resume=False: calls.append(("scan_windows", windows, output, resume)))
    monkeypatch.setattr(cli, "merge_candidates_file", fake_merge)
    monkeypatch.setattr(cli, "CheapModelClient", fail_client)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key=None,
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))

    cli.run_scan(video_path, output_dir, resume=True, skip_transcript_correction=True)

    assert read_json(output_dir / "transcript.json") == {
        "sentences": [{"start": 0.0, "end": 1.0, "text": "原文", "speaker": None}],
        "corrections": [],
    }
    assert calls == [
        ("windows", output_dir / "windows.json", 1),
        ("merge", output_dir / "cheap_candidates.json", output_dir / "merged_candidates.json"),
    ]
    assert read_json(output_dir / "run_metadata.json")["transcript_correction"] == "skipped"


def test_run_brief_uses_run_files(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    write_json(run_dir / "run_metadata.json", {"source_name": "source.mp4"})
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    calls = []

    def fake_build(candidates, transcript, output, source_name):
        calls.append((candidates, transcript, output, source_name))
        write_json(output, {"source_name": source_name, "candidates": []})
        return {"source_name": source_name, "candidates": []}

    monkeypatch.setattr(cli, "build_codex_brief_file", fake_build)
    monkeypatch.setattr(cli, "build_codex_review_markdown", lambda brief, brief_path, selection_path: "review")
    monkeypatch.setattr(cli, "build_selected_clips_template", lambda brief: [{"clip_id": "clip-1"}])

    cli.run_brief(run_dir)

    assert calls == [(
        run_dir / "merged_candidates.json",
        run_dir / "transcript.json",
        run_dir / "codex_brief.json",
        "source.mp4",
    )]
    assert (run_dir / "codex_review.md").read_text(encoding="utf-8") == "review"
    assert read_json(run_dir / "selected_clips.template.json") == [{"clip_id": "clip-1"}]


def test_run_brief_can_use_refined_candidates(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    write_json(run_dir / "run_metadata.json", {"source_name": "source.mp4"})
    write_json(run_dir / "refined_candidates.json", [])
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    calls = []

    def fake_build(candidates, transcript, output, source_name):
        calls.append((candidates, transcript, output, source_name))
        write_json(output, {"source_name": source_name, "candidates": []})
        return {"source_name": source_name, "candidates": []}

    monkeypatch.setattr(cli, "build_codex_brief_file", fake_build)
    monkeypatch.setattr(cli, "build_codex_review_markdown", lambda brief, brief_path, selection_path: "review")
    monkeypatch.setattr(cli, "build_selected_clips_template", lambda brief: [])

    cli.run_brief(run_dir, source="refined")

    assert calls == [(
        run_dir / "refined_candidates.json",
        run_dir / "transcript.json",
        run_dir / "codex_brief.json",
        "source.mp4",
    )]


def test_run_brief_reports_missing_required_files(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    write_json(run_dir / "run_metadata.json", {"source_name": "source.mp4"})
    calls = []
    monkeypatch.setattr(cli, "build_codex_brief_file", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(FileNotFoundError, match="merged_candidates.json"):
        cli.run_brief(run_dir)

    write_json(run_dir / "merged_candidates.json", [])
    with pytest.raises(FileNotFoundError, match="transcript.json"):
        cli.run_brief(run_dir)

    assert calls == []


def test_run_refine_uses_agnes_client_and_writes_refined_candidates(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    write_json(run_dir / "merged_candidates.json", [])
    write_json(run_dir / "transcript.json", {"sentences": [], "corrections": []})
    calls = []

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))
    monkeypatch.setattr(cli, "CheapModelClient", lambda settings: "client")
    monkeypatch.setattr(
        cli,
        "refine_candidates_file",
        lambda candidates, transcript, output, client, *, top_n=25: calls.append((
            candidates,
            transcript,
            output,
            client,
            top_n,
        )) or write_json(output, []),
    )

    output_path = cli.run_refine(run_dir, top_n=7)

    assert output_path == run_dir / "refined_candidates.json"
    assert calls == [(
        run_dir / "merged_candidates.json",
        run_dir / "transcript.json",
        run_dir / "refined_candidates.json",
        "client",
        7,
    )]


def test_run_pipeline_stages_source_scans_refines_and_builds_brief(tmp_path, monkeypatch):
    source = tmp_path / "nas" / "recording.mkv"
    input_dir = tmp_path / "input"
    run_dir = tmp_path / "output" / "recording"
    source.parent.mkdir()
    source.write_bytes(b"video")
    calls = []

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(cheap_model_api_key="test-key"))
    monkeypatch.setattr(cli, "stage_source_file", lambda source_path, input_dir: calls.append(("stage", source_path, input_dir)) or input_dir / "recording.mkv")
    monkeypatch.setattr(
        cli,
        "run_scan",
        lambda video, output_dir, resume=False, skip_transcript_correction=False: calls.append((
            "scan",
            video,
            output_dir,
            resume,
            skip_transcript_correction,
        )) or run_dir,
    )
    monkeypatch.setattr(cli, "record_pipeline_metadata", lambda run, original, local: calls.append(("metadata", run, original, local)))
    monkeypatch.setattr(cli, "run_refine", lambda run, top_n=25: calls.append(("refine", run, top_n)))
    monkeypatch.setattr(cli, "run_brief", lambda run, source="merged": calls.append(("brief", run, source)))
    monkeypatch.setattr(cli, "build_run_status", lambda run: calls.append(("status", run)))

    result = cli.run_pipeline(source, input_dir=input_dir, output_dir=run_dir, correct_transcript=False, refine=True, top_n=9)

    assert result == run_dir
    assert calls == [
        ("stage", source, input_dir),
        ("scan", input_dir / "recording.mkv", run_dir, True, True),
        ("metadata", run_dir, source, input_dir / "recording.mkv"),
        ("refine", run_dir, 9),
        ("brief", run_dir, "refined"),
        ("status", run_dir),
    ]


def test_run_cleanup_reports_preview_by_default(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    calls = []
    monkeypatch.setattr(
        cli,
        "cleanup_local_artifacts",
        lambda run, input_dir, confirm=False, force=False: calls.append((run, input_dir, confirm, force)) or {
            "deleted": [],
            "targets": [
                {
                    "path": str(run / "audio.wav"),
                    "bytes": 1024,
                    "deletable": True,
                    "reason": "test",
                }
            ],
        },
    )

    report = cli.run_cleanup(run_dir, input_dir=tmp_path / "input")

    assert calls == [(run_dir, tmp_path / "input", False, False)]
    assert report["deleted"] == []


def test_run_render_validates_then_renders(tmp_path, monkeypatch):
    selection_path = tmp_path / "run" / "selected_clips.json"
    calls = []

    monkeypatch.setattr(cli, "validate_selected_clips_file", lambda selection, candidates: calls.append(("validate", selection, candidates)) or [])
    monkeypatch.setattr(cli, "render_selected_clips", lambda selection: calls.append(("render", selection)) or [])

    cli.run_render(selection_path)

    assert calls == [
        ("validate", selection_path, selection_path.parent / "merged_candidates.json"),
        ("render", selection_path),
    ]


def test_run_scan_fails_before_audio_extraction_when_cheap_model_key_missing(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    calls = []

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key=None,
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))
    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append("extract"))

    with pytest.raises(ValueError, match="CHEAP_MODEL_API_KEY"):
        cli.run_scan(video_path, tmp_path / "run")

    assert calls == []


def test_run_pipeline_fails_before_staging_when_cheap_model_key_missing(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"video")
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(cheap_model_api_key=None))
    monkeypatch.setattr(cli, "stage_source_file", lambda *args, **kwargs: pytest.fail("must not stage"))

    with pytest.raises(ValueError, match="设置 → AI 服务"):
        cli.run_pipeline(source, input_dir=tmp_path / "input", output_dir=tmp_path / "output")

    assert not (tmp_path / "input").exists()


def test_run_scan_fails_before_creating_run_dir_when_video_is_missing(tmp_path, monkeypatch):
    video_path = tmp_path / "missing.mp4"
    output_dir = tmp_path / "run"
    calls = []

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))
    monkeypatch.setattr(cli, "extract_audio", lambda source, output: calls.append("extract"))

    with pytest.raises(FileNotFoundError, match="missing.mp4"):
        cli.run_scan(video_path, output_dir)

    assert calls == []
    assert not output_dir.exists()


def test_run_doctor_reports_deployment_readiness(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key=None,
        cheap_model_name="agnes-2.0-flash",
        asr_backend="mlx_whisper",
        asr_model="mlx-community/whisper-large-v3-turbo",
    ))
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg" if command == "ffmpeg" else None)

    report = cli.run_doctor(input_dir)

    assert report["ok"] is False
    assert report["checks"] == [
        {"name": "ffmpeg", "ok": True, "detail": "/usr/bin/ffmpeg"},
        {"name": "input_video", "ok": False, "detail": f"No supported video files found in {input_dir}"},
        {"name": "cheap_model_api_key", "ok": False, "detail": "CHEAP_MODEL_API_KEY is not set"},
        {"name": "cheap_model", "ok": True, "detail": "agnes-2.0-flash @ https://apihub.agnes-ai.com/v1"},
        {"name": "asr", "ok": True, "detail": "mlx_whisper / mlx-community/whisper-large-v3-turbo"},
        {"name": "glossary", "ok": True, "detail": "glossary/common_terms.example.json"},
        {"name": "hf_token", "ok": False, "detail": "HF_TOKEN is not set; downloads may be slower"},
    ]


def test_run_doctor_requires_asr_api_key_for_openai_backend(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "source.mp4").write_bytes(b"video")

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        cheap_model_api_base="https://apihub.agnes-ai.com/v1",
        cheap_model_api_key="cheap-secret",
        cheap_model_name="agnes-2.0-flash",
        asr_backend="openai",
        asr_api_base="https://api.openai.com/v1",
        asr_api_key=None,
        asr_model="whisper-1",
    ))
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg" if command == "ffmpeg" else None)

    report = cli.run_doctor(input_dir)

    assert report["ok"] is False
    assert {"name": "asr_api_key", "ok": False, "detail": "ASR_API_KEY is required for ASR_BACKEND=openai"} in report["checks"]


def test_resolve_glossary_path_prefers_real_terms_file(tmp_path):
    glossary_dir = tmp_path / "glossary"
    glossary_dir.mkdir()
    real_path = glossary_dir / "common_terms.json"
    example_path = glossary_dir / "common_terms.example.json"
    real_path.write_text("[]", encoding="utf-8")
    example_path.write_text("[]", encoding="utf-8")

    assert cli.resolve_glossary_path(glossary_dir) == real_path


def test_resolve_glossary_path_falls_back_to_example_file(tmp_path):
    glossary_dir = tmp_path / "glossary"
    glossary_dir.mkdir()
    example_path = glossary_dir / "common_terms.example.json"
    example_path.write_text("[]", encoding="utf-8")

    assert cli.resolve_glossary_path(glossary_dir) == example_path


def test_main_dispatches_scan_brief_and_render(tmp_path, monkeypatch):
    calls = []
    default_input_dir = cli.Path("input")
    default_output_root = cli.Path("output")
    video_path = tmp_path / "source.mp4"
    run_dir = tmp_path / "run"
    selection_path = run_dir / "selected_clips.json"

    monkeypatch.setattr(
        cli,
        "run_scan",
        lambda video, output_dir, resume=False, skip_transcript_correction=False: calls.append((
            "scan",
            video,
            output_dir,
            resume,
            skip_transcript_correction,
        )),
    )
    monkeypatch.setattr(cli, "run_refine", lambda path, top_n=25: calls.append(("refine", path, top_n)))
    monkeypatch.setattr(cli, "run_brief", lambda path, source="merged": calls.append(("brief", path, source)))
    monkeypatch.setattr(cli, "run_render", lambda path: calls.append(("render", path)))
    monkeypatch.setattr(
        cli,
        "run_pipeline",
        lambda source, input_dir=default_input_dir, output_dir=None, correct_transcript=False, refine=False, top_n=25: calls.append((
            "pipeline",
            source,
            input_dir,
            output_dir,
            correct_transcript,
            refine,
            top_n,
        )),
    )
    monkeypatch.setattr(cli, "run_cleanup", lambda path, input_dir=default_input_dir, confirm=False, force=False: calls.append(("cleanup", path, input_dir, confirm, force)) or {"deleted": []})
    monkeypatch.setattr(cli, "run_doctor", lambda input_dir: calls.append(("doctor", input_dir)) or {"ok": True, "checks": []})
    monkeypatch.setattr(cli, "run_local_smoke", lambda output_dir: calls.append(("smoke", output_dir)) or {"ok": True})
    monkeypatch.setattr(cli, "build_run_status", lambda path: calls.append(("status", path)) or {"ok": True})
    monkeypatch.setattr(
        cli,
        "start_latest_recording_job",
        lambda source_dir, input_dir=default_input_dir, output_root=default_output_root, since_hours=36, min_age_minutes=10, refine=True, top_n=25, correct_transcript=False: calls.append((
            "automation-start",
            source_dir,
            input_dir,
            output_root,
            since_hours,
            min_age_minutes,
            refine,
            top_n,
            correct_transcript,
        )) or {"ok": True},
    )
    monkeypatch.setattr(cli, "check_automation_runs", lambda output_root: calls.append(("automation-check", output_root)) or {"ok": True})

    monkeypatch.setattr("sys.argv", ["live-clipper", "doctor", "--input-dir", str(tmp_path / "input")])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "smoke", "--output-dir", str(tmp_path / "smoke")])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "status", str(run_dir)])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "pipeline", str(video_path), "--input-dir", str(tmp_path / "input"), "--output-dir", str(run_dir), "--refine", "--top-n", "7"])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "scan", str(video_path), "--output-dir", str(run_dir), "--resume"])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "refine", str(run_dir), "--top-n", "7"])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "brief", str(run_dir), "--source", "refined"])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "render", str(selection_path)])
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "cleanup", str(run_dir), "--input-dir", str(tmp_path / "input"), "--confirm"])
    cli.main()
    monkeypatch.setattr(
        "sys.argv",
        [
            "live-clipper",
            "automation",
            "start-latest",
            "--source-dir",
            str(tmp_path / "nas"),
            "--input-dir",
            str(tmp_path / "input"),
            "--output-root",
            str(tmp_path / "output"),
            "--since-hours",
            "48",
            "--min-age-minutes",
            "20",
            "--top-n",
            "9",
        ],
    )
    cli.main()
    monkeypatch.setattr("sys.argv", ["live-clipper", "automation", "check", "--output-root", str(tmp_path / "output")])
    cli.main()

    assert calls == [
        ("doctor", tmp_path / "input"),
        ("smoke", tmp_path / "smoke"),
        ("status", run_dir),
        ("pipeline", video_path, tmp_path / "input", run_dir, False, True, 7),
        ("scan", video_path, run_dir, True, False),
        ("refine", run_dir, 7),
        ("brief", run_dir, "refined"),
        ("render", selection_path),
        ("cleanup", run_dir, tmp_path / "input", True, False),
        ("automation-start", tmp_path / "nas", tmp_path / "input", tmp_path / "output", 48, 20, True, 9, False),
        ("automation-check", tmp_path / "output"),
    ]


def test_main_reports_cheap_model_service_errors_without_traceback(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    run_dir = tmp_path / "run"

    def fail_scan(video, output_dir, resume=False, skip_transcript_correction=False):
        raise cli.CheapModelServiceError("Cheap model request failed after 5/5 attempt(s): SSLError")

    monkeypatch.setattr(cli, "run_scan", fail_scan)
    monkeypatch.setattr("sys.argv", ["live-clipper", "scan", str(video_path), "--output-dir", str(run_dir), "--resume"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == (
        "Cheap model request failed after 5/5 attempt(s): SSLError\n"
        "进度已经写入断点文件。请使用同一条命令加 --resume 继续。"
    )


def test_run_app_upgrades_existing_home_without_touching_app_files(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_path = home / "live-clipper.toml"
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_dir = 'input'",
                "output_root = 'output'",
                "",
                "[recording_source.default]",
                f"source_dir = '{tmp_path / 'offline-nas'}'",
            ]
        ),
        encoding="utf-8",
    )
    env_bytes = b"ASR_API_KEY=keep\\n"
    marker_bytes = b'{"completed": true}\\n'
    fixture_bytes = b"legacy input"
    (home / ".env").write_bytes(env_bytes)
    marker = home / "work" / "service" / "onboarding.json"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(marker_bytes)
    legacy_fixture = home / "input" / "keep.txt"
    legacy_fixture.parent.mkdir()
    legacy_fixture.write_bytes(fixture_bytes)
    monkeypatch.setattr(cli, "run_web_server", lambda **kwargs: None)
    monkeypatch.setattr(cli, "start_embedded_service", lambda *args, **kwargs: {"ok": True})

    cli.run_app()

    assert f'workspace_root = "{home / "workspace"}"' in config_path.read_text(encoding="utf-8")
    assert (home / "workspace" / "runs").is_dir()
    assert (home / ".env").read_bytes() == env_bytes
    assert marker.read_bytes() == marker_bytes
    assert legacy_fixture.read_bytes() == fixture_bytes
