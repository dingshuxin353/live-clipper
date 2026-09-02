# Changelog

## 1.0.0 - 2026-09-02

### Changed

- On first launch, Venus now checks and backs up existing 0.3.x data before migration. The writable workbench remains closed until the upgrade finishes, and migration does not delete original recordings.
- New installations now use guided setup for speech recognition, the AI service, and the first project.

### Added

- Added project workspaces with independent recording sources, output folders, manual and scheduled scans, processing history, and queue status.
- Added automatic transcription, analysis, structured AI review, subtitle generation, and per-clip rendering.
- Added clip playback with saved AI decisions, titles, descriptions, and other publishing material.
- Added actionable issue recovery and same-recording reprocessing with preserved earlier versions and settings comparison.

## 0.3.6 - 2026-08-20

### Changed

- Task result tabs now show complete phase counts, stable pagination, and queue positions instead of treating the first page as the full task history.
- Scan summaries now distinguish recordings discovered in the current scan, pipelines started in the current pass, and the total queue remaining afterward.

### Fixed

- Phase filters now run against the complete task collection before pagination, so queued, processing, review, rendered, and failed tasks remain visible after more than 20 historical runs.
- Legacy task phases now map consistently to their user-facing tabs, and request failures no longer appear as an empty task history.

## 0.3.5 - 2026-08-17

### Changed

- Scan results now explain unsupported files, skipped directories, per-file failures, recovered historical runs, and queue-start failures instead of reporting a generic success.
- Recording stability checks now snapshot all candidates, wait once, and recheck the batch instead of waiting once per file.
- Automation now reports running, degraded, paused, and stopped health separately, and derives the next recording scan from the real scheduler job.

### Fixed

- A historical run with an empty, invalid, or failed clip selection can no longer abort reconciliation before new recordings are scanned and queued.
- Empty AI or MCP selections remain reviewable and can no longer be rendered, marked as completed, or sent into cleanup without a real rendered clip.
- Daily and weekly scheduler jobs continue recurring after their first run, advance overdue schedules according to the missed-run policy, and do not execute the same due time twice.
- Reconciliation, legacy identity migration, content hashing, and queue startup failures are isolated to the affected run or file so other work continues.
- Both scan buttons share an in-progress state, prevent duplicate submissions, and show the same actionable result.

## 0.3.4 - 2026-08-09

### Changed

- API keys can now be pasted directly in first-run setup and Settings while plaintext remains local and hidden after saving.
- Updated the built-in DeepSeek preset to the current HTTPS endpoint and `deepseek-v4-flash` model.
- Release builds now verify that the bundled backend contains the required MLX runtime while excluding downloaded model weights.

### Fixed

- Restored standard macOS edit actions and added explicit paste controls for LLM and cloud ASR password fields.
- Users who chose “Set up later” can now save or replace the AI API key from Settings without opening hidden folders or editing `.env` manually.
- Saved API keys take effect immediately, remain configured after restart, and are never echoed by the configuration API or settings UI.

## 0.3.3 - 2026-08-06

### Changed

- Scans now consider every stable recording regardless of its date, identify content with a complete streaming SHA-256, persist run identities, and process new recordings through a single-concurrency queue.
- Scan results now distinguish newly discovered, queued, duplicate, too-new, and still-changing recordings.
- Configuration health cards now use Stone semantic colors to distinguish healthy, needs-configuration, and neutral states while retaining visible status text.

### Fixed

- Scanning and retrying without an AI API key now stop before creating a run, copying a recording, or starting the pipeline, with a direct path to the relevant settings.
- Failed runs can now be retried manually and wait in the queue when another recording is processing.
- Renamed or copied recordings with identical content are deduplicated, and concurrent scans serialize run-state mutations.

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
