# V4 Web Config Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the Web settings page into a Chinese, whitelist-based config editor that can validate, back up, atomically save, and service-restart the local `live-clipper.toml`.

**Architecture:** Add `src/live_clipper/config_editor.py` as the only structured config editing layer. `src/live_clipper/web.py` remains a thin adapter that exposes `/api/config*`; the static Web app renders the form and posts drafts. Existing `load_settings()` remains the final validation source after writes.

**Tech Stack:** Python 3.11 standard library (`tomllib`, atomic `Path.replace`), existing HTTP server, vanilla JS/CSS, pytest, ruff.

---

### Task 1: Config Editor Core

**Files:**
- Create: `src/live_clipper/config_editor.py`
- Test: `tests/test_config_editor.py`

- [ ] **Step 1: Write failing tests**

Cover `load_editable_config()`, `validate_editable_config()`, `save_editable_config()`, env status redaction, path/number/enum validation, backup creation, and `load_settings()` verification after save.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_config_editor.py -q`

Expected: fail because `live_clipper.config_editor` does not exist.

- [ ] **Step 3: Implement minimal editor**

Implement:
- `CONFIG_SECTIONS` whitelist for `paths`, `recording_source_default`, `llm`, `asr`, `service`, `web`.
- `load_editable_config(config_path=Path("live-clipper.toml"))`.
- `validate_editable_config(draft, config_path=..., base_dir=Path.cwd())`.
- `save_editable_config(draft, config_path=..., backup_root=Path("work/config_backups"))`.
- a small TOML writer for dict/list/scalar values; comments are not preserved, unknown dict fields are preserved.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_config_editor.py -q`

Expected: pass.

### Task 2: Web API

**Files:**
- Modify: `src/live_clipper/web.py`
- Test: `tests/test_web_v4_config.py`

- [ ] **Step 1: Write failing API tests**

Cover:
- `GET /api/config` returns whitelist config and env status without secret values.
- `POST /api/config/validate` rejects invalid drafts with Chinese errors.
- `POST /api/config` creates backup and writes loadable TOML.
- parse failure refuses overwrite.
- `POST /api/config/restart-service` returns `restarted: false` when stopped and stop/start result when running.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_web_v4_config.py -q`

Expected: fail with route not found / missing module behavior.

- [ ] **Step 3: Implement thin adapters**

Add routes:
- `GET /api/config`
- `POST /api/config/validate`
- `POST /api/config`
- `POST /api/config/restart-service`

The restart route calls `service.stop_service()` then `service.start_service(load_settings())` only when current service status is running, and keeps pipeline-child behavior unchanged.

- [ ] **Step 4: Run API tests to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_web_v4_config.py tests/test_config_editor.py -q`

Expected: pass.

### Task 3: Web Config Page

**Files:**
- Modify: `src/live_clipper/web_static/index.html`
- Modify: `src/live_clipper/web_static/app.js`
- Modify: `src/live_clipper/web_static/styles.css`
- Test: `tests/test_docs.py`

- [ ] **Step 1: Write failing static/doc tests**

Update assertions so static HTML/JS expose `配置`, `/api/config`, `检查配置`, `保存配置`, and `重启服务`.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_docs.py::test_web_static_exposes_v4_config_editor -q`

Expected: fail because the current page still says `设置`.

- [ ] **Step 3: Implement the UI**

Rename the visible tab to `配置`, add sections `基础路径`, `录播源`, `AI 与 ASR`, `服务行为`, `高级配置`, add form field bindings, dirty-state tracking, validate/save/reload/default/restart actions, env status display, and Chinese result messages.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_docs.py -q`

Expected: pass.

### Task 4: Docs, Regression, Handoff

**Files:**
- Modify: `README.md`
- Modify: `_系统/协作/lanes/development-manager/worklog.md` only for lane worklog after implementation.

- [ ] **Step 1: Document V4**

Add README guidance for the Web `配置` page, API key env-var handling, backups, validation, and service restart boundary.

- [ ] **Step 2: Run full verification**

Run:
- `.venv/bin/python -m pytest -q`
- `uv run --with ruff ruff check src/live_clipper/config_editor.py src/live_clipper/web.py src/live_clipper/web_static tests/test_config_editor.py tests/test_web_v4_config.py tests/test_docs.py`

Expected: all pass.

- [ ] **Step 3: HTTP smoke**

Start Web locally, then check:
- `GET /` contains `配置`
- `GET /api/config` returns `ok: true`
- `POST /api/config/validate` returns structured validation results

- [ ] **Step 4: Commit and request acceptance**

Commit implementation on `codex/v4-web-config-editor`, update development-manager worklog, send an acceptance request to product-manager, and do not merge/push.

---

## Coverage Check

- V4.1 is covered by Tasks 1 and 2.
- V4.2 is covered by Task 3.
- V4.3 is covered by Tasks 2 and 4.
- Non-goals remain excluded: no multi-source editor, no arbitrary TOML editor, no `.env` secret editing, no public access/auth changes, no Web self-restart.
