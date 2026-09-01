const http = require("http");

function nodeTransport(options, body) {
  return new Promise((resolve, reject) => {
    const request = http.request(options, (response) => {
      let payload = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { payload += chunk; });
      response.on("end", () => resolve({ statusCode: response.statusCode || 0, body: payload }));
    });
    request.on("error", reject);
    request.on("timeout", () => {
      request.destroy(new Error("后台请求超时"));
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
    throw new Error("后台启动状态无效");
  }
  return payload;
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
    } catch (_error) {
      throw new Error(`后台请求失败（${apiPath}）`);
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw new Error(`后台请求失败（${apiPath}，HTTP ${response.statusCode}）`);
    }
    try {
      return response.body ? JSON.parse(response.body) : {};
    } catch (_error) {
      throw new Error(`后台响应无法读取（${apiPath}）`);
    }
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
      if (!isAlive()) throw new Error("后台服务在启动期间退出");
      try {
        return await this.checkReady();
      } catch (error) {
        lastError = error;
      }
      await sleep(pollMs);
    }
    throw new Error(lastError ? "后台服务启动超时" : "后台服务未就绪");
  }
}

module.exports = { BackendClient, nodeTransport, redactText, requireOnboardingSnapshot };
