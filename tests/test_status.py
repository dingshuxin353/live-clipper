from __future__ import annotations

from live_clipper.status import build_run_status
from live_clipper.utils import read_json, write_json


def test_build_run_status_reports_next_step_and_writes_report(tmp_path):
    run_dir = tmp_path / "run"
    write_json(run_dir / "run_metadata.json", {"source_name": "source.mp4"})
    write_json(run_dir / "transcript_raw.json", {"segments": [{"start": 0, "end": 1, "text": "raw"}]})
    write_json(run_dir / "transcript.json", {"sentences": [{"start": 0, "end": 1, "text": "正文"}]})
    write_json(run_dir / "windows.json", [])
    write_json(run_dir / "cheap_candidates.json", [])
    write_json(run_dir / "merged_candidates.json", [])

    report = build_run_status(run_dir)

    assert report["files"]["transcript.json"]["count"] == 1
    assert report["files"]["merged_candidates.json"]["count"] == 0
    assert report["next_step"] == "运行 refine，让 Agnes 二次复评候选"
    assert read_json(run_dir / "run_report.json")["next_step"] == report["next_step"]


def test_build_run_status_does_not_create_missing_run_dir(tmp_path):
    run_dir = tmp_path / "missing"

    report = build_run_status(run_dir)

    assert report["exists"] is False
    assert report["next_step"] == "运行 scan 创建新的 run 目录"
    assert not run_dir.exists()
