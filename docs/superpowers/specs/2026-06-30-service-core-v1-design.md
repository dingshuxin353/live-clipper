# live-clipper V1 Service Core Design

Date: 2026-06-30

## Summary

V1 turns `live-clipper` into a local long-running service controlled by the CLI.

The service watches configured recording sources, copies stable NAS recordings into the local project library, starts the existing pipeline in the background, tracks run lifecycle state, detects when review output appears, automatically renders selected clips, and only previews cleanup. It does not expose MCP tools or redesign the Web console yet, but the internal boundaries should prepare for both.

## Product Goal

Users should be able to start one local service and trust it to keep the weekly recording workflow moving without hand-written Codex scheduled prompts:

1. Start the service.
2. Service scans the configured recording source on a schedule.
3. Service stages new stable recordings into `input/`.
4. Service starts the pipeline and records background status.
5. Service stops at the review point until `selected_clips.json` exists.
6. Service auto-renders after selection appears.
7. Service only previews cleanup and never deletes files automatically.

## Non-Goals

- No MCP tools in V1.
- No launchd, login item, or OS-level daemon installation.
- No Web console redesign.
- No automatic deletion of audio, local input copies, clips, or source recordings.
- No full multi-source UI. The configuration shape should leave room for future multi-source support.
- No cloud queue, remote worker, or multi-user permissions.

## User-Facing CLI

V1 adds a new `service` command group:

```bash
live-clipper service start
live-clipper service stop
live-clipper service status
live-clipper service logs
```

Recommended optional flags:

```bash
live-clipper service start --foreground
live-clipper service start --once
live-clipper service logs --follow
live-clipper service status --json
```

`start` should refuse to start a duplicate live service unless the recorded PID is stale.

`stop` should ask the service process to shut down cleanly. If a pipeline child process is running, V1 should stop scheduling new work and report the child state rather than killing it by default.

`status` should summarize service health, configured source, next scan time, active run, pending review runs, rendered runs, failed runs, and last error.

`logs` should print or tail `work/service/service.log`.

## Configuration

V1 extends `live-clipper.toml` with:

```toml
[service]
enabled = true
scan_interval_minutes = 30
auto_render_after_selection = true
cleanup_mode = "preview_only"

[recording_source.default]
source_dir = "/Volumes/homes/weixiaodan12/录播"
input_dir = "input"
output_root = "output"
since_hours = 168
min_age_minutes = 10
stable_check_seconds = 60
```

V1 reads only `recording_source.default`, but the nested table structure intentionally leaves room for future multiple sources.

`cleanup_mode` supports only `preview_only` in V1. Any other value should fail configuration validation with a clear message.

## Run Identity

The local input file keeps the original recording filename:

```text
input/<original_filename>
```

The output run directory uses a unique ID:

```text
output/<source_id>/<recording_stem>__<short_fingerprint>/
```

The fingerprint is derived from:

```text
source_id + source_path + file_size + mtime
```

This avoids overwriting when two recordings have the same filename or when the NAS file changes.

## State Files

V1 introduces a service state directory:

```text
work/service/
  service.pid
  service.json
  service.log
  runs.json
  events.jsonl
```

`service.pid` stores the service process PID.

`service.json` stores current service health:

```json
{
  "status": "running",
  "pid": 12345,
  "started_at": "...",
  "last_heartbeat_at": "...",
  "next_scan_at": "...",
  "config_snapshot": {
    "source_id": "default",
    "source_dir": "/Volumes/homes/weixiaodan12/录播",
    "scan_interval_minutes": 30
  },
  "last_error": null
}
```

`runs.json` stores source-to-run lifecycle records:

```json
{
  "runs": [
    {
      "run_id": "2026-06-19-21-07-13__a8f31c",
      "source_id": "default",
      "source_path": "/Volumes/homes/weixiaodan12/录播/2026-06-19-21-07-13.mkv",
      "local_source_path": "input/2026-06-19-21-07-13.mkv",
      "run_dir": "output/default/2026-06-19-21-07-13__a8f31c",
      "fingerprint": "a8f31c",
      "phase": "needs_review",
      "pid": null,
      "log_path": "work/service/runs/2026-06-19-21-07-13__a8f31c.log",
      "created_at": "...",
      "updated_at": "...",
      "last_error": null
    }
  ]
}
```

`events.jsonl` records append-only service events such as `service_started`, `recording_discovered`, `staging_started`, `pipeline_started`, `phase_changed`, `render_started`, `render_completed`, `cleanup_preview_created`, and `run_failed`.

The existing `work/automation_state/*.json` files can remain readable for compatibility, but V1 service logic should write its own state under `work/service/`.

## Run Lifecycle

V1 lifecycle phases:

```text
discovered -> staging -> processing -> needs_review -> rendering -> rendered
                                      \-> failed
```

`discovered`: the service found a candidate file in the configured source.

`staging`: the service is copying or reusing the local input copy.

`processing`: the background pipeline is running and should produce a review package.

`needs_review`: `codex_brief.json` exists and `selected_clips.json` does not exist.

`rendering`: `selected_clips.json` exists and clips are being rendered.

`rendered`: `clips/*.mp4` exists.

`failed`: any stage failed. The run remains visible with error, log path, and recovery hint.

## Service Loop

The main loop runs while the service is active:

1. Load settings and validate `service` plus `recording_source.default`.
2. Update heartbeat and next scan time.
3. Scan source directory when due.
4. Filter files by supported extension, `since_hours`, `min_age_minutes`, and stability check.
5. Compute fingerprint and skip already-known runs.
6. Stage the recording into `input/`.
7. Start the existing `pipeline` command as a background child.
8. Periodically reconcile run state from files and child PIDs.
9. When `codex_brief.json` exists and no selection exists, mark `needs_review`.
10. When `selected_clips.json` exists and no clips exist, run render.
11. When clips exist, run cleanup preview and mark `rendered`.
12. Append all meaningful transitions to `events.jsonl`.

## Recovery Behavior

On service start, V1 should reconcile state before scanning:

- If `service.pid` points to a dead process, treat it as stale and overwrite it.
- If a run says `processing` but the child PID is dead, infer the current phase from run files.
- If `codex_brief.json` exists and `selected_clips.json` does not, mark `needs_review`.
- If `selected_clips.json` exists and no clips exist, mark `rendering` and render.
- If clips exist, mark `rendered`.
- If expected pipeline outputs are missing and no child is alive, mark `failed` with a recovery hint.

## Safety Rules

- Never delete NAS source recordings.
- Never delete local input copies automatically.
- Never run `cleanup --confirm` automatically.
- Do not start duplicate pipeline jobs for the same source fingerprint.
- Do not overwrite an existing run directory with a different source fingerprint.
- Keep failure logs redacted according to existing privacy settings.
- Bind the Web server to localhost by default if V1 reuses any HTTP health endpoint.

## Testing

V1 should include focused tests for:

- Config parsing and validation.
- Fingerprint and run ID generation.
- Source scanning filters.
- Stable file detection.
- Duplicate prevention.
- Service state read/write.
- Event log append.
- Lifecycle reconciliation from files.
- Auto-render trigger when `selected_clips.json` appears.
- Cleanup preview created but confirm deletion not executed.
- Stale PID handling.

Integration smoke tests should use temporary directories and synthetic small files, not real NAS paths.

## Acceptance Criteria

- `live-clipper service start` starts a local service and writes `service.pid`.
- `live-clipper service status --json` reports service health and known runs.
- A stable source recording is staged to `input/` and processed into a unique `output/<source_id>/<run_id>/` directory.
- The service stops at `needs_review` until `selected_clips.json` exists.
- After `selected_clips.json` appears, the service renders clips automatically.
- The service records lifecycle events in `events.jsonl`.
- No deletion happens without an explicit future confirmation mechanism.
