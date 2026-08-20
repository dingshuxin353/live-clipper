import { defaultConfig } from "../src/config";

export const MODELS = [
  {
    id: "mlx-community/whisper-small-mlx-q4",
    display_name: "Small",
    tier_label: "轻量",
    size_note: "约 500 MB",
    state: "installed",
    current: true,
  },
  {
    id: "mlx-community/whisper-medium-mlx-q4",
    display_name: "Medium",
    tier_label: "平衡",
    size_note: "约 1.5 GB",
    state: "missing",
  },
  {
    id: "mlx-community/whisper-large-v3-turbo",
    display_name: "Large",
    tier_label: "高精度",
    size_note: "约 3 GB",
    state: "missing",
  },
];

export const PROJECT = {
  project_id: "project-1",
  name: "游戏直播高光",
  description: "晚场直播自动生产线",
  activation_state: "active",
  current_config_revision: 1,
  created_at: "2026-08-20T01:00:00Z",
  updated_at: "2026-08-20T02:00:00Z",
  activated_at: "2026-08-20T01:00:00Z",
  paused_at: null,
  main_status: "processing",
  workload: { processing: 1, queued: 1, awaiting_review: 0, failed: 0, completed: 2 },
  readiness_state: "ready",
  runtime: { project_id: "project-1", readiness_state: "ready", auto_scan_state: "off", last_scan_at: null, next_scan_at: null, failure_code: null, failure_summary: null, discovery_baseline: null, first_scan_state: "not_required", schedule_cursor: null },
  latest_scan: null,
  current_run: null,
  recent_result: null,
  blocking_issues: [],
  schedule: { enabled: false, timezone: "Asia/Tokyo", next_scan_at: null },
  config: {
    project_id: "project-1",
    revision: 1,
    schema_version: 1,
    created_at: "2026-08-20T01:00:00Z",
    config: {
      schema_version: 1,
      source: { directory: "/recordings", supported_extensions: [".m4v", ".mkv", ".mov", ".mp4", ".webm"], include_patterns: [], exclude_patterns: [], first_scan_mode: "new_only", lookback_days: null },
      schedule: { enabled: false, mode: "daily", daily_time: "22:00", interval_minutes: null, timezone: "Asia/Tokyo" },
      resources: { asr_ref: "asr.local", analysis_ref: "analysis.main", arbitration_mode: "reuse_analysis", arbitration_ref: null },
      processing: { review_strategy: "manual", output_profile: "current_renderer", naming_policy: "system_safe" },
      output: { directory: "/outputs", intermediate_retention: "remind_after_7_days", original_media_policy: "never_delete", final_media_policy: "keep" },
    },
  },
};

export const RUN = {
  run_id: "run-1", project_id: "project-1", content_id: "content-1", processing_sequence: 1,
  origin_run_id: null, source_scan_id: null, trigger_source: "manual", first_seen_path: "night.mkv",
  latest_seen_path: "night.mkv", status: "processing", current_stage: "analyze", config_revision: 1,
  parameter_snapshot: {}, queued_at: "2026-08-20T01:00:00Z", started_at: "2026-08-20T01:01:00Z",
  review_at: null, completed_at: null, updated_at: "2026-08-20T02:00:00Z", error_code: null,
  error_summary: null, queue_position: null,
};

export function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}

export function installFetchMock(overrides: Record<string, unknown> = {}) {
  const calls: Array<[string, RequestInit | undefined]> = [];
  const base: Record<string, unknown> = {
    "/api/onboarding": { needs_onboarding: false },
    "/api/service": {
      ok: true,
      running: false,
      service: { status: "stopped" },
      pending_review_runs: [],
      failed_runs: [],
      pending_confirmation_count: 0,
    },
    "/api/runs": { ok: true, runs: [] },
    "/api/confirmations": { ok: true, confirmations: [] },
    "/api/events": { ok: true, events: [] },
    "/api/config": {
      ok: true,
      config: defaultConfig(),
      config_path: "live-clipper.toml",
      exists: true,
      env_status: {},
      warnings: [],
    },
    "/api/scheduler": { ok: true, scheduler: { enabled: true }, jobs: [] },
    "/api/review-automation": {
      ok: true,
      review_automation: { enabled: false },
      environment: {},
    },
    "/api/asr/models": { ok: true, models: MODELS, download_source: "modelscope" },
    "/api/projects": { ok: true, projects: [PROJECT] },
    "/api/projects/project-1": { ok: true, project: PROJECT },
    "/api/projects/project-1/runs": { ok: true, runs: [RUN], cursor: null, has_more: false },
    "/api/runs/run-1": { ok: true, run: RUN, stage_events: [] },
    "/api/studio": {
      ok: true,
      through_event_id: 0,
      changes: [],
      pending_review_count: 0,
      workload: PROJECT.workload,
      unattended_changes: { created: [], completed: [], awaiting_review: [], failed: [] },
      needs_attention: { failed_runs: [], blocked_project_ids: [] },
      in_progress: { processing: [RUN], queued: [] },
      recent_results: [],
      project_health: [PROJECT],
      projects: [PROJECT],
    },
    "/api/project-form-options": {
      ok: true,
      data_mode: "projects",
      resources: [
        { resource_id: "asr.local", display_name: "本地 ASR", resource_type: "asr", ready: true, status: "ready" },
        { resource_id: "analysis.main", display_name: "主分析模型", resource_type: "analysis", ready: true, status: "ready" },
      ],
      first_scan_modes: ["new_only", "recent", "choose_existing"],
      lookback_days: [3, 7, 30],
      schedule_modes: ["daily", "interval"],
      interval_minutes: [30, 60, 180, 360, 720],
      intermediate_retention: ["remind_immediately", "remind_after_7_days", "keep"],
      timezone: "Asia/Tokyo",
      defaults: { first_scan_mode: "new_only", lookback_days: null, schedule_enabled: false, schedule_mode: "daily", daily_time: "22:00", intermediate_retention: "remind_after_7_days" },
    },
    "/api/projects/scan-preview": { ok: true, estimated_files: 1, supported_files: 1, processable_files: 1, warnings: [] },
    "/api/projects/validate": { ok: true, valid: true, fatal: [], blockers: [], warnings: [], normalized_config: PROJECT.config.config },
  };
  const payloads = { ...base, ...overrides };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    calls.push([path, options]);
    const value = payloads[path] ?? payloads[path.split("?")[0]];
    if (value instanceof Error) return Promise.reject(value);
    if (typeof value === "function") return (value as (options?: RequestInit) => Promise<Response>)(options);
    if (value === undefined) return jsonResponse({ ok: true });
    return jsonResponse(value);
  }));
  return calls;
}
