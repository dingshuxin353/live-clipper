# live-clipper

Local CLI pipeline for turning long livestream recordings into reviewable clip candidates and rendered highlights.

## Handoff Notes

See [docs/session-handoff.md](docs/session-handoff.md) for the current design summary, decisions, and next steps.

## First MVP

The first version keeps the expensive judgment step small:

1. Extract audio from a livestream recording.
2. Transcribe audio with sentence-level timestamps into `transcript_raw.json`.
3. Use the cheap model and a maintained glossary to correct ASR errors into `transcript.json`.
4. Split the corrected transcript into overlapping 3-5 minute windows.
5. Use a cheap model API to scan windows and produce clip candidates.
6. Merge duplicate or overlapping candidates.
7. Build a compact `codex_brief.json` for Codex review.
8. Save final selections in `selected_clips.json`.
9. Render selected clips with `ffmpeg`.

## Project Layout

```text
live-clipper/
  input/
    .gitkeep

  output/
    .gitkeep

  glossary/
    common_terms.example.json

  src/
    live_clipper/
      __init__.py
      __main__.py
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
      .gitkeep
    logs/
      .gitkeep
```

## Commands

```bash
.venv/bin/live-clipper doctor
.venv/bin/live-clipper smoke
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023 --resume
.venv/bin/live-clipper brief output/week_023
.venv/bin/live-clipper render output/week_023/selected_clips.json
```

`doctor` checks local deployment readiness and exits non-zero when required items are missing. `smoke` runs a synthetic local pipeline check without ASR or cheap-model API calls. `scan` runs the batch pipeline up to candidate generation. `scan --resume` reuses existing intermediate files in the output directory after an interrupted run. `brief` builds the Codex review package. `render` renders clips from the reviewed selection file.

## Local Setup

Use Python 3.11+ and install the project in editable mode:

```bash
uv venv --python /Users/gouzi/.local/bin/python3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

`ffmpeg` must be available on `PATH`.

The MVP has defaults for the local ASR backend and Agnes model endpoint. The only required secret for the cheap-model stages is:

```bash
export CHEAP_MODEL_API_KEY="your_agnes_key"
```

Optional Hugging Face authentication can speed up first-time model downloads:

```bash
export HF_TOKEN="your_huggingface_token"
```

Default runtime values:

```text
ASR_BACKEND=mlx_whisper
ASR_MODEL=mlx-community/whisper-large-v3-turbo
CHEAP_MODEL_API_BASE=https://apihub.agnes-ai.com/v1
CHEAP_MODEL_NAME=agnes-2.0-flash
```

Cloud ASR fallback is available when local transcription is not practical:

```bash
export ASR_BACKEND=openai
export ASR_API_BASE=https://api.openai.com/v1
export ASR_API_KEY="your_openai_key"
export ASR_MODEL=whisper-1
```

When `ASR_BACKEND=openai` and `ASR_MODEL` is not set, the CLI defaults to `whisper-1`.

## MVP Run Flow

Check the local environment before a long run:

```bash
.venv/bin/live-clipper doctor
```

The report is JSON so it can be read by humans or automation. Required checks are `ffmpeg`, at least one supported input video, `CHEAP_MODEL_API_KEY`, cheap-model config, and ASR config. Missing `HF_TOKEN` is reported as a warning because downloads can still work unauthenticated.
The glossary check is informational: the CLI prefers `glossary/common_terms.json` and falls back to `glossary/common_terms.example.json` when the editable file has not been created yet.

Run a local smoke test before using a real recording:

```bash
.venv/bin/live-clipper smoke
```

This creates a synthetic video and fixture transcript under `work/smoke/`, builds `codex_brief.json`, writes a minimal `selected_clips.json`, and renders one clip. It does not call ASR or the cheap model API.

Run scan:

```bash
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
```

If a long scan fails after producing some files, resume from the existing output directory:

```bash
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023 --resume
```

`--resume` skips existing `audio.wav`, `transcript_raw.json`, `transcript.json`, `windows.json`, `cheap_candidates.json`, and `merged_candidates.json` as applicable. During cheap-model transcript correction, completed batches are checkpointed to `transcript.partial.json`; during cheap-model window scanning, completed windows are checkpointed to `cheap_candidates.partial.json`. A resumed scan continues from these checkpoints when the final `transcript.json` or `cheap_candidates.json` has not been completed yet. If `transcript.json` and `cheap_candidates.json` already exist, resume can finish local-only stages such as window regeneration or candidate merge without calling the cheap model again.

Expected files:

```text
output/week_023/
  run_metadata.json
  audio.wav
  transcript_raw.json
  transcript.json
  windows.json
  cheap_candidates.json
  merged_candidates.json
```

Build the compact Codex review package:

```bash
.venv/bin/live-clipper brief output/week_023
```

This writes:

```text
output/week_023/
  codex_brief.json
  codex_review.md
  selected_clips.template.json
```

Open `codex_review.md` when handing the candidate package to Codex. It points to `codex_brief.json` and restates the required `selected_clips.json` output contract.
Use `selected_clips.template.json` as a copyable starting point, then save the reviewed result as `selected_clips.json`. Each `clip_id` may be selected at most once, and IDs must stay filename-safe: letters, numbers, dots, underscores, or hyphens only.
Candidate IDs are validated as unique before merge output, brief generation, and final selection validation so older or hand-edited files fail before ambiguous review or rendering.

After Codex or a human creates `selected_clips.json`, render clips:

```json
[
  {
    "clip_id": "w0001-c001",
    "source_start": 12.5,
    "source_end": 58.0,
    "title": "A concise clip title",
    "remove_ranges": [[25.0, 28.0]]
  }
]
```

```bash
.venv/bin/live-clipper render output/week_023/selected_clips.json
```

This writes:

```text
output/week_023/
  edit_decision_list.json
  subtitles/
  clips/
```

`remove_ranges` are applied during rendering by keeping the remaining source segments and concatenating them into the final clip. Kept segments are re-encoded with reset timestamps before concatenation so the final MP4 has stable stream timing. SRT subtitles are written on the final post-removal timeline. `edit_decision_list.json` records whether ranges were applied.
Remove ranges must stay inside the selected clip, must not overlap, and must leave at least one kept segment.

## Transcript Correction

ASR output should be kept as `transcript_raw.json`. The correction stage uses the cheap model plus `glossary/common_terms.json` to produce `transcript.json`.

This stage sends transcript sentences to the cheap model in bounded batches. It should preserve timestamps and sentence boundaries whenever possible. It should only correct likely ASR mistakes, especially recurring terms that are common in our livestreams.

Create an editable glossary before production runs:

```bash
cp glossary/common_terms.example.json glossary/common_terms.json
```

If `common_terms.json` is missing, `scan` uses `common_terms.example.json` so the first run still has baseline terms.

Example glossary entry:

```json
{
  "canonical": "Codex",
  "common_mistakes": ["code x", "扣得克斯", "codec"],
  "notes": "OpenAI coding agent product name"
}
```
