# Session Handoff

This document summarizes the project decisions made before moving work to the Mac mini.

## Project Goal

Build a local-first livestream clipping pipeline for weekly 2-hour recordings.

The key design principle is:

```text
cheap model = high-volume batch worker
Codex = editor, reviewer, and decision maker
ffmpeg = execution engine
```

The system should avoid sending full 2-hour transcripts to Codex. Instead, cheap/local tooling should turn the recording into structured candidates, and Codex should only review compact, high-value briefing data.

## Current MVP Pipeline

```text
livestream recording
  -> ffmpeg extracts audio
  -> ASR creates transcript_raw.json
  -> cheap model + glossary corrects ASR errors into transcript.json
  -> transcript is split into overlapping 3-5 minute windows
  -> cheap model scans windows for clip candidates
  -> script merges duplicate/overlapping candidates
  -> script builds codex_brief.json
  -> Codex reviews candidates and writes selected_clips.json
  -> ffmpeg renders final clips and subtitles
```

The ASR correction stage was added because raw ASR often gets names, product terms, English words, and recurring domain phrases wrong.

## Important Output Files

Each recording should get its own output directory, for example:

```text
output/week_023/
  audio.wav
  transcript_raw.json
  transcript.json
  windows.json
  cheap_candidates.json
  merged_candidates.json
  codex_brief.json
  selected_clips.json
  edit_decision_list.json
  subtitles/
  clips/
```

`transcript_raw.json` is the untouched ASR result.

`transcript.json` is the corrected transcript after cheap-model proofreading with glossary support.

`codex_brief.json` is the compact package Codex should review instead of the full transcript.

`edit_decision_list.json` is the final machine-readable editing plan for ffmpeg.

## Current Project Structure

```text
live-clipper/
  README.md
  pyproject.toml
  .env.example

  input/
  output/

  glossary/
    common_terms.example.json

  src/live_clipper/
    cli.py
    config.py
    models.py
    video.py
    transcribe.py
    correct_transcript.py
    windows.py
    cheap_model_client.py
    scan_windows.py
    merge_candidates.py
    build_codex_brief.py
    codex_selection.py
    render_clips.py
    subtitles.py
    smoke.py
    prompt_loader.py
    utils.py
    prompts/
      cheap_correct_transcript.md
      cheap_scan_window.md
      cheap_refine_candidate.md
      cheap_reverse_review.md
      codex_select_clips.md

  work/
    cache/
    logs/
```

## CLI Commands

```bash
.venv/bin/live-clipper doctor
.venv/bin/live-clipper smoke
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023 --resume
.venv/bin/live-clipper brief output/week_023
.venv/bin/live-clipper render output/week_023/selected_clips.json
```

`doctor` checks local deployment readiness. `smoke` runs a synthetic no-API pipeline check. `scan` runs extraction, ASR, transcript correction, windowing, cheap-model candidate scanning, and merge. `scan --resume` skips existing stage outputs; during cheap-model transcript correction it can continue from `transcript.partial.json`, and during cheap-model window scanning it can continue from `cheap_candidates.partial.json`. When `transcript.json` and `cheap_candidates.json` already exist, it can finish local-only stages without calling the cheap model again. `brief` builds the compact Codex review package. `render` validates `selected_clips.json`, writes subtitles and an edit decision list, then renders final clips with ffmpeg.

## Glossary Design

Maintain recurring terms in:

```text
glossary/common_terms.json
```

Example:

```json
{
  "canonical": "Codex",
  "common_mistakes": ["code x", "扣得克斯", "codec"],
  "notes": "OpenAI coding agent product name"
}
```

The correction stage sends sentences to the cheap model in bounded batches. It should preserve timestamps and sentence boundaries. It should only fix likely ASR errors, not rewrite the speaker's style.

## ASR Direction

The current recommendation is local-first ASR with cloud fallback.

For the Mac mini with Apple Silicon and 16GB memory, local ASR is realistic for weekly 2-hour recordings because this is not a real-time product. Even if transcription takes tens of minutes, that is acceptable for batch processing.

Recommended local model order:

1. `whisper-large-v3-turbo`
2. `medium`
3. `small`

Recommended local backends to test:

1. `mlx-whisper` or another MLX-based Whisper runner for Apple Silicon.
2. `whisper.cpp` if a stable CLI path is preferred.

Default config shape:

```text
ASR_BACKEND=mlx_whisper
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

Keep a cloud fallback:

```text
ASR_BACKEND=openai
ASR_API_BASE=https://api.openai.com/v1
ASR_API_KEY=...
ASR_MODEL=whisper-1
```

Useful references from the research:

- OpenAI Whisper model sizes and memory guidance: https://github.com/openai/whisper
- OpenAI transcription pricing: https://developers.openai.com/api/docs/pricing
- MLX Whisper package: https://pypi.org/project/mlx-whisper/
- whisper.cpp Apple Silicon/Core ML support: https://github.com/ggml-org/whisper.cpp

## Current Implementation State

Implemented:

- Python package layout and console script.
- CLI commands: `doctor`, `smoke`, `scan`, `brief`, and `render`.
- ffmpeg audio extraction.
- Local ASR through `mlx_whisper`, defaulting to `mlx-community/whisper-large-v3-turbo`.
- OpenAI-compatible cloud ASR fallback.
- Cheap model client for the Agnes OpenAI-compatible endpoint.
- Batched transcript correction with glossary support, validation failure logs, and checkpoint/resume support.
- Window splitting for corrected transcripts.
- Cheap-model window scanning with per-window checkpoint/resume support, plus candidate merge logic.
- Codex brief generation, including `codex_brief.json`, `codex_review.md`, and `selected_clips.template.json`.
- Candidate ID uniqueness validation at scan, merge, brief, and final selection validation boundaries.
- Selection validation, subtitle generation, edit decision list generation, and ffmpeg clip rendering.
- `remove_ranges` rendering by re-encoding kept segments with reset timestamps before concatenation, with subtitles mapped onto the final post-removal timeline.
- Prompt files packaged under `src/live_clipper/prompts`.
- Local synthetic smoke run that exercises the file and render pipeline without ASR or remote model calls.

Verified:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall src tests
.venv/bin/live-clipper smoke
ffprobe -v error -show_entries format=duration:stream=codec_type,codec_name -of json work/smoke/clips/smoke-clip.mp4
uv build --wheel --out-dir /tmp/live-clipper-wheel-check
.venv/bin/live-clipper doctor
```

Current verification notes:

- The test suite currently passes locally.
- `smoke` renders a valid MP4 with h264 video and aac audio.
- `doctor` is expected to return non-zero until a real input video is placed under `input/` and `CHEAP_MODEL_API_KEY` is set.
- Missing `HF_TOKEN` is a warning for slower first-time Hugging Face downloads, not a hard blocker.

## Suggested Next Step

To run the real pipeline, provide the two remaining external inputs:

```bash
cp glossary/common_terms.example.json glossary/common_terms.json
export CHEAP_MODEL_API_KEY="your_agnes_key"
mkdir -p input
# Put a supported recording such as input/week_023_live.mp4 in input/.
.venv/bin/live-clipper doctor
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
.venv/bin/live-clipper brief output/week_023
```

After Codex or a human writes `output/week_023/selected_clips.json`, run:

```bash
.venv/bin/live-clipper render output/week_023/selected_clips.json
```
