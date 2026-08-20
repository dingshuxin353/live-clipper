export type JsonObject = Record<string, unknown>;

export type ApiErrorCode =
  | "network_error"
  | "invalid_response"
  | "unknown_error"
  | "validation_failed"
  | "migration_required"
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
  | "initial_scan_failed";

const API_ERROR_CODES: ReadonlySet<ApiErrorCode> = new Set([
  "network_error", "invalid_response", "unknown_error", "validation_failed", "migration_required",
  "route_not_found", "project_not_found", "run_not_found", "request_id_conflict", "data_integrity_error",
  "revision_conflict", "project_not_ready", "scan_in_progress", "source_unavailable",
  "source_path_outside_project", "resource_unavailable", "initial_scan_failed",
]);

function apiErrorCode(value: unknown): ApiErrorCode {
  const candidate = String(value ?? "");
  return API_ERROR_CODES.has(candidate as ApiErrorCode) ? candidate as ApiErrorCode : "unknown_error";
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly fields: Record<string, string>;

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
    throw new ApiError("网络连接失败", 0, "network_error");
  }

  let payload: JsonObject;
  try {
    payload = (await response.json()) as JsonObject;
  } catch {
    throw new ApiError("服务返回了无法读取的响应", response.status, "invalid_response");
  }
  if (!response.ok || payload.ok === false) {
    const nested = typeof payload.error === "object" && payload.error
      ? payload.error as Record<string, unknown>
      : null;
    const message = nested?.message ?? payload.message ?? (typeof payload.error === "string" ? payload.error : "请求失败");
    const rawFields = nested?.fields;
    const fields = typeof rawFields === "object" && rawFields
      ? Object.fromEntries(Object.entries(rawFields as Record<string, unknown>).map(([key, value]) => [key, String(value)]))
      : {};
    throw new ApiError(String(message), response.status, apiErrorCode(nested?.code ?? payload.error_code), fields);
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
