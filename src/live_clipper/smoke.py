"""Local smoke run that exercises the file and render pipeline without remote APIs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .build_codex_brief import build_codex_brief_file
from .models import ClipCandidate, CorrectedTranscript, SelectedClip, TranscriptSentence
from .render_clips import render_selected_clips
from .utils import ensure_dir, write_json
from .windows import write_windows_file


def create_smoke_source_video(output_path: Path) -> Path:
    ensure_dir(output_path.parent)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=30:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:duration=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required on PATH to run smoke") from exc
    return output_path


def run_local_smoke(output_dir: Path = Path("work") / "smoke") -> dict[str, object]:
    run_dir = ensure_dir(output_dir)
    source_video = create_smoke_source_video(run_dir / "source_smoke.mp4")

    write_json(run_dir / "run_metadata.json", {
        "source_video_path": str(source_video),
        "source_name": source_video.name,
        "smoke": True,
    })
    write_json(run_dir / "transcript_raw.json", {
        "segments": [
            {
                "start": 0.0,
                "end": 3.0,
                "text": "这是一段本地烟测视频，用来验证直播切片流程。",
            }
        ]
    })
    transcript = CorrectedTranscript(sentences=[
        TranscriptSentence(start=0.0, end=1.4, text="这是一段本地烟测视频。"),
        TranscriptSentence(start=1.4, end=3.0, text="用来验证直播切片流程。"),
    ])
    write_json(run_dir / "transcript.json", transcript.model_dump())
    write_windows_file(transcript, run_dir / "windows.json")

    candidates = [
        ClipCandidate(
            id="smoke-clip",
            start=0.0,
            end=3.0,
            score=8.0,
            clip_type="smoke_test",
            hook="本地烟测",
            core_value="验证 brief 和 render 链路",
            reason="Synthetic candidate for local pipeline validation.",
        )
    ]
    candidate_data = [candidate.model_dump() for candidate in candidates]
    write_json(run_dir / "cheap_candidates.json", candidate_data)
    write_json(run_dir / "merged_candidates.json", candidate_data)
    build_codex_brief_file(
        run_dir / "merged_candidates.json",
        run_dir / "transcript.json",
        run_dir / "codex_brief.json",
        source_name=source_video.name,
    )

    selection_path = run_dir / "selected_clips.json"
    write_json(selection_path, [
        SelectedClip(
            clip_id="smoke-clip",
            source_start=0.0,
            source_end=3.0,
            title="本地烟测片段",
            remove_ranges=[(1.2, 1.5)],
        ).model_dump()
    ])
    rendered = render_selected_clips(selection_path)
    report = {
        "ok": True,
        "run_dir": str(run_dir),
        "source_video": str(source_video),
        "brief": str(run_dir / "codex_brief.json"),
        "selection": str(selection_path),
        "rendered_clips": [str(path) for path in rendered],
    }
    write_json(run_dir / "smoke_report.json", report)
    return report
