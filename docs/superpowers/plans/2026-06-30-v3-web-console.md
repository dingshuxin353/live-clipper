# V3 Web Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the local Web console into the Service Core control surface.

**Architecture:** Service Core owns run, event, and confirmation state. Web API reads `work/service/*`, calls Service Core or V2 MCP tool adapters for actions, and never directly deletes files outside confirmation approval. Static UI renders Service, Runs, Confirmations, Logs, and Settings from the new API.

**Tech Stack:** Python 3.11 standard-library HTTP server, existing Service Core, pytest, vanilla HTML/CSS/JS.

---

### Task 1: Confirmation Execution Core

**Files:**
- Modify: `src/live_clipper/service.py`
- Test: `tests/test_web_v3.py`

- [ ] Write failing tests for approve/reject, batch approve/reject, path rejection, and NAS-original protection.
- [ ] Add Service Core helpers to find/update confirmations, execute `delete_clip`, `cleanup_confirm`, and `delete_local_source`, and append execution/rejection events.
- [ ] Run the new tests and keep existing service/MCP tests passing.

### Task 2: Service-Core Web API

**Files:**
- Modify: `src/live_clipper/web.py`
- Test: `tests/test_web_v3.py`

- [ ] Write failing tests for `GET /api/service`, `GET /api/runs`, run detail/log, confirmations, events, settings, and confirmation action endpoints.
- [ ] Route V3 endpoints to Service Core and V2 MCP adapters.
- [ ] Convert old destructive run endpoints into confirmation requests rather than direct deletion.

### Task 3: V3 Static Console

**Files:**
- Modify: `src/live_clipper/web_static/index.html`
- Modify: `src/live_clipper/web_static/app.js`
- Modify: `src/live_clipper/web_static/styles.css`
- Test: `tests/test_docs.py`

- [ ] Replace the old output-only layout with Service, Runs, Confirmations, Logs, and Settings sections.
- [ ] Render confirmation queue actions and batch action buttons.
- [ ] Keep styling dense and operational, without marketing copy or nested card-heavy layout.

### Task 4: Verification And Handoff

**Files:**
- Modify: `_系统/协作/lanes/development-manager/worklog.md`

- [ ] Run V3 tests, full pytest, and touched-file ruff.
- [ ] Start the local Web server for a smoke check.
- [ ] Send product-manager an acceptance request; do not merge.
