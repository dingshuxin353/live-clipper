export type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
    const message = payload.message ?? payload.error ?? "请求失败";
    throw new ApiError(String(message), response.status);
  }
  return payload as T;
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
