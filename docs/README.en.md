# live-clipper

English documentation is a secondary entry point for now. The canonical README is the Chinese `README.md`.

live-clipper is a local CLI pipeline for turning long livestream recordings into reviewable clip candidates and rendered highlight clips.

Quick start:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/live-clipper config init
.venv/bin/live-clipper prompts export --output prompts.local
.venv/bin/live-clipper smoke
```

Read the Chinese README for the complete workflow, configuration, prompt customization, and Codex scheduled automation setup.
