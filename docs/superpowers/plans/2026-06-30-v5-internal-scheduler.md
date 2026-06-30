# V5 Internal Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a built-in scheduler that runs with `live-clipper service`, exposes jobs in the Web `配置` page, and supports scan/review/maintenance jobs without implementing V6 AI auto-review.

**Architecture:** Add `src/live_clipper/scheduler.py` as the scheduler core for config validation, next-run calculation, state files, events, and job actions. Extend `config.py`/`config_editor.py` with whitelist scheduler config. Wire `service.py` through a short tick path and `web.py` through thin scheduler APIs; update the existing V4 static config page with a `定时任务` section.

**Tech Stack:** Python 3.11 standard library (`zoneinfo`, `datetime`, JSON state files), existing HTTP server, vanilla JS/CSS, pytest, ruff.

---

### Task 1: Scheduler Config

**Files:**
- Modify: `src/live_clipper/config.py`
- Modify: `src/live_clipper/config_editor.py`
- Test: `tests/test_config.py`
- Test: `tests/test_config_editor.py`

- [ ] **Step 1: Write failing tests**

Add tests for default `[scheduler]` values, TOML `[[scheduler.jobs]]` parsing, editable config inclusion, and validation failures for invalid timezone/missed policy/job fields.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_config_editor.py -q`

Expected: fail because `settings.scheduler` and editable scheduler fields do not exist.

- [ ] **Step 3: Implement config dataclasses and editor whitelist**

Add `SchedulerConfig` and `SchedulerJobConfig`, defaults for weekly scan and weekly review check, TOML load, config template fields, editor clean/validate/merge/diff handling for `scheduler` and `scheduler_jobs`.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_config_editor.py -q`

Expected: pass.

### Task 2: Scheduler Core

**Files:**
- Create: `src/live_clipper/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

Cover weekly/daily/interval next-run calculation, `missed_policy=run_once`, skip-if-running, job validation, scan job calling existing service action, review_due_check marking `needs_review`, and no V6 selected-clips generation.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -q`

Expected: fail because `live_clipper.scheduler` does not exist.

- [ ] **Step 3: Implement scheduler core**

Implement state paths, event appenders, `get_scheduler_status()`, `run_job_now()`, `pause_job()`, `resume_job()`, `upsert_job()`, `tick_scheduler()`, and job actions `scan_recordings`, `review_due_check`, `maintenance_check`.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -q`

Expected: pass.

### Task 3: Service and Web API

**Files:**
- Modify: `src/live_clipper/service.py`
- Modify: `src/live_clipper/web.py`
- Test: `tests/test_service.py`
- Test: `tests/test_web_scheduler.py`

- [ ] **Step 1: Write failing tests**

Add Web API tests for `GET /api/scheduler`, job upsert, run-now, pause/resume, invalid job Chinese errors. Add service tick tests that scheduler tick writes state and does not run V6 behavior.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_web_scheduler.py tests/test_service.py -q`

Expected: fail because routes/service tick integration do not exist.

- [ ] **Step 3: Implement integration**

Add `/api/scheduler`, `/api/scheduler/jobs`, `/api/scheduler/jobs/<id>/run-now`, `/pause`, `/resume`, `/api/scheduler/events`. Add `run_service_tick()` and make `service_loop()` sleep on scheduler tick seconds while preserving `start --once`.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest tests/test_web_scheduler.py tests/test_service.py tests/test_scheduler.py -q`

Expected: pass.

### Task 4: Web UI and Docs

**Files:**
- Modify: `src/live_clipper/web_static/index.html`
- Modify: `src/live_clipper/web_static/app.js`
- Modify: `src/live_clipper/web_static/styles.css`
- Modify: `README.md`
- Test: `tests/test_docs.py`

- [ ] **Step 1: Write failing static/docs tests**

Assert the Web config page exposes `定时任务`, `每周录播扫描`, `每周审阅检查`, `立即执行`, `暂停`, `启用`, and scheduler API calls; README documents built-in scheduler and states no Codex/cron dependency.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_docs.py -q`

Expected: fail because the UI/docs still have no scheduler section.

- [ ] **Step 3: Implement UI/docs**

Add `定时任务` fieldset, scheduler summary, job cards, basic edit form controls, run-now/pause/resume buttons, and README guidance. Keep all V6 AI review copy as “后续版本” only.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest tests/test_docs.py -q`

Expected: pass.

### Task 5: Verification and Handoff

**Files:**
- Modify lane worklog only after implementation.

- [ ] **Step 1: Run full verification**

Run:
- `.venv/bin/python -m pytest -q`
- `uv run --with ruff ruff check src/live_clipper/scheduler.py src/live_clipper/service.py src/live_clipper/config.py src/live_clipper/config_editor.py src/live_clipper/web.py src/live_clipper/web_static tests/test_scheduler.py tests/test_web_scheduler.py tests/test_config.py tests/test_config_editor.py tests/test_docs.py`
- `node --check src/live_clipper/web_static/app.js`

- [ ] **Step 2: HTTP smoke**

Start Web locally and check:
- `GET /` contains `定时任务`
- `GET /api/scheduler` returns `ok: true`
- `POST /api/scheduler/jobs/weekly_review_due/run-now` returns a review due result without creating `selected_clips.json`

- [ ] **Step 3: Commit and acceptance request**

Commit on `codex/v5-internal-scheduler`, update development-manager worklog, send acceptance request to product-manager, and do not merge/push.

---

## Coverage Check

- V5 default weekly jobs are covered by Tasks 1 and 2.
- Scheduler service integration is covered by Task 3.
- Web config page placement, job viewing, creating/editing, run-now, pause/resume are covered by Tasks 3 and 4.
- V6 AI auto-review is explicitly excluded; no Codex CLI, Claude Code, model call, or `selected_clips.json` generation is implemented.
