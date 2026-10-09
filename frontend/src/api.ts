export type JsonObject = Record<string, unknown>;

export type ApiErrorCode =
  | "model_directory_unwritable"
  | "model_integrity_failed"
  | "job_not_found"
  | "unauthorized"
  | "selection_token_invalid"
  | "selection_token_expired"
  | "selection_token_already_used"
  | "source_identity_mismatch"
  | "issue_not_found"
  | "issue_not_ready"
  | "issue_group_not_found"
  | "resource_not_repairable"
  | "output_not_retryable"
  | "material_not_retryable"
  | "preflight_changed"
  | "reprocess_blocked"
  | "output_not_found"
  | "material_not_found"
  | "review_reprocess_required"
  | "migration_inspection_failed"
  | "migration_apply_failed"
  | "migration_running"
  | "migration_completion_conflict"
  | "migration_backup_invalid"
  | "migration_resource_conflict"
  | "migration_credential_source_unknown"
  | "migration_resources_failed"
  | "migration_evidence_conflict"
  | "migration_result_unknown"
  | "backup_not_available"
  | "validation_result_unknown"
  | "timeout_error"
  | "network_error"
  | "invalid_response"
  | "unknown_error"
  | "validation_failed"
  | "migration_required"
  | "migration_source_changed"
  | "migration_plan_changed"
  | "migration_space_insufficient"
  | "migration_choices_required"
  | "migration_conflict"
  | "migration_not_found"
  | "migration_interrupted"
  | "diagnostic_required"
  | "route_not_found"
  | "project_not_found"
  | "run_not_found"
  | "request_id_conflict"
  | "data_integrity_error"
  | "revision_conflict"
  | "project_not_ready"
  | "scan_in_progress"
  | "source_unavailable"
  | "source_path_outside_project"
  | "resource_unavailable"
  | "initial_scan_failed"
  | "onboarding_not_started"
  | "onboarding_state_conflict"
  | "onboarding_revision_conflict"
  | "onboarding_contract_replaced"
  | "environment_not_ready"
  | "model_not_ready"
  | "model_download_active"
  | "model_download_unavailable"
  | "insufficient_disk_space"
  | "asr_request_failed"
  | "asr_unreachable"
  | "asr_auth_failed"
  | "asr_model_unavailable"
  | "ai_request_failed"
  | "ai_unreachable"
  | "ai_auth_failed"
  | "ai_model_unavailable"
  | "resource_commit_failed"
  | "project_validation_failed"
  | "project_creation_uncertain"
  | "service_not_ready"
  | "resource_not_found"
  | "resource_deleted"
  | "request_conflict"
  | "validation_changed"
  | "validation_required"
  | "referenced_capability_required"
  | "required_connection_fields"
  | "resource_in_use"
  | "failed_runs_confirmation_required"
  | "original_configuration_unknown"
  | "same_identity_confirmation_required"
  | "workspace_required"
  | "region_required"
  | "custom_endpoint_requires_custom_provider"
  | "invalid_resource"
  | "invalid_fields"
  | "migration_pending"
  | "migration_source_unknown"
  | "incompatible_resource"
  | "request_id_required";

const API_ERROR_CODES: ReadonlySet<ApiErrorCode> = new Set([
  "model_directory_unwritable", "model_integrity_failed", "job_not_found", "unauthorized",
  "migration_inspection_failed", "migration_apply_failed", "backup_not_available",
  "migration_running", "migration_completion_conflict", "migration_backup_invalid", "migration_resource_conflict", "migration_credential_source_unknown", "migration_resources_failed", "migration_evidence_conflict", "migration_result_unknown",
  "selection_token_invalid", "selection_token_expired", "selection_token_already_used", "source_identity_mismatch", "issue_not_found", "issue_not_ready", "issue_group_not_found", "resource_not_repairable", "output_not_retryable", "material_not_retryable", "preflight_changed", "reprocess_blocked", "output_not_found", "material_not_found", "review_reprocess_required",
  "resource_not_found", "resource_deleted", "request_conflict", "validation_changed", "validation_required", "referenced_capability_required", "required_connection_fields", "resource_in_use", "failed_runs_confirmation_required", "original_configuration_unknown", "same_identity_confirmation_required", "workspace_required", "region_required", "custom_endpoint_requires_custom_provider", "invalid_resource", "invalid_fields", "migration_pending", "migration_source_unknown", "incompatible_resource", "request_id_required",
  "timeout_error", "validation_result_unknown", "network_error", "invalid_response", "unknown_error", "validation_failed", "migration_required",
  "migration_source_changed", "migration_plan_changed", "migration_space_insufficient",
  "migration_choices_required", "migration_conflict", "migration_not_found", "migration_interrupted",
  "diagnostic_required",
  "route_not_found", "project_not_found", "run_not_found", "request_id_conflict", "data_integrity_error",
  "revision_conflict", "project_not_ready", "scan_in_progress", "source_unavailable",
  "source_path_outside_project", "resource_unavailable", "initial_scan_failed",
  "onboarding_not_started", "onboarding_state_conflict", "onboarding_revision_conflict",
  "onboarding_contract_replaced", "environment_not_ready", "model_not_ready", "model_download_active",
  "model_download_unavailable", "insufficient_disk_space", "asr_request_failed", "asr_unreachable",
  "asr_auth_failed", "asr_model_unavailable", "ai_request_failed", "ai_unreachable", "ai_auth_failed",
  "ai_model_unavailable", "resource_commit_failed", "project_validation_failed", "project_creation_uncertain",
  "service_not_ready",
]);

function apiErrorCode(value: unknown): ApiErrorCode {
  const candidate = String(value ?? "");
  return API_ERROR_CODES.has(candidate as ApiErrorCode) ? candidate as ApiErrorCode : "unknown_error";
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly fields: Record<string, string>;

  // A failed response does not prove that a write was rejected.
  get outcomeUnknown(): boolean {
    return this.status === 0 || this.status === 408 || this.status >= 500
      || ["invalid_response", "validation_result_unknown", "project_creation_uncertain"].includes(this.code);
  }

  constructor(message: string, status = 0, code: ApiErrorCode = "unknown_error", fields: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      signal,
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    const timeout = error instanceof Error && error.name === "TimeoutError";
    throw new ApiError(timeout ? "后台服务响应超时" : "无法连接后台服务。", 0, timeout ? "timeout_error" : "network_error");
  }

  let payload: JsonObject;
  try {
    const value: unknown = await response.json();
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid envelope");
    payload = value as JsonObject;
  } catch {
    throw new ApiError("无法读取返回结果。", response.status, "invalid_response");
  }
  if (!response.ok || payload.ok === false) {
    const nested = typeof payload.error === "object" && payload.error
      ? payload.error as Record<string, unknown>
      : null;
    const code = apiErrorCode(nested?.code ?? payload.error_code);
    const message = code === "route_not_found" ? "暂时无法完成此操作。" : nested?.message ?? payload.message ?? (typeof payload.error === "string" ? payload.error : "请求失败");
    const rawFields = nested?.fields;
    const fields = typeof rawFields === "object" && rawFields
      ? Object.fromEntries(Object.entries(rawFields as Record<string, unknown>).map(([key, value]) => [key, String(value)]))
      : {};
    throw new ApiError(code === "unknown_error" ? "暂时无法完成此操作。" : String(message), response.status, code, code === "unknown_error" ? {} : fields);
  }
  return payload as T;
}

export function patch<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return api<T>(path, { method: "PATCH", body: JSON.stringify(body) }, signal);
}

export function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return api<T>(
    path,
    {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    signal,
  );
}
