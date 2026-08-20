export type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly fields: Record<string, string>;

  constructor(message: string, status = 0, code?: string, fields: Record<string, string> = {}) {
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
    throw new ApiError("网络连接失败");
  }

  let payload: JsonObject;
  try {
    payload = (await response.json()) as JsonObject;
  } catch {
    throw new ApiError("服务返回了无法读取的响应", response.status);
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
    throw new ApiError(String(message), response.status, String(nested?.code ?? payload.error_code ?? "") || undefined, fields);
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
