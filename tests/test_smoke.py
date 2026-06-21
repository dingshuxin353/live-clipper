from __future__ import annotations

from live_clipper import smoke
from live_clipper.utils import read_json


def test_run_local_smoke_writes_minimal_pipeline_artifacts(tmp_path, monkeypatch):
    output_dir = tmp_path / "smoke"

    def fake_create_video(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        return path

    def fake_render(selection_path):
        clips_dir = selection_path.parent / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output = clips_dir / "smoke-clip.mp4"
        output.write_bytes(b"clip")
        return [output]

    monkeypatch.setattr(smoke, "create_smoke_source_video", fake_create_video)
    monkeypatch.setattr(smoke, "render_selected_clips", fake_render)

    report = smoke.run_local_smoke(output_dir)

    assert report["ok"] is True
    assert report["run_dir"] == str(output_dir)
    assert report["rendered_clips"] == [str(output_dir / "clips" / "smoke-clip.mp4")]
    for name in [
        "run_metadata.json",
        "source_smoke.mp4",
        "transcript_raw.json",
        "transcript.json",
        "windows.json",
        "cheap_candidates.json",
        "merged_candidates.json",
        "codex_brief.json",
        "selected_clips.json",
        "smoke_report.json",
    ]:
        assert (output_dir / name).exists()

    brief = read_json(output_dir / "codex_brief.json")
    assert brief["candidate_count"] == 1
    assert brief["candidates"][0]["id"] == "smoke-clip"
    assert read_json(output_dir / "selected_clips.json")[0]["remove_ranges"] == [[1.2, 1.5]]
    assert read_json(output_dir / "smoke_report.json") == report
