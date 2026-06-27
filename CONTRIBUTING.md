# Contributing

Thanks for helping improve live-clipper.

## Development setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`ffmpeg` must be available on `PATH` for smoke and render tests that exercise media generation.

## Before opening a pull request

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
```

Keep changes focused. Avoid committing local recordings, generated clips, transcripts, logs, `.env`, or virtual environments.

## Configuration and prompt changes

User-facing configuration should go through `src/live_clipper/config.py`. Prompt changes should update the packaged prompts under `src/live_clipper/prompts/`; root prompt copies are only development references until the prompt export flow fully replaces them.
