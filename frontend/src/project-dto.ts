export type ActivationState = "inactive" | "active" | "paused";
export type ProjectMainStatus = "blocked" | "failed" | "awaiting_review" | "processing" | "queued" | "paused" | "inactive" | "idle";
export type RunStatus = "queued" | "processing" | "awaiting_review" | "failed" | "completed";
export type RunStage = "read_source" | "transcribe" | "analyze" | "arbitrate" | "review" | "render";
export type ScanStatus = "running" | "success" | "partial" | "failed";
export type RunFilter = "all" | "active" | "attention" | "completed";

export interface Workload { queued: number; processing: number; awaiting_review: number; failed: number; completed: number }
export interface ProjectRuntime {
  project_id: string; readiness_state: string; auto_scan_state: string; last_scan_at: string | null;
  next_scan_at: string | null; failure_code: string | null; failure_summary: string | null;
  discovery_baseline: string | null; first_scan_state: "not_required" | "pending" | "completed"; schedule_cursor: string | null;
}
export interface ScanEvent {
  scan_id: string; project_id: string; trigger_source?: "manual" | "scheduled"; recovery_scan?: boolean;
  scheduled_at?: string | null; started_at?: string; completed_at?: string | null; status: ScanStatus;
  matched_count?: number; created_count?: number; duplicate_count?: number; unstable_count?: number;
  unsupported_count?: number; excluded_count?: number; failed_count?: number; error_summary?: string | null;
  reused?: boolean; created_run_ids?: string[]; failures?: Array<{ path?: string; message?: string }>;
}
export interface ProjectConfig {
  schema_version: 1;
  source: { directory: string; supported_extensions: string[]; include_patterns: string[]; exclude_patterns: string[]; first_scan_mode: "new_only" | "recent" | "choose_existing"; lookback_days: 3 | 7 | 30 | null };
  schedule: { enabled: boolean; mode: "daily" | "interval"; daily_time: string | null; interval_minutes: 30 | 60 | 180 | 360 | 720 | null; timezone: string };
  resources: { asr_ref: string; analysis_ref: string; arbitration_mode: "reuse_analysis"; arbitration_ref: null };
  processing: { review_strategy: "manual"; output_profile: "current_renderer"; naming_policy: "system_safe" };
  output: { directory: string; intermediate_retention: "remind_immediately" | "remind_after_7_days" | "keep"; original_media_policy: "never_delete"; final_media_policy: "keep" };
}
export interface ProjectConfigRevision { project_id: string; revision: number; config: ProjectConfig; schema_version: number; created_at: string }
export interface Run {
  run_id: string; project_id: string; content_id: string; processing_sequence: number; origin_run_id: string | null;
  source_scan_id: string | null; trigger_source: string; first_seen_path: string; latest_seen_path: string;
  status: RunStatus; current_stage: RunStage | null; config_revision: number; parameter_snapshot: Record<string, unknown>;
  queued_at: string; started_at: string | null; review_at: string | null; completed_at: string | null; updated_at: string;
  error_code: string | null; error_summary: string | null; queue_position?: number | null;
}
export interface ProjectSummary {
  project_id: string; name: string; description: string; activation_state: ActivationState; current_config_revision: number;
  created_at: string; updated_at: string; activated_at: string | null; paused_at: string | null; main_status: ProjectMainStatus;
  workload: Workload; readiness_state: string; runtime: ProjectRuntime | null; latest_scan: ScanEvent | null;
  current_run: Run | null; recent_result: Run | null; blocking_issues: Array<{ code: string | null; message: string | null }>;
  schedule: { enabled: boolean; timezone: string; next_scan_at: string | null } | null; config?: ProjectConfigRevision | null;
}
export interface WorkspaceEvent { event_id: number; event_type: string; project_id: string | null; run_id: string | null; scan_id: string | null; occurred_at: string; payload: Record<string, unknown> }
export interface StudioPayload {
  ok: true; through_event_id: number; changes: WorkspaceEvent[]; pending_review_count: number; workload: Workload;
  unattended_changes: { created: Run[]; completed: Run[]; awaiting_review: Run[]; failed: Run[] };
  needs_attention: { failed_runs: Run[]; blocked_project_ids: string[] };
  in_progress: { processing: Run[]; queued: Run[] }; recent_results: Run[]; project_health: ProjectSummary[]; projects: ProjectSummary[];
}
export interface ResourceOption { resource_id: string; display_name: string; resource_type: string; ready: boolean; status: string; detail?: string | null }
export interface FormOptionsPayload {
  ok: true; data_mode: "legacy" | "projects"; resources: ResourceOption[];
  first_scan_modes: ProjectConfig["source"]["first_scan_mode"][]; lookback_days: Array<3 | 7 | 30>;
  schedule_modes: ProjectConfig["schedule"]["mode"][]; interval_minutes: Array<30 | 60 | 180 | 360 | 720>;
  intermediate_retention: ProjectConfig["output"]["intermediate_retention"][]; timezone: string;
  defaults: { first_scan_mode: ProjectConfig["source"]["first_scan_mode"]; lookback_days: 3 | 7 | 30 | null; schedule_enabled: boolean; schedule_mode: ProjectConfig["schedule"]["mode"]; daily_time: string; intermediate_retention: ProjectConfig["output"]["intermediate_retention"] };
}
export interface ValidationIssue { field: string; code: string; message: string }
export interface ValidationPayload { ok: true; valid: boolean; fatal: ValidationIssue[]; blockers: ValidationIssue[]; warnings: ValidationIssue[]; normalized_config: ProjectConfig | null }
export interface ScanPreviewPayload { ok: true; estimated_files: number; supported_files: number; processable_files: number; warnings: string[] }
export interface SourceFile { relative_path: string; bytes: number; modified_at: string; selectable: boolean; reason: string | null }
export interface StageEvent { event_id: number; run_id: string; stage: RunStage; event_type: string; occurred_at: string; detail: Record<string, unknown> }
