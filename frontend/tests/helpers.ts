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
