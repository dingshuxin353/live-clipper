# Changelog

## 0.3.2 - 2026-08-04

### Changed

- Migrated the desktop renderer to React 19, TypeScript, and Vite while preserving the existing local APIs and workflows.
- Unified navigation, forms, dialogs, lists, model controls, and status feedback on Astryx Stone with Venus brand tokens and MiSans.
- Added responsive navigation and layout behavior for minimum window sizes and increased zoom.

### Fixed

- Fixed onboarding validation so blocked actions explain the problem and focus the relevant field.
- Fixed inconsistent disabled and busy states, oversized notice banners, narrow-layout clipping, and the ambiguous file-cleanup navigation.
- Localized built-in accessibility labels and restored the MiSans heading theme tokens.
- Prevented onboarding API keys from being serialized into page HTML during React rerenders.

## 0.3.1 - 2026-07-26

### Added

- Added resumable, integrity-checked local Whisper model downloads from ModelScope and Hugging Face.
- Added Small, Medium, and Large local model choices with direct current-model switching.
- Added a four-step first-run flow with local ASR first and cloud ASR as a fallback.

### Changed

- Made Whisper Small the initial first-run local choice without labeling any model as recommended.
- Adopted MiSans as the default interface font.
- Switched formal macOS release preparation to the local asynchronous notarization process.

### Fixed

- Fixed the macOS menu bar template icon so it no longer renders as a solid square.

## 0.3.0 - 2026-07-23

### Added

- Shipped the first signed and notarized formal macOS installer as a `.dmg`.
- Added a GitHub Actions pipeline for automatic release builds and publishing.
- Added in-app automatic updates powered by `electron-updater`.

### Changed

- Simplified the information architecture of the settings page.

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
