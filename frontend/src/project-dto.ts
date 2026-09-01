export type ActivationState = "inactive" | "active" | "paused";
export type ProjectMainStatus = "blocked" | "failed" | "processing" | "queued" | "new_results" | "paused" | "inactive" | "idle";
export type RunStatus = "queued" | "processing" | "awaiting_review" | "failed" | "completed";
export type RunStage = "read_source" | "transcribe" | "analyze" | "arbitrate" | "review" | "render";
export type ScanStatus = "running" | "success" | "partial" | "failed";
export type RunFilter = "all" | "active" | "attention" | "completed";
export type ResultType = "clips_ready" | "no_clip" | "partial" | "unavailable";
export type OutputStatus = "pending" | "rendering" | "ready" | "failed" | "missing" | "unreadable";
export type IssueStatus = "retrying" | "action_required" | "checking" | "ready_to_recover" | "recovering" | "resolved";
export type AvailableAction = "recheck" | "open_resource_repair" | "select_source" | "select_recovery_output" | "continue_run" | "retry_output" | "retry_material" | "copy_diagnostic";
export type IssueAction = AvailableAction;

export interface Workload { queued: number; processing: number; failed: number; completed: number; new_results: number }
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
  schema_version: 2;
  source: { directory: string; supported_extensions: string[]; include_patterns: string[]; exclude_patterns: string[]; first_scan_mode: "new_only" | "recent" | "choose_existing"; lookback_days: 3 | 7 | 30 | null };
  schedule: { enabled: boolean; mode: "daily" | "interval"; daily_time: string | null; interval_minutes: 30 | 60 | 180 | 360 | 720 | null; timezone: string };
  resources: { asr_ref: string; analysis_ref: string; review_ref: string; arbitration_mode: "reuse_analysis"; arbitration_ref: null };
  processing: { review_strategy: "ai_auto"; output_profile: "current_renderer"; naming_policy: "system_safe"; review_policy_version: "auto_review_v1"; material_policy_version: "publish_material_v1" };
  output: { directory: string; intermediate_retention: "remind_immediately" | "remind_after_7_days" | "keep"; original_media_policy: "never_delete"; final_media_policy: "keep" };
}
export interface ProjectConfigRevision { project_id: string; revision: number; config: ProjectConfig; schema_version: number; created_at: string }
export interface IssueSummary {
  issue_id: string; issue_code: string; group_key: string; status: IssueStatus; impact_level: string;
  title: string; summary: string; next_step: string; issue_revision: number; available_actions: IssueAction[];
}
export interface IssueGroupSummary { group_key: string; count: number; title: string; impact_level: string; issue_ids: string[]; available_actions: IssueAction[] }
export interface ResultSummary {
  run_id: string; project: { project_id: string; name: string }; source_name: string; result_type: Exclude<ResultType, "unavailable">;
  result_revision: number; seen: boolean; overall_summary: string; available_output_count: number; failed_output_count: number;
  total_duration_ms: number; primary_output_id: string | null; completed_at: string; issue_summary: IssueSummary | null;
}
export type ClipResultCard = ResultSummary;
export interface Run {
  run_id: string; project_id: string; content_id: string; processing_sequence: number; origin_run_id: string | null;
  source_scan_id: string | null; source_name: string; trigger_source: string; status: RunStatus; current_stage: RunStage | null;
  config_revision: number; queued_at: string; started_at: string | null; review_at: string | null; completed_at: string | null;
  updated_at: string; error_code: string | null; error_summary: string | null; queue_position?: number | null;
  has_result?: boolean; result_summary?: ResultSummary | null; active_issue_summary?: IssueSummary | null; legacy_awaiting_review?: boolean;
}
export interface ProjectSummary {
  project_id: string; name: string; description: string; activation_state: ActivationState; current_config_revision: number;
  created_at: string; updated_at: string; activated_at: string | null; paused_at: string | null; main_status: ProjectMainStatus;
  workload: Workload; readiness_state: string; runtime: ProjectRuntime | null; latest_scan: ScanEvent | null;
  current_run: Run | null; recent_result: ResultSummary | null; unseen_result_count: number; issue_groups: IssueGroupSummary[];
  blocking_issues: Array<{ code: string | null; message: string | null } | IssueSummary>;
  schedule: { enabled: boolean; timezone: string; next_scan_at: string | null } | null; config?: ProjectConfigRevision | null;
}
export interface WorkspaceEvent { event_id: number; event_type: string; project_id: string | null; run_id: string | null; scan_id: string | null; occurred_at: string; payload: Record<string, unknown> }
export interface StudioPayload {
  ok: true; through_event_id: number; changes: WorkspaceEvent[]; unseen_result_count: number; legacy_awaiting_review_count: number; workload: Workload;
  unattended_changes: { created: Run[]; completed: Run[]; failed: Run[] };
  needs_attention: { failed_runs: Run[]; blocked_project_ids: string[]; issue_groups: IssueGroupSummary[] };
  in_progress: { processing: Run[]; queued: Run[] }; recent_results: ResultSummary[]; project_health: ProjectSummary[]; projects: ProjectSummary[];
}
export interface ClipsPayload { ok: true; view: "new" | "all"; unseen_result_count: number; results: ResultSummary[]; cursor: string | null; has_more: boolean }
export interface RunResult {
  run_id: string; review_session_id: string; result_type: ResultType; candidate_count: number; selected_count: number; rejected_count: number;
  available_output_count: number; failed_output_count: number; total_duration_ms: number; overall_summary: string; warnings: string[];
  format_version: number; result_revision: number; seen: boolean; seen_at: string | null; source_kind: string; completed_at: string; updated_at: string;
}
export interface ReviewSession {
  review_session_id: string; attempt_number: number; status: string; resource_ref: string; model_name: string; strategy_version: string;
  format_version: number; overall_summary: string; warnings: string[]; candidate_count: number; selected_count: number; rejected_count: number;
  started_at: string; completed_at: string | null; validated_at: string | null;
}
export interface ReviewDecision {
  decision_id: string; candidate_id: string; decision: "selected" | "rejected"; rank: number | null; candidate_type: string;
  source_start_ms: number; source_end_ms: number; selected_start_ms: number | null; selected_end_ms: number | null;
  remove_ranges: Array<{ start_ms: number; end_ms: number }>; hook: string | null; core_value: string | null; reason: string | null;
  rejection_reason_code: string | null; risks: string[]; transcript_excerpt: string | null; output_id: string | null;
}
export type CandidateDecision = ReviewDecision;
export interface MaterialSummary { material_id: string; status: string; material_revision: number; preferred_title_id: string | null; saved_at: string | null }
export interface RunOutput {
  output_id: string; run_id: string; project_id: string; candidate_id: string; status: OutputStatus; display_order: number;
  file_name: string; duration_ms: number | null; width: number | null; height: number | null; container: string | null;
  video_codec: string | null; byte_size: number | null; generated_at: string | null; verified_at: string | null; available: boolean;
  media_url: string | null; display_path?: string; material: MaterialSummary | null; active_issue_summary: IssueSummary | null;
}
export interface OutputMaterial {
  material_id: string; output_id: string; status: string; material_revision: number; titles: Array<{ title_id: string; text: string }>;
  preferred_title_id: string | null; description: string; tags: string[]; generated_from: string; saved_at: string | null; active_issue_summary: IssueSummary | null;
}
export type MaterialDraft = Pick<OutputMaterial, "titles" | "preferred_title_id" | "description" | "tags" | "material_revision">;
export interface IssueDetail extends IssueSummary {
  category: string; scope: { type: "project" | "run" | "output" | "material"; project_id: string; run_id: string | null; output_id: string | null; material_id: string | null };
  impact: string; preserved_content: string; safe_checkpoint: RunStage | null; reuse_stages: RunStage[]; redo_stages: RunStage[];
  automatic_attempt_count: number; total_attempt_count: number; next_retry_at: string | null; retry_exhausted: boolean;
  diagnostic: { diagnostic_id: string | null; summary: string | null }; occurred_at: string; updated_at: string; resolved_at: string | null;
  events: IssueEvent[];
}
export interface IssueEvent { issue_event_id?: number; event_id?: number; issue_id: string; event_type: string; occurred_at: string; detail?: Record<string, unknown> }
export type IssueGroup = IssueGroupSummary;
export interface RecoveryAttempt { ok: true; run_id: string; recovery_attempt_id: string; queued_at: string; queue_position: number | null; reused_stages: string[]; rerun_stages: string[] }
export interface RunResultPayload { ok: true; result: RunResult; review_session: ReviewSession | null; decisions: ReviewDecision[]; outputs: RunOutput[]; issues: IssueSummary[]; available_actions: string[] }
export interface RepairContext { resource_id: string; display_name: string; resource_type: string; api_base: string | null; model: string | null; credential_state: string; repair_capability: "inline_connection" | "settings_only"; settings_url: string; issue_id: string }
export interface ResourceOption { resource_id: string; display_name: string; resource_type: string; ready: boolean; problem: string | null; version: string | null }
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
export type OnboardingEntryMode = "onboarding" | "workbench" | "migration_required" | "diagnostic_required";
export type OnboardingEntryState = "new" | "resume" | "paused" | "activation_pending" | null;
export type OnboardingSessionState = "in_progress" | "paused" | "activation_pending" | "completed";
export type OnboardingStep = "welcome" | "asr" | "ai" | "project" | "complete";
export interface OnboardingDraft {
  asr?: { mode?: "local" | "cloud"; local_model_id?: string; model_source?: string; api_base?: string; model?: string };
  ai?: { provider_id?: string; api_base?: string; model?: string };
  project?: { name?: string; source_directory?: string; trigger_mode?: "manual" | "scheduled"; schedule_mode?: "daily" | "interval"; daily_time?: string; interval_minutes?: number; output_directory?: string };
}
export interface OnboardingSession {
  state: OnboardingSessionState; current_step: OnboardingStep; revision: number; draft: OnboardingDraft;
  pending_finish_request_id: string | null; failure: { code: string; summary: string | null } | null;
  first_project: { project_id: string; name: string; activation_state: ActivationState; readiness_state: string } | null;
}
export interface OnboardingEnvironmentCheck { name: string; status: "ready" | "blocked" | string; problem: string | null }
export interface OnboardingEnvironment { status: "ready" | "blocked" | string; checks: OnboardingEnvironmentCheck[] }
export interface OnboardingResourceSummary {
  mode?: "local" | "cloud"; configured: boolean; ready: boolean; model_id?: string | null; model_label?: string | null;
  provider_label?: string | null; api_base_display?: string | null; model?: string | null; credential_present: boolean; problem: string | null;
}
export interface OnboardingModel {
  id: string; display_name: string; backend: string; tier: "light" | "balanced" | "high_accuracy" | string;
  tier_label: string; size_note: string; ram_note: string; speed_note: string; accuracy_note: string; recommended: boolean;
  state: "not_installed" | "downloading" | "installed" | "damaged" | string; state_reason: string | null;
  installed: boolean; downloading: boolean; job_id: string | null; installed_bytes: number; partial_bytes: number;
  bytes_downloaded: number; bytes_total: number; download_source: string; current: boolean;
}
export interface OnboardingProviderPreset { id: string; label: string; api_base: string; model: string; signup_url?: string | null }
export type MigrationEntry = "inspect" | "review" | "executing" | "completed" | "failed" | "diagnostic";
export type MigrationSessionState = "backing_up" | "migrating" | "validating" | "completed_ready" | "completed_attention" | "failed_rolled_back" | "diagnostic_required";
export type MigrationStage = "copy" | "project" | "history" | "database" | "complete" | "rolled_back" | null;
export interface MigrationChoices {
  project_name: string; source_directory: string; output_directory: string; trigger_mode: "manual" | "scheduled";
  schedule_mode: "daily" | "interval" | null; daily_time: string | null; interval_minutes: number | null;
}
export interface MigrationHistoryEntry { display_identity: string; category: "importable" | "compatibility" | "quarantined"; reason_code: string | null; reason_label: string; safe_result: boolean }
export interface MigrationPlan {
  plan_version: number; source_fingerprint: string; plan_hash: string;
  project: Omit<MigrationChoices, "project_name"> & { name: string; timezone: string };
  resources: Record<string, { label: string; model: string | null; credential_present: boolean; status: "ready" | "problem" }>;
  discovery: { legacy_weekly_detected: boolean; default_trigger_mode: "manual"; existing_recordings_scanned: boolean };
  history: { counts: { importable: number; compatibility: number; quarantined: number; safe_result: number }; entries: MigrationHistoryEntry[]; quarantine_reason_codes: string[] };
  backup: { target_display: string; source_bytes: number; required_bytes: number; available_bytes: number; space_status: "ready" | "insufficient" };
  readiness: { source_status: string; output_status: string; resource_problems: string[]; can_start: boolean };
  required_choices: string[]; warnings: string[]; choices: MigrationChoices;
}
export interface MigrationSession {
  migration_id: string; state: MigrationSessionState; stage: MigrationStage; revision: number;
  processed_history_count: number | null; total_history_count: number | null; backup_status: "pending" | "completed" | "failed";
  failure: { code: string; summary: string } | null; project_id: string | null; started_at: string; updated_at: string;
}
export interface MigrationReport {
  plan_version: number; plan_hash: string; project: { project_id: string; name: string };
  discovery: { legacy_weekly_detected: boolean; existing_recordings_scanned: boolean; trigger_mode: "manual" | "scheduled"; schedule_mode: "daily" | "interval" | null; daily_time: string | null; interval_minutes: number | null };
  imported: number; compatibility: number; quarantined: number; safe_results: number; history_total: number;
  quarantine_reason_codes: string[]; backup_created: boolean; readiness: "ready" | "attention";
  blocker_count: number; blocker_codes: string[]; completed_at: string; acknowledged_at: string | null;
}
export interface MigrationStartupSummary { entry: MigrationEntry; session: MigrationSession | null; report: MigrationReport | null }
export interface MigrationSnapshot extends MigrationStartupSummary {
  ok: true; source: { detected: true; checked_at: string; display_summary: { metadata_file_count: number; history_count?: number } };
  plan: MigrationPlan | null;
}
export interface MigrationInspectPayload { ok: true; source: MigrationSnapshot["source"]; plan: MigrationPlan }
export interface MigrationPlanPayload { ok: true; plan: MigrationPlan }
export interface MigrationSessionPayload { ok: true; session: MigrationSession }
export interface MigrationAcknowledgePayload extends MigrationSessionPayload { project_id: string }
export interface OnboardingSnapshot {
  ok: true; entry: { mode: OnboardingEntryMode; onboarding: OnboardingEntryState; reason_code: string | null; evidence_codes: string[] };
  session: OnboardingSession | null; environment: OnboardingEnvironment; resources: { asr: OnboardingResourceSummary; ai: OnboardingResourceSummary };
  model_catalog: OnboardingModel[]; initial_local_model: string; provider_presets: OnboardingProviderPreset[];
  suggestions: { project_name: string; output_directory: string };
  migration?: MigrationStartupSummary | null;
}
export interface OnboardingSessionPayload { ok: true; session: OnboardingSession; reused?: boolean }
export interface OnboardingValidationPayload extends ValidationPayload {
  checks: { asr: { ready: boolean }; ai: { ready: boolean }; source_directory: { status: string }; output_directory: { status: string } };
  summary: { recording_source: string; discovery: string; processing: string; output: string }; existing_video_count: number;
}
export interface OnboardingFinishPayload { ok: true; project?: ProjectSummary; session: OnboardingSession; reused?: boolean }
export interface ModelJob { id: string; status: string; bytes_downloaded?: number; bytes_total?: number; error?: string; message?: string }
