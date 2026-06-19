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

  prompts/
    cheap_correct_transcript.md
    cheap_scan_window.md
    cheap_refine_candidate.md
    cheap_reverse_review.md
    codex_select_clips.md

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
      utils.py

  work/
    cache/
      .gitkeep
    logs/
      .gitkeep
```

## Planned Commands

```bash
python -m live_clipper scan input/week_023_live.mp4
python -m live_clipper brief output/week_023/
python -m live_clipper render output/week_023/selected_clips.json
```

The first command should run the batch pipeline up to candidate generation. The second should build the Codex review package. The third should render clips from the reviewed selection file.

## Transcript Correction

ASR output should be kept as `transcript_raw.json`. The correction stage uses the cheap model plus `glossary/common_terms.json` to produce `transcript.json`.

This stage should preserve timestamps and sentence boundaries whenever possible. It should only correct likely ASR mistakes, especially recurring terms that are common in our livestreams.

Example glossary entry:

```json
{
  "canonical": "Codex",
  "common_mistakes": ["code x", "扣得克斯", "codec"],
  "notes": "OpenAI coding agent product name"
}
```
