# Contributing

Use Python 3.11, Node 24 and npm 11 for source checks. On macOS, install
`ffmpeg` with Homebrew and make sure both `ffmpeg` and `ffprobe` are on `PATH`.
CI runs the application checks on macOS 14.

## Development setup

From the repository root:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix desktop ci
npm --prefix frontend ci
```

Install desktop dependencies before running Python tests: the blockmap integration
test uses `app-builder-lib`. Ordinary source checks don't need MLX models,
recordings, signing credentials or a user configuration file. Ruff is pinned to
0.16.3; npm uses each directory's lockfile. Python dependencies include version
ranges, so installations can resolve to different versions.

## Before opening a pull request

Run these checks from the repository root:

```bash
.venv/bin/python -m pip freeze
.venv/bin/python -m pip check
.venv/bin/ruff check src tests scripts/release.py
.venv/bin/python -m pytest -ra
npm --prefix desktop test
node --check desktop/main.js
node --check desktop/preload.js
npm --prefix frontend run check
.venv/bin/python -m build
bash scripts/ci/check-generated.sh
```

The frontend check runs type checking, tests, theme generation and the production
build. Commit `frontend/src/theme/venus-stone-overrides.css` and all generated
files under `src/live_clipper/web_static/react/` with any source changes that
alter them. The last command compares those paths with the current commit,
including staged changes and untracked files; run it again after committing.
Don't commit dependency directories, local recordings, clips, transcripts,
logs, `.env` or virtual environments.

If a check fails, read the failed step and fix its cause before rerunning it.
`pytest -ra` lists skipped tests and their reasons. Tests requiring retained
release ZIPs or a real model keep their explicit prerequisites; a skipped test
isn't evidence that the corresponding behavior passed.

CI checks every PR targeting `master`, including drafts, and every push to
`master`. It also provides manual runs once the workflow is on the default
branch; its optional `revision` input accepts a commit, tag or branch. Backend
and desktop checks share one environment; frontend checks run
in parallel. `ci-required` succeeds only when both jobs succeed. New commits
cancel older runs of the same PR; other PRs, push runs and manual runs remain
independent. Download caches speed up installation when available; every run
still performs clean npm installs and all checks.

Include the run link and any local evidence in your PR. The summary records the
source and base commits, tested commits and trees, runtime versions and check
outcomes. Logs contain test counts, skip reasons and resolved Python versions;
GitHub shows job and step timing. Manual checks don't replace PR checks. Source
CI doesn't verify signing, notarization, model behavior or packaged updates;
those checks belong to release acceptance.

## Configuration and prompt changes

User-facing configuration should go through `src/live_clipper/config.py`.
Prompt changes should update the packaged prompts under
`src/live_clipper/prompts/`; root prompt copies are only development references
until the prompt export flow fully replaces them.
