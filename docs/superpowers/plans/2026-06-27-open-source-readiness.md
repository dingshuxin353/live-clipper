# Live Clipper Open Source Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `live-clipper` suitable for public open-source use by removing internal assumptions, unifying configuration, making prompts user-configurable, hardening local security defaults, and documenting the Codex review handoff.

**Architecture:** Keep the current CLI-first pipeline and local web console. Introduce a single configuration layer that feeds CLI, web, automation, model clients, ASR, paths, logging, and prompts. Treat prompt files as user-overridable assets with packaged defaults, not as hidden internal implementation details.

**Tech Stack:** Python 3.11+, setuptools packaging, argparse CLI, pydantic domain models, requests, python-dotenv, pytest, ffmpeg, OpenAI-compatible LLM APIs, optional ASR backends.

---

## Scope

This plan covers P0 and P1 open-source readiness work.

P0 means the repository should be safe and understandable to publish. P1 means the first public users can install, configure, run, and customize the project without reading source code.

This plan does not redesign the clip selection algorithm, add a SaaS backend, or add multi-user authentication. The web console remains a local workstation tool.

## Current Workflow

The current pipeline is:

1. A user places or stages a long recording under `input/`, or the automation flow discovers a recording from a configured source directory.
2. `live-clipper doctor` checks local readiness: ffmpeg, input video, model config, ASR config, and optional Hugging Face token.
3. `live-clipper scan <video>` or `live-clipper pipeline <source>` creates a run directory under `output/<run_id>/`.
4. The pipeline writes `run_metadata.json`.
5. It extracts audio into `audio.wav`.
6. It transcribes audio into `transcript_raw.json`.
7. It optionally corrects ASR output with the configured cheap/OpenAI-compatible model and glossary, producing `transcript.json`.
8. It splits the transcript into overlapping windows in `windows.json`.
9. It sends windows to the configured cheap/OpenAI-compatible model and writes `cheap_candidates.json`.
10. It merges duplicate or overlapping candidates into `merged_candidates.json`.
11. It optionally refines candidates with the configured model into `refined_candidates.json`.
12. It builds `codex_brief.json`, `codex_review.md`, and `selected_clips.template.json`.
13. A human or Codex reviews candidates and writes `selected_clips.json`.
14. `live-clipper render output/<run_id>/selected_clips.json` renders clips, subtitles, and `edit_decision_list.json`.
15. `live-clipper cleanup` previews and optionally removes local intermediate files while preserving original recordings outside the controlled input copy.

## Where Codex Intervenes Today

Codex is intended to intervene after candidate generation and before rendering.

The trigger condition is: `codex_brief.json` exists but `selected_clips.json` does not exist.

Current user-visible signals:

- CLI: `live-clipper brief <run_dir>` writes `codex_review.md` and `selected_clips.template.json`; the README tells the user to open `codex_review.md` and hand the candidate package to Codex.
- Status: `build_run_status()` reports the next step as a Codex selection step when `codex_brief.json` exists and `selected_clips.json` is missing.
- Web console: the run detail contains a `Codex 选择` step. It becomes `waiting` when the brief exists and selection is missing.
- Web index: `requires_codex` becomes true for runs in the `needs_codex_selection` phase.
- Automation: `check_automation_runs()` writes `codex_task.md` into the run directory for `needs_codex_selection`, telling Codex to read `codex_brief.json` and write `selected_clips.json`.

Open-source change needed: document this as a generic "review agent or human selection" step, with Codex as one supported reviewer. The UI and docs should clearly say when the user should open Codex, what files to provide, and what output file Codex must produce.

## P0 Requirements

### P0.1 Repository Publication Safety

**Problem:** The working tree contains local runtime artifacts, and the repository includes internal handoff/spec docs. Git currently tracks only small source/docs/test files, but public release still needs a deliberate publication boundary.

**Required outcome:**

- The public repository does not include real recordings, transcripts, failure payload logs, local virtual environments, editor files, or generated package metadata.
- Internal session handoff docs are either removed, rewritten as public architecture docs, or excluded from the initial public release.
- `.gitignore` explicitly covers common generated artifacts.

**Files:**

- Modify: `.gitignore`
- Modify or replace: `docs/session-handoff.md`
- Review: `docs/superpowers/specs/2026-06-21-live-clipper-web-console-design.md`

**Acceptance checks:**

- `git ls-files` contains no `.env`, `.venv`, `.DS_Store`, media files, rendered clips, logs, caches, or `*.egg-info`.
- `git ls-files -o --exclude-standard` is empty before release.
- Public docs contain no personal home paths or private recording-source paths.

### P0.2 Open Source Metadata

**Problem:** `pyproject.toml` has minimal package metadata and the repo lacks common open-source files.

**Required outcome:**

- The package has license, maintainers, URLs, classifiers, keywords, and supported Python versions.
- The repository has `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, and `CHANGELOG.md`.

**Files:**

- Modify: `pyproject.toml`
- Create: `LICENSE`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `CHANGELOG.md`

**Acceptance checks:**

- `python -m build` can build sdist and wheel.
- `python -m twine check dist/*` passes if release tooling is installed.
- README links to license, contributing guide, security policy, and changelog.

### P0.3 Remove Internal Provider Coupling

**Problem:** Code, docs, prompts, UI, and tests treat Agnes/NAS/Codex as default product concepts instead of configurable providers or optional workflows.

**Required outcome:**

- Public code uses generic names: LLM provider, review agent, recording source, source directory.
- Agnes remains possible as an example OpenAI-compatible provider.
- Codex remains possible as an example review agent.
- NAS remains possible as an example recording source.

**Files:**

- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/cheap_model_client.py`
- Modify: `src/live_clipper/cli.py`
- Modify: `src/live_clipper/automation.py`
- Modify: `src/live_clipper/status.py`
- Modify: `src/live_clipper/web.py`
- Modify: `src/live_clipper/web_static/app.js`
- Modify: `src/live_clipper/web_static/index.html`
- Modify: `README.md`
- Modify tests that assert Agnes/NAS/Codex-specific wording.

**Acceptance checks:**

- Provider-specific terms only appear as intentional examples or compatibility notes.
- Existing tests pass after wording updates.

### P0.4 Secure Local Web Defaults

**Problem:** The local web console currently defaults to `0.0.0.0`, and its POST APIs can render and delete local files inside controlled paths. That is useful on a trusted LAN but not safe as a public default.

**Required outcome:**

- Default web host is `127.0.0.1`.
- LAN access requires explicit `--host 0.0.0.0`.
- Startup output clearly prints local URL and warns when bound to non-loopback host.
- Optional P1/P2 path: add `--token` for browser/API access.

**Files:**

- Modify: `src/live_clipper/cli.py`
- Modify: `src/live_clipper/web.py`
- Modify: `tests/test_cli.py`
- Modify or add: `tests/test_web.py`
- Modify: `README.md`

**Acceptance checks:**

- `live-clipper web` binds to `127.0.0.1`.
- `live-clipper web --host 0.0.0.0` binds to all interfaces and prints a warning.
- Tests assert both default and explicit LAN behavior.

### P0.5 Privacy-Safe Failure Logging

**Problem:** Failure logs may contain transcript text, prompts, and model payloads. Public users need explicit control over sensitive data written to disk.

**Required outcome:**

- Failure logging is documented.
- Users can choose full logs, redacted logs, or disabled payload logs.
- Default should avoid surprising large sensitive dumps.

**Files:**

- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/cheap_model_client.py`
- Modify: `src/live_clipper/utils.py`
- Modify: `.env.example`
- Modify: `README.md`
- Add tests: `tests/test_cheap_model_client.py` or `tests/test_config.py`

**Acceptance checks:**

- With redacted/default mode, failure logs omit or truncate transcript payload content.
- With explicit full mode, failure logs include payloads for debugging.
- README has a privacy section explaining local files and remote model calls.

## P1 Requirements

### P1.1 Unified Friendly Configuration

**Problem:** Configuration is scattered across environment variables, CLI defaults, module constants, README text, and internal defaults.

**Required outcome:**

- One typed configuration model owns all user-facing settings.
- CLI, web, automation, ASR, model client, paths, logging, and prompts read from the same resolved settings.
- A generated config file gives users a friendly starting point.

**Proposed configuration groups:**

- `paths`: input directory, output directory, work directory, cache directory, log directory, state directory, glossary path.
- `recording_source`: source directory, supported extensions, stability age, lookback window.
- `asr`: backend, model, language, API base, API key env name, Hugging Face token env name.
- `llm`: provider label, API base, API key env name, model, timeout, retries, retry delay.
- `prompts`: prompt directory override, prompt profile name.
- `privacy`: failure log mode, max logged payload characters.
- `web`: host, port, optional access token, allowed origins or local-only mode.
- `review`: reviewer label, review package filenames, selection filename.
- `render`: ffmpeg path, subtitle behavior, output format.

**Files:**

- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/cli.py`
- Modify: `src/live_clipper/automation.py`
- Modify: `src/live_clipper/web.py`
- Modify: `src/live_clipper/cheap_model_client.py`
- Modify: `src/live_clipper/transcribe.py`
- Modify: `.env.example`
- Create: `docs/configuration.md`
- Add tests: `tests/test_config.py`, `tests/test_cli.py`

**Acceptance checks:**

- `live-clipper config init` writes a documented config template.
- Environment variables still work for secrets.
- CLI flags override config file values.
- Tests cover default resolution, config file resolution, env secret resolution, and CLI override resolution.

### P1.2 User-Configurable Prompts

**Problem:** Prompt files are packaged internals. There is also a duplicate root `prompts/` directory, which creates ambiguity about which files are active.

**Required outcome:**

- Packaged prompts remain defaults.
- Users can export prompts into a local directory, edit them, and run with `--prompt-dir` or config `prompts.directory`.
- The active prompt source is visible in metadata.
- Duplicate prompt directories are removed or clearly separated into packaged defaults and user overrides.

**Files:**

- Modify: `src/live_clipper/prompt_loader.py`
- Modify: `src/live_clipper/cli.py`
- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/build_codex_brief.py`
- Modify: `src/live_clipper/correct_transcript.py`
- Modify: `src/live_clipper/scan_windows.py`
- Modify: `src/live_clipper/refine_candidates.py`
- Create or modify tests: `tests/test_prompt_loader.py`, `tests/test_cli.py`
- Create: `docs/prompts.md`

**Acceptance checks:**

- `live-clipper prompts export --output prompts.local` writes editable prompt files.
- `live-clipper scan ... --prompt-dir prompts.local` uses user prompts.
- `run_metadata.json` records active prompt source and prompt file names.
- Tests prove user prompt override takes precedence over packaged prompts.

### P1.3 Public Documentation Rewrite

**Problem:** README currently describes an internal MVP and includes personal installation paths and internal service language.

**Required outcome:**

- README starts with a public-friendly explanation: what the tool does, who it is for, what leaves the machine, and quickstart.
- Separate docs cover configuration, prompts, workflow, web console, automation, privacy, and troubleshooting.

**Files:**

- Rewrite: `README.md`
- Create: `docs/workflow.md`
- Create: `docs/configuration.md`
- Create: `docs/prompts.md`
- Create: `docs/privacy.md`
- Create: `docs/web-console.md`
- Create: `docs/troubleshooting.md`

**Acceptance checks:**

- A new user can follow README from clone to smoke test without local/private paths.
- README explains `doctor`, `smoke`, `scan`, review handoff, `render`, and `cleanup`.
- Docs clearly state that remote LLM/ASR providers may receive transcript/audio data depending on configuration.

### P1.4 Cross-Platform ASR and Language Settings

**Problem:** The default ASR path is Apple/MLX-oriented and language is hardcoded to Chinese.

**Required outcome:**

- ASR backend, model, and language are configured centrally.
- Apple MLX remains a supported backend.
- OpenAI-compatible ASR remains a supported backend.
- Documentation explains platform support and installation tradeoffs.

**Files:**

- Modify: `src/live_clipper/transcribe.py`
- Modify: `src/live_clipper/config.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `README.md`
- Add tests: `tests/test_transcribe.py`, `tests/test_config.py`

**Acceptance checks:**

- `ASR_LANGUAGE=auto` or config equivalent is accepted.
- `ASR_LANGUAGE=zh` preserves current behavior.
- Users can install optional ASR extras instead of always installing every backend dependency.

### P1.5 CI, Linting, and Release Workflow

**Problem:** Tests exist, but the project has no public CI, linting, coverage, or release workflow.

**Required outcome:**

- GitHub Actions runs tests on pull requests.
- Formatting and linting are deterministic.
- Build artifacts are checked before release.

**Files:**

- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml` or document manual release.
- Optional create: `.pre-commit-config.yaml`

**Acceptance checks:**

- CI runs `python -m pytest`.
- CI runs formatting/lint checks.
- CI builds the package.
- Release process is documented and repeatable.

## Suggested Execution Order

1. P0.1 repository publication safety.
2. P0.2 open-source metadata.
3. P0.4 secure web defaults.
4. P0.3 provider wording cleanup.
5. P0.5 privacy-safe logs.
6. P1.1 unified configuration.
7. P1.2 configurable prompts.
8. P1.3 public docs rewrite.
9. P1.4 cross-platform ASR and language settings.
10. P1.5 CI and release workflow.

## Verification Matrix

Run these before considering P0/P1 complete:

```bash
python -m pytest
python -m build
python -m twine check dist/*
rg -n "Agnes|NAS|Codex|/Volumes|/Users/|recording-source-placeholder" README.md src tests docs
git ls-files -o --exclude-standard
```

Expected:

- Tests pass.
- Package builds.
- Twine check passes.
- Search results only contain intentional example references.
- No untracked release-unsafe files remain.
