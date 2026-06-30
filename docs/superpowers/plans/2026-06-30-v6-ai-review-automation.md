# V6 AI Review Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add V6 AI 自动审阅 so `needs_review` runs can generate a validated `selected_clips.json` through a configured local Agent or OpenAI-compatible model.

**Architecture:** Keep V6 as a thin executor around existing Service Core state, review packages, `validate_selected_clips_file()`, and automatic render detection. `review_automation.py` owns payload construction, adapters, JSON extraction, temp-file validation, final replacement, summary state, and events. Web and Scheduler only call this module.

**Tech Stack:** Python stdlib, existing `Settings`/TOML config, `CheapModelClient`, existing Web static JS/CSS, pytest, ruff.

---

### Task 1: Review Automation Config

**Files:**
- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/config_editor.py`
- Test: `tests/test_config.py`
- Test: `tests/test_config_editor.py`

- [ ] Add failing config tests for default `review_automation`, TOML loading, editable whitelist, Chinese validation errors, and no secret exposure.
- [ ] Run `tests/test_config.py tests/test_config_editor.py` and confirm failures mention missing `review_automation`.
- [ ] Add dataclasses for `ReviewAutomationConfig`, `ReviewAutomationLocalAgentConfig`, and `ReviewAutomationModelConfig`.
- [ ] Extend `DEFAULT_CONFIG_TEMPLATE`, `Settings`, and `load_settings()`.
- [ ] Extend config editor whitelist, numeric/boolean enum validation, env status, save/load merge, and service restart detection.
- [ ] Run config tests and confirm green.

### Task 2: Review Automation Core

**Files:**
- Create: `src/live_clipper/review_automation.py`
- Test: `tests/test_review_automation.py`

- [ ] Add failing tests for payload construction, JSON array extraction, validation failure cleanup, success final write, max-runs-per-tick, local Agent fake runner, model fake client, and safety non-actions.
- [ ] Run `tests/test_review_automation.py` and confirm failures are due to missing module/functions.
- [ ] Implement state/event files: `review_automation.json` and `review_automation_events.jsonl`, with mirrored Service Core events.
- [ ] Implement `build_review_payload()`, `extract_selection_json()`, temp write, `validate_selected_clips_file()`, atomic replace, and structured success/failure payloads.
- [ ] Implement environment checks for `codex` and `claude` without revealing secrets.
- [ ] Implement local Agent adapter using injectable runner and default non-interactive commands.
- [ ] Implement model adapter using injectable client factory and `CheapModelClient`.
- [ ] Implement `run_ai_review_for_run()` and `run_due_ai_reviews()`.
- [ ] Run review automation tests and confirm green.

### Task 3: Scheduler Integration

**Files:**
- Modify: `src/live_clipper/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] Add failing scheduler tests that `ai_review` is accepted, `run_job_now()` delegates to review automation, and existing `review_due_check` remains unchanged.
- [ ] Run scheduler tests and confirm `ai_review` rejection fails as expected.
- [ ] Extend job type validation and display labels to include `ai_review`.
- [ ] Add `_run_job_action()` branch calling `review_automation.run_due_ai_reviews()`.
- [ ] Keep destructive actions impossible from Scheduler.
- [ ] Run scheduler tests and confirm green.

### Task 4: Web API

**Files:**
- Modify: `src/live_clipper/web.py`
- Test: `tests/test_web_review_automation.py`

- [ ] Add failing Web tests for `GET /api/review-automation`, `POST /api/review-automation/check`, `POST /api/runs/<run_id>/ai-review`, `POST /api/review-automation/run-due`, invalid phase Chinese error, existing selection skip, and no render on validation failure.
- [ ] Run Web review tests and confirm missing routes.
- [ ] Add routes that delegate to `review_automation`.
- [ ] Add `can_ai_review` action to Service Core run detail.
- [ ] Return structured Chinese errors for missing run, invalid phase, selected file already exists, disabled automation, environment/model errors, validation failure.
- [ ] Run Web review tests and confirm green.

### Task 5: Web UI And Docs

**Files:**
- Modify: `src/live_clipper/web_static/index.html`
- Modify: `src/live_clipper/web_static/app.js`
- Modify: `src/live_clipper/web_static/styles.css`
- Modify: `README.md`
- Test: `tests/test_docs.py`

- [ ] Add failing docs/static tests for AI 审阅 config section, environment check button, run detail button, `ai_review` scheduler action, and README safety wording.
- [ ] Run docs tests and confirm failures.
- [ ] Add config page `AI 审阅` section in Chinese, near 定时任务.
- [ ] Add task detail `立即 AI 审阅` button only when run is `needs_review`.
- [ ] Add frontend calls for review automation status/check/run due/run run-id.
- [ ] Add scheduler job type option `ai_review` with label `AI 自动审阅`.
- [ ] Update README with local Agent, model mode, selected-clips validation, and safety boundaries.
- [ ] Run docs tests and `node --check`.

### Task 6: Verification And Handoff

**Files:**
- Modify: `_系统/协作/lanes/development-manager/worklog.md` after code commit only.

- [ ] Run full verification: `.venv/bin/python -m pytest -q`.
- [ ] Run touched-file ruff.
- [ ] Run `node --check src/live_clipper/web_static/app.js`.
- [ ] Run Web/API smoke for `/`, `/api/review-automation`, `/api/review-automation/check`, and an invalid `ai-review` request.
- [ ] Commit implementation on `codex/v6-ai-review-automation`.
- [ ] Restore StarWork collaboration state, update worklog, send acceptance request to product-manager, and record the request.

### Safety Checklist

- [ ] AI output is parsed as JSON and system writes files.
- [ ] `selected_clips.json` appears only after `validate_selected_clips_file()` succeeds.
- [ ] Validation failure deletes temp file and does not render.
- [ ] AI cannot delete files, run cleanup confirm, approve/reject confirmations, or move NAS originals.
- [ ] Logs and status do not include API keys or authorization headers.
- [ ] Default config keeps auto AI review disabled.
