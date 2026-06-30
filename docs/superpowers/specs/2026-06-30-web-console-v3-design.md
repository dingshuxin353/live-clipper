# live-clipper V3 Web Console Design

Date: 2026-06-30

## Summary

V3 upgrades the Web console into the unified control surface for the service.

The current Web console reads output runs and exposes local actions. V3 makes it a client of Service Core, showing the same service status, run lifecycle, event log, MCP-created confirmation queue, and safe actions that CLI and MCP use.

## Product Goal

Users should be able to open one local Web console and understand:

- Whether the service is running.
- Which recording sources are configured.
- What runs exist and what phase each run is in.
- Which runs need review.
- Which runs failed and why.
- Which delete requests are pending confirmation.
- Which files would be affected by cleanup.

Users should also be able to batch-confirm or reject deletion requests created by AI/MCP.

## Non-Goals

- No public internet exposure.
- No multi-user account system.
- No cloud storage or remote worker management.
- No advanced timeline editing UI.
- No full visual clip editor.
- No complex settings editor in V3. Settings are shown read-only except for service actions.

## Architecture

```text
Web UI
  -> Web API
    -> Service Core
      -> service.json
      -> runs.json
      -> confirmations.json
      -> events.jsonl
      -> output files
```

The Web API should not maintain a separate status inference model when Service Core has the answer. It can still read output files through Service Core helpers for details such as clips and review package paths.

## Main Navigation

V3 Web console has five sections:

```text
Service
Runs
Confirmations
Logs
Settings
```

## Service Page

Shows:

- Service status: running, stopped, stale, error.
- PID.
- Started time.
- Last heartbeat.
- Next scan time.
- Configured source summary.
- Active processing count.
- Pending review count.
- Pending confirmation count.
- Last error.

Actions:

- Start service.
- Stop service.
- Scan now.
- Refresh.

Start and stop call Service Core and append events.

## Runs Page

Shows a table of runs with filters:

- all
- processing
- needs_review
- rendering
- rendered
- failed

Columns:

- source id
- run id
- source name
- phase
- candidate count
- selected count
- clip count
- last updated
- primary action

Primary actions:

- `needs_review`: open review package.
- `rendered`: preview cleanup.
- `failed`: view logs.
- `processing`: view logs.

## Run Detail Page

Shows:

- Source recording path.
- Local input path.
- Run directory.
- Current phase.
- File checklist.
- Candidate counts.
- Selected clip count.
- Rendered clip list.
- Log tail.
- Event timeline.
- Cleanup preview.

Actions:

- Render when `selected_clips.json` exists and clips are missing.
- Preview cleanup when selection exists.
- Create delete requests for clips.
- Open file paths in a copy-friendly way.

Actual deletion still goes through Confirmations.

## Confirmations Page

This is the major V3 addition.

The page lists pending requests from:

- MCP tools.
- Web actions that require confirmation.
- Future CLI actions that choose to defer destructive work.

Columns:

- checkbox
- action
- run id
- target path
- reason
- risk level
- created by
- created at
- status

Supported actions:

- Approve selected.
- Reject selected.
- Open run detail.
- Preview affected files.

Approval rules:

1. Re-read the confirmation by ID.
2. Re-run validation from the stored validation envelope.
3. Confirm the target still exists and is still within the allowed path.
4. Confirm the run phase still allows the action.
5. Execute the deletion or cleanup.
6. Mark confirmation as `approved_executed`.
7. Append `confirmation_executed` event.

Rejection rules:

1. Mark confirmation as `rejected`.
2. Store optional rejection reason if provided.
3. Append `confirmation_rejected` event.

Batch approval should execute each selected request independently and return per-item results. One failure should not hide the status of other selected requests.

## Confirmation Actions

`delete_clip`

Allowed only for rendered `.mp4` files under the run's `clips/` directory.

`cleanup_confirm`

Allowed only for files returned by cleanup preview with `deletable=true`.

`delete_local_source`

Allowed only when:

- clips exist,
- the local source is under configured `input_dir`,
- the local source differs from `original_source_path`,
- the source is not the NAS original.

## Logs Page

Shows:

- Service log tail.
- Event stream.
- Selected run log tail.
- Failed run summaries.

The event stream should be filterable by run id and event type.

## Settings Page

V3 settings are read-only by default:

- source directory
- input directory
- output root
- scan interval
- auto-render setting
- cleanup mode
- web host and port

The page may show guidance for editing `live-clipper.toml`, but does not need to write config.

## Web API Shape

Recommended endpoints:

```text
GET  /api/service
POST /api/service/start
POST /api/service/stop
POST /api/service/scan-now

GET  /api/runs
GET  /api/runs/<run_id>
GET  /api/runs/<run_id>/log
POST /api/runs/<run_id>/render
POST /api/runs/<run_id>/cleanup-preview

GET  /api/confirmations
POST /api/confirmations/<id>/approve
POST /api/confirmations/<id>/reject
POST /api/confirmations/batch-approve
POST /api/confirmations/batch-reject

GET  /api/events
GET  /api/settings
```

Existing endpoints can be preserved temporarily, but V3 UI should prefer Service Core endpoints.

## Safety Rules

- Bind to `127.0.0.1` by default.
- Do not expose Web console publicly.
- Do not execute stale confirmation requests without revalidation.
- Show risk level before approval.
- Batch confirmation must show per-item results.
- Never delete NAS original recordings.
- Deletion events must include action, target, result, and source confirmation ID.

## Testing

V3 should include tests for:

- Service page API state.
- Runs list from Service Core state.
- Run detail with file checklist.
- Confirmation queue listing.
- Batch approval partial success.
- Batch rejection.
- Revalidation blocking stale or path-invalid requests.
- `delete_clip` approval restricted to `clips/*.mp4`.
- `cleanup_confirm` approval restricted to cleanup plan targets.
- `delete_local_source` approval protecting NAS originals.
- Web API error payloads.

## Acceptance Criteria

- Web console shows service status from Service Core.
- Web console shows runs using Service Core lifecycle phases.
- MCP-created delete requests appear in the confirmation queue.
- User can approve or reject confirmations individually.
- User can batch approve or batch reject confirmations.
- Approval revalidates path and run state before executing.
- Deletion results are recorded in `events.jsonl`.
- Web, CLI, and MCP observe the same run state after actions.
