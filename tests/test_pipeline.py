from __future__ import annotations

from live_clipper.pipeline import cleanup_local_artifacts, cleanup_plan, record_pipeline_metadata, stage_source_file
from live_clipper.utils import read_json, write_json


def test_stage_source_file_copies_source_into_input_dir(tmp_path):
    source = tmp_path / "nas" / "recording.mkv"
    input_dir = tmp_path / "input"
    source.parent.mkdir()
    source.write_bytes(b"video-data")

    staged = stage_source_file(source, input_dir=input_dir)

    assert staged == input_dir / "recording.mkv"
    assert staged.read_bytes() == b"video-data"
    assert not (input_dir / "recording.mkv.part").exists()


def test_stage_source_file_resumes_part_file(tmp_path):
    source = tmp_path / "nas" / "recording.mkv"
    input_dir = tmp_path / "input"
    source.parent.mkdir()
    source.write_bytes(b"video-data")
    input_dir.mkdir()
    (input_dir / "recording.mkv.part").write_bytes(b"video")

    staged = stage_source_file(source, input_dir=input_dir)

    assert staged.read_bytes() == b"video-data"


def test_cleanup_local_artifacts_deletes_only_local_copy_and_audio_after_render(tmp_path):
    run_dir = tmp_path / "output" / "recording"
    input_dir = tmp_path / "input"
    nas_source = tmp_path / "Volumes" / "homes" / "recording.mkv"
    local_source = input_dir / "recording.mkv"
    audio = run_dir / "audio.wav"
    clip = run_dir / "clips" / "clip-1.mp4"
    nas_source.parent.mkdir(parents=True)
    input_dir.mkdir()
    clip.parent.mkdir(parents=True)
    nas_source.write_bytes(b"nas-video")
    local_source.write_bytes(b"local-video")
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    clip.write_bytes(b"mp4")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(local_source),
        "source_name": "recording.mkv",
    })
    write_json(run_dir / "selected_clips.json", [])
    record_pipeline_metadata(run_dir, nas_source, local_source)

    plan = cleanup_plan(run_dir, input_dir=input_dir)
    assert {item["kind"] for item in plan if item["deletable"]} == {"audio", "local_source_video"}

    preview = cleanup_local_artifacts(run_dir, input_dir=input_dir, confirm=False)
    assert local_source.exists()
    assert audio.exists()
    assert preview["deleted"] == []

    report = cleanup_local_artifacts(run_dir, input_dir=input_dir, confirm=True)

    assert not local_source.exists()
    assert not audio.exists()
    assert nas_source.exists()
    assert set(report["deleted"]) == {str(local_source), str(audio)}
    metadata = read_json(run_dir / "run_metadata.json")
    assert metadata["pipeline"]["cleanup_confirmed"] is True


def test_cleanup_local_artifacts_refuses_before_render_without_force(tmp_path):
    run_dir = tmp_path / "output" / "recording"
    input_dir = tmp_path / "input"
    local_source = input_dir / "recording.mkv"
    input_dir.mkdir()
    local_source.write_bytes(b"local-video")
    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(local_source),
        "source_name": "recording.mkv",
        "pipeline": {"local_source_path": str(local_source)},
    })

    try:
        cleanup_local_artifacts(run_dir, input_dir=input_dir, confirm=True)
    except RuntimeError as exc:
        assert "尚未检测到已渲染成片" in str(exc)
    else:
        raise AssertionError("cleanup should require rendered clips unless force is set")

    assert local_source.exists()
