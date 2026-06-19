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

  prompts/
    cheap_correct_transcript.md
    cheap_scan_window.md
    cheap_refine_candidate.md
    cheap_reverse_review.md
    codex_select_clips.md

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
    utils.py

  work/
    cache/
    logs/
```

## Planned CLI

```bash
python -m live_clipper scan input/week_023_live.mp4
python -m live_clipper brief output/week_023/
python -m live_clipper render output/week_023/selected_clips.json
```

The CLI exists as a placeholder. The commands are not implemented yet.

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

The correction stage should preserve timestamps and sentence boundaries. It should only fix likely ASR errors, not rewrite the speaker's style.

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

Suggested config shape:

```text
ASR_BACKEND=mlx_whisper
ASR_MODEL=large-v3-turbo
```

Keep a cloud fallback:

```text
ASR_BACKEND=openai
```

Useful references from the research:

- OpenAI Whisper model sizes and memory guidance: https://github.com/openai/whisper
- OpenAI transcription pricing: https://developers.openai.com/api/docs/pricing
- MLX Whisper package: https://pypi.org/project/mlx-whisper/
- whisper.cpp Apple Silicon/Core ML support: https://github.com/ggml-org/whisper.cpp

## Current Implementation State

Implemented:

- Project skeleton.
- Python package layout under `src/live_clipper`.
- Placeholder CLI with `scan`, `brief`, and `render` commands.
- Prompt files for cheap-model correction, scanning, refinement, reverse review, and Codex selection.
- Glossary example file.
- Basic Pydantic models:
  - `TranscriptSentence`
  - `TranscriptWindow`
  - `GlossaryTerm`
  - `TranscriptCorrection`
  - `CorrectedTranscript`
  - `ClipCandidate`
  - `SelectedClip`

Verified:

```bash
python3 -m compileall -q src
```

Not implemented yet:

- ffmpeg audio extraction.
- Local ASR integration.
- Transcript correction implementation.
- Window splitting.
- Cheap model client.
- Candidate merge logic.
- Codex brief generation.
- ffmpeg clip rendering.

## Suggested Next Step

Start by implementing the transcript path:

```text
video.py
  extract audio

transcribe.py
  run local ASR and produce transcript_raw.json

correct_transcript.py
  load glossary
  call cheap model
  produce transcript.json

windows.py
  split corrected transcript into windows.json
```

This gives the rest of the project a stable input foundation.

