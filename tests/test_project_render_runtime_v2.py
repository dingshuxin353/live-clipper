from __future__ import annotations

import hashlib
import shutil
import subprocess

import pytest
from test_project_auto_review_v2 import _candidate, _project_run, _selected

from live_clipper.media_probe import MediaMetadata, probe_media
from live_clipper.project_result_runtime import render_project_outputs, safe_output_name


def _review_two(repository, run, run_dir):
    from live_clipper.project_result_runtime import run_project_review

    return run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "two",
            "warnings": [],
            "decisions": [
                _selected("one"),
                {**_selected("two", start=30, end=40), "rank": 2},
            ],
        },
    )


def test_safe_output_name_removes_controls_and_path_separators():
    assert safe_output_name("  bad/name\\\x00  ") == "badname"
    assert safe_output_name("   ") == "recording"
    assert len(safe_output_name("片" * 100)) == 60


def test_render_keeps_first_success_when_second_output_fails(tmp_path):
    repository, _project, run, run_dir, output_dir = _project_run(
        tmp_path,
        candidates=[_candidate("one"), _candidate("two", start=30, end=40)],
    )
    _review_two(repository, run, run_dir)
    calls = []

    def renderer(_source, _transcript, clip, _work_dir, partial):
        calls.append(clip.clip_id)
        if clip.clip_id == "two":
            raise RuntimeError("synthetic render failure")
        partial.write_bytes(b"ready-one")

    report = render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=renderer,
        probe=lambda path: MediaMetadata(1000, 1280, 720, "mov,mp4", "h264", path.stat().st_size),
    )

    assert calls == ["one", "two"]
    assert len(report.ready_output_ids) == 1 and len(report.failed_output_ids) == 1
    assert repository.get_run_result(run.run_id).result_type == "partial"
    assert repository.get_run(run.run_id).status == "completed"
    ready = repository.get_run_output(report.ready_output_ids[0])
    ready_path = output_dir / ready.file_name
    assert ready_path.read_bytes() == b"ready-one"
    assert not list(output_dir.glob(".venus-*.partial.mp4"))


def test_render_retry_never_overwrites_an_existing_ready_file(tmp_path):
    repository, _project, run, run_dir, output_dir = _project_run(tmp_path, candidates=[_candidate("one")])
    from live_clipper.project_result_runtime import run_project_review

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
    )
    render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda _source, _transcript, _clip, _work, partial: partial.write_bytes(b"stable"),
        probe=lambda path: MediaMetadata(1000, 1280, 720, "mp4", "h264", path.stat().st_size),
    )
    output = repository.list_run_outputs(run.run_id)[0]
    final_path = output_dir / output.file_name
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()

    second = render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda *_args: (_ for _ in ()).throw(AssertionError("ready output rendered again")),
        probe=lambda path: MediaMetadata(1000, 1280, 720, "mp4", "h264", path.stat().st_size),
    )

    assert second.reused_output_ids == (output.output_id,)
    assert hashlib.sha256(final_path.read_bytes()).hexdigest() == digest


def test_ready_output_with_matching_media_facts_but_changed_hash_is_not_reused(tmp_path):
    repository, _project, run, run_dir, output_dir = _project_run(tmp_path, candidates=[_candidate("one")])
    from live_clipper.project_result_runtime import run_project_review

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
    )
    def fake_probe(path):
        return MediaMetadata(1000, 1280, 720, "mp4", "h264", path.stat().st_size)

    render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda _source, _transcript, _clip, _work, partial: partial.write_bytes(b"stable"),
        probe=fake_probe,
    )
    output = repository.list_run_outputs(run.run_id)[0]
    final_path = output_dir / output.file_name
    final_path.write_bytes(b"mutate")

    report = render_project_outputs(repository, run.run_id, run_dir=run_dir, probe=fake_probe)

    assert report.failed_output_ids == (output.output_id,)
    assert not report.reused_output_ids
    assert repository.get_run_output(output.output_id).status == "unreadable"
    assert repository.list_issues(run_id=run.run_id, active_only=True)[0].issue_code == "output_unreadable"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real media integrity regression requires ffmpeg and ffprobe",
)
def test_tampered_ready_output_is_blocked_without_overwriting_other_ready_output(tmp_path):
    repository, _project, run, run_dir, output_dir = _project_run(
        tmp_path,
        candidates=[_candidate("one", start=0, end=1), _candidate("two", start=1, end=2)],
    )
    source = tmp_path / "source" / "recording.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=15:d=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    from live_clipper.project_result_runtime import run_project_review

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "two",
            "warnings": [],
            "decisions": [
                _selected("one", start=0, end=1),
                {**_selected("two", start=1, end=2), "rank": 2},
            ],
        },
    )
    first = render_project_outputs(repository, run.run_id, run_dir=run_dir)
    assert len(first.ready_output_ids) == 2
    outputs = repository.list_run_outputs(run.run_id)
    tampered_path = output_dir / outputs[0].file_name
    preserved_path = output_dir / outputs[1].file_name
    preserved_sha256 = hashlib.sha256(preserved_path.read_bytes()).hexdigest()
    tampered_path.write_bytes(b"not a playable mp4")

    second = render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda *_args: (_ for _ in ()).throw(AssertionError("ready output rendered again")),
    )

    assert second.failed_output_ids == (outputs[0].output_id,)
    assert second.reused_output_ids == (outputs[1].output_id,)
    assert repository.get_run_output(outputs[0].output_id).status == "unreadable"
    assert repository.get_run_output(outputs[0].output_id).error_code == "output_unreadable"
    issues = repository.list_issues(run_id=run.run_id, active_only=True)
    assert [(issue.issue_code, issue.output_id) for issue in issues] == [
        ("output_unreadable", outputs[0].output_id)
    ]
    assert hashlib.sha256(preserved_path.read_bytes()).hexdigest() == preserved_sha256


def test_probe_media_parses_ffprobe_json_without_exposing_command_output(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"1234")

    def runner(command, **kwargs):
        assert command[0] == "ffprobe"
        assert kwargs["capture_output"] and kwargs["text"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"format":{"duration":"1.25","format_name":"mov,mp4"},"streams":[{"codec_type":"video","codec_name":"h264","width":1920,"height":1080}]}',
            stderr="",
        )

    metadata = probe_media(target, runner=runner)

    assert metadata == MediaMetadata(1250, 1920, 1080, "mov,mp4", "h264", 4)


def test_restart_registers_verified_partial_without_rendering_again(tmp_path):
    repository, _project, run, run_dir, output_dir = _project_run(tmp_path, candidates=[_candidate("one")])
    from live_clipper.project_result_runtime import run_project_review

    run_project_review(
        repository,
        run.run_id,
        run_dir=run_dir,
        adapter=lambda _payload: {
            "format_version": 1,
            "overall_summary": "one",
            "warnings": [],
            "decisions": [_selected("one")],
        },
    )
    output = repository.list_run_outputs(run.run_id)[0]
    repository.update_output_and_reproject_result(output.output_id, status="rendering")
    partial = output_dir / f".venus-{output.output_id}.partial.mp4"
    partial.write_bytes(b"verified-partial")

    report = render_project_outputs(
        repository,
        run.run_id,
        run_dir=run_dir,
        renderer=lambda *_args: (_ for _ in ()).throw(AssertionError("rendered twice")),
        probe=lambda path: MediaMetadata(1000, 1280, 720, "mp4", "h264", path.stat().st_size),
    )

    assert report.reused_output_ids == (output.output_id,)
    assert repository.get_run_output(output.output_id).status == "ready"
    assert (output_dir / output.file_name).read_bytes() == b"verified-partial"
    assert not partial.exists()
