# live-clipper V2 MCP Tools Design

Date: 2026-06-30

## Summary

V2 adds an MCP server that lets AI agents operate `live-clipper` through a stable tool interface.

The MCP server is a thin adapter over the V1 Service Core. It does not duplicate workflow logic, parse output folders independently, or shell out ad hoc commands when a Service Core action exists. All meaningful actions should update the same service state and event log that CLI and Web use.

## Product Goal

AI agents should be able to inspect service status, find runs needing review, read review packages, write selections, trigger renders, preview cleanup, and request destructive actions without directly deleting files.

## Non-Goals

- No separate MCP-only job scheduler.
- No second run-state database.
- No automatic clip selection built into the service.
- No direct deletion by MCP tools.
- No Web confirmation UI in V2. The confirmation queue is written in V2 and acted on in V3.
- No remote/cloud MCP deployment.

## Architecture

```text
AI Agent
  -> MCP Server
    -> Service Core
      -> work/service/*.json
      -> work/service/events.jsonl
      -> input/
      -> output/
```

MCP validates parameters, calls Service Core functions, and returns structured results. Service Core performs all state transitions and file operations.

## Tool Groups

### Read Tools

`get_service_status`

Returns service health, PID, heartbeat, configured sources, active runs, pending review count, rendered count, failed count, and pending confirmation count.

`list_runs`

Parameters:

```json
{
  "phase": "optional phase filter",
  "source_id": "optional source id",
  "limit": 20
}
```

Returns compact run summaries.

`get_run_detail`

Parameters:

```json
{
  "run_id": "..."
}
```

Returns source path, local path, run dir, phase, files, candidates count, selected count, rendered clips, last error, and relevant event tail.

`get_run_log`

Parameters:

```json
{
  "run_id": "...",
  "lines": 200
}
```

Returns the tail of the run log.

`get_review_package`

Parameters:

```json
{
  "run_id": "..."
}
```

Returns paths and compact contents for `codex_brief.json`, `codex_review.md`, `selected_clips.template.json`, and `refined_candidates.json` when present. Large payloads should be bounded; the tool can return paths plus summaries if files are too large.

### Safe Action Tools

`scan_now`

Asks the service to scan configured recording sources immediately.

`start_run_for_source`

Parameters:

```json
{
  "source_path": "/absolute/path/to/recording.mkv",
  "source_id": "default"
}
```

Starts a run for a specific source file after applying the same stability and duplicate checks as the service loop.

`write_selected_clips`

Parameters:

```json
{
  "run_id": "...",
  "selected_clips": [
    {
      "clip_id": "...",
      "source_start": 12.5,
      "source_end": 58.0,
      "title": "...",
      "remove_ranges": []
    }
  ]
}
```

Validates the selection using existing selection validation before writing `selected_clips.json`.

`render_run`

Parameters:

```json
{
  "run_id": "..."
}
```

Triggers render if `selected_clips.json` exists and clips are not already rendered.

`preview_cleanup`

Parameters:

```json
{
  "run_id": "..."
}
```

Returns cleanup targets and writes a cleanup preview event. It does not delete.

### Confirmation-Required Tools

V2 includes destructive-intent tools, but they do not perform deletion. They create pending confirmation requests.

`delete_clip`

Creates a confirmation request to delete a rendered clip.

`cleanup_confirm`

Creates a confirmation request to execute cleanup on plan-approved deletable files.

`delete_local_source`

Creates a confirmation request to delete the local `input/` source copy after rendered clips exist.

All three return:

```json
{
  "status": "confirmation_required",
  "confirmation_id": "confirm_...",
  "message": "用户需要在 Web 控制台确认后才会执行删除。"
}
```

## Confirmation Request Model

V2 writes pending confirmations into Service Core state:

```text
work/service/confirmations.json
```

Shape:

```json
{
  "confirmations": [
    {
      "id": "confirm_20260630_001",
      "action": "delete_clip",
      "run_id": "...",
      "target_path": "output/default/.../clips/clip_03.mp4",
      "reason": "AI judged this rendered clip should be removed.",
      "risk_level": "low",
      "created_by": "mcp",
      "created_at": "...",
      "status": "pending",
      "validation": {
        "must_be_relative_to": "output/default/.../clips",
        "allowed_suffixes": [".mp4"]
      }
    }
  ]
}
```

Risk levels:

- `low`: delete a rendered clip under `clips/`.
- `medium`: cleanup confirmed intermediates or delete local input copy.
- `high`: reserved for future operations; V2 should not create high-risk delete requests.

## Delete Safety Rules

MCP tools must never delete directly.

Every destructive-intent request must:

- Resolve the target path.
- Store a validation envelope.
- Record the requesting tool and reason.
- Append a `confirmation_created` event.
- Return `confirmation_required`.

Execution is reserved for V3 Web confirmation handling, which re-validates at confirmation time.

## Error Handling

MCP tools should return structured errors:

```json
{
  "ok": false,
  "error_code": "run_not_found",
  "message": "Run not found: ..."
}
```

Recommended error codes:

- `service_not_running`
- `run_not_found`
- `invalid_phase`
- `selection_validation_failed`
- `duplicate_run`
- `source_not_stable`
- `path_rejected`
- `confirmation_required`
- `internal_error`

## Testing

V2 should include tests for:

- Tool schema validation.
- Read tools returning Service Core state.
- `write_selected_clips` validation and file output.
- `render_run` phase checks.
- `preview_cleanup` not deleting files.
- Destructive tools creating confirmation requests instead of deleting.
- Path traversal rejection.
- Event logging for MCP actions.

## Acceptance Criteria

- An AI client can query service status through MCP.
- An AI client can list runs and find `needs_review` runs.
- An AI client can read the review package for a run.
- An AI client can write validated `selected_clips.json`.
- An AI client can trigger render.
- Cleanup preview is available through MCP.
- Delete requests return `confirmation_required` and appear in the confirmation queue.
- No MCP tool directly deletes files.
