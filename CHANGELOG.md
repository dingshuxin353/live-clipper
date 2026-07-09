# Changelog

## 0.2.0 - 2026-07-09

### Fixed

- Runs launched by the local service could get stuck in "processing" forever: pipeline subprocesses became zombies after exit, and the service misread a zombie as "still running." The service now reaps exited children and force-advances any run whose output is already complete but stuck past a configurable timeout (`service.stuck_after_minutes`).
- Manual AI review could hang indefinitely: the `codex exec` invocation ran in an isolated non-git directory (rejected without `--skip-git-repo-check`) with an inherited stdin, so it blocked waiting for input that would never arrive.
- AI review selections from the LLM could fail validation and silently never reach render when the model returned `remove_ranges` as `{"start": ..., "end": ...}` objects instead of `[start, end]` pairs; both forms are now accepted.
- `run_service_once` lost its reconciliation results if the recording source (e.g. an unmounted NAS share) was unavailable. It now persists reconciled state first and degrades a missing source into a `recording_source_unavailable` event instead of crashing.

### Added

- The web console now shows real in-flight feedback for AI review (button disables while running) and surfaces the reason for the last failed review directly on the run card.
- Run cards flag a "stuck" warning when a task has sat in "processing" past the configured timeout.
- AI review is now asynchronous: `POST /api/runs/{id}/ai-review` returns immediately (202) with a background job id instead of blocking on the review for minutes. The web UI polls `GET /api/jobs/{id}` and resumes polling automatically if the page is reloaded mid-review. Concurrent requests for the same run are deduplicated to a single job.

### Chore

- Redacted the maintainer's real NAS path and home directory from README and docs (source code already used a generic default). Untracked `.starwork/` and `_系统/` (internal multi-agent workflow scaffolding, unrelated to the tool itself) and tightened `.gitignore` around local config and runtime state.

## 0.1.0

- Initial public-readiness work for the local livestream clipping pipeline.
- Added unified configuration template support.
- Added prompt export and override support.
- Added safer local web defaults.
- Added privacy controls for model failure logs.
