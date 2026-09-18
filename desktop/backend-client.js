const http = require("http");

function nodeTransport(options, body) {
  return new Promise((resolve, reject) => {
    const request = http.request(options, (response) => {
      let payload = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { payload += chunk; });
      response.on("error", reject);
      response.on("end", () => resolve({ statusCode: response.statusCode || 0, body: payload }));
    });
    request.on("error", reject);
    request.on("timeout", () => {
      request.destroy(Object.assign(new Error("后台服务响应超时。"), { code: "ETIMEDOUT" }));
    });
    if (body) request.write(body);
    request.end();
  });
}

function redactText(value, secrets = []) {
  return secrets.reduce((text, secret) => secret ? text.split(String(secret)).join("[REDACTED]") : text, String(value));
}

const STARTUP_MODES = new Set(["onboarding", "workbench", "migration_required", "diagnostic_required"]);
const ONBOARDING_MODES = new Set(["new", "resume", "paused", "activation_pending"]);

function requireOnboardingSnapshot(payload) {
  const entry = payload?.entry;
  const mode = entry?.mode;
  const onboarding = entry?.onboarding;
  const validMode = payload?.ok === true && STARTUP_MODES.has(mode);
  const validOnboarding = mode === "onboarding"
    ? ONBOARDING_MODES.has(onboarding)
    : onboarding === null;
  if (!validMode || !validOnboarding) {
    throw new Error("无法读取启动状态，请重新打开 Venus。");
  }
  return payload;
}

const BUSINESS_ERRORS = new Set([
  "validation_failed", "revision_conflict", "request_id_conflict", "project_not_found", "run_not_found", "output_not_found", "output_unavailable",
  "source_unavailable", "resource_unavailable", "service_not_ready", "migration_required", "migration_not_found", "backup_not_available", "diagnostic_required",
]);
class BackendError extends Error {
  constructor(message, { code, status = 0, apiPath, method, fields = {}, transportCode } = {}) {
    super(message); this.name = "BackendError"; this.code = code; this.status = status; this.fields = fields;
    this.outcomeUnknown = method !== "GET" && (status === 0 || status === 408 || status >= 500 || code === "invalid_response");
    this.diagnostic = { apiPath, status, code, transportCode };
  }
}

class BackendClient {
  constructor({ port, token, host = "127.0.0.1", transport = nodeTransport }) {
    this.host = host;
    this.port = port;
    this.token = token;
    this.transport = transport;
  }

  async request(apiPath, { method = "GET", body = null, timeoutMs = 3000 } = {}) {
    const serialized = body === null ? null : JSON.stringify(body);
    const headers = { Authorization: `Bearer ${this.token}`, Accept: "application/json" };
    if (serialized !== null) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(serialized);
    }
    let response;
    try {
      response = await this.transport({ host: this.host, port: this.port, path: apiPath, method, timeout: timeoutMs, headers }, serialized);
    } catch (error) {
      const timeout = error?.code === "ETIMEDOUT";
      const transportCode = ["ETIMEDOUT", "ECONNREFUSED", "ECONNRESET", "EPIPE"].includes(error?.code) ? error.code : "connection_error";
      throw new BackendError(timeout ? "后台服务响应超时。" : "无法连接后台服务。", { code: timeout ? "timeout_error" : "network_error", apiPath, method, transportCode });
    }
    let payload;
    try {
      payload = JSON.parse(response.body);
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("invalid envelope");
    } catch {
      throw new BackendError("无法读取后台服务返回的结果。", { code: "invalid_response", status: response.statusCode, apiPath, method });
    }
    if (response.statusCode < 200 || response.statusCode >= 300 || payload.ok === false) {
      const code = BUSINESS_ERRORS.has(payload.error?.code ?? payload.error_code) ? (payload.error?.code ?? payload.error_code) : "unknown_error";
      const message = code !== "unknown_error" && typeof (payload.error?.message ?? payload.message) === "string"
        ? redactText(payload.error?.message ?? payload.message, [this.token]) : "后台服务未能完成此请求。";
      const rawFields = code !== "unknown_error" ? payload.error?.fields : null;
      const fields = rawFields && typeof rawFields === "object" && !Array.isArray(rawFields)
        ? Object.fromEntries(Object.entries(rawFields).filter(([, value]) => typeof value === "string").map(([key, value]) => [key, redactText(value, [this.token])])) : {};
      throw new BackendError(message, { code, status: response.statusCode, apiPath, method, fields });
    }
    return payload;
  }

  getStudio() {
    return this.request("/api/studio");
  }

  resolveOutputPath(outputId) {
    return this.request(`/api/desktop/outputs/${encodeURIComponent(outputId)}/path`);
  }

  registerFileSelection(issueId, kind, selectedPath) {
    return this.request("/api/desktop/file-selections", {
      method: "POST",
      body: { issue_id: issueId, kind, selected_path: selectedPath },
    });
  }

  getMigrationBackupGrant(migrationId) {
    return this.request(`/api/migration/${encodeURIComponent(migrationId)}/backup-grant`);
  }

  stopService(timeoutMs = 3000) {
    return this.request("/api/service/stop", { method: "POST", body: {}, timeoutMs });
  }

  async checkReady() {
    const snapshot = requireOnboardingSnapshot(await this.request("/api/onboarding", { timeoutMs: 1000 }));
    if (
      snapshot.entry.mode === "workbench"
      && snapshot.entry.reason_code !== "migration_completed_unacknowledged"
    ) {
      await this.getStudio();
    }
    return snapshot;
  }

  async waitUntilReady({ timeoutMs = 30000, pollMs = 250, isAlive = () => true, sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)), now = () => Date.now() } = {}) {
    const started = now();
    let lastError = null;
    while (now() - started <= timeoutMs) {
      if (!isAlive()) throw new BackendError("后台服务在启动时停止，请重新打开 Venus。", { code: "startup_stopped", method: "GET" });
      try {
        return await this.checkReady();
      } catch (error) {
        lastError = error;
      }
      await sleep(pollMs);
    }
    const error = new BackendError(lastError ? "后台服务启动超时，请重新打开 Venus。" : "后台服务暂时不可用，请稍后重试。", { code: "startup_timeout", method: "GET" });
    error.diagnostic.lastFailure = lastError instanceof BackendError ? lastError.diagnostic : { code: "invalid_startup_state" };
    throw error;
  }
}

module.exports = { BackendError, BackendClient, nodeTransport, redactText, requireOnboardingSnapshot };
