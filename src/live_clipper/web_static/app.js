const state = {
  activeTab: "service",
  phase: "",
  runs: [],
  selectedRunId: null,
  detail: null,
  confirmations: [],
  events: [],
};

const el = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || payload.error || "request failed");
  }
  return payload;
}

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => {
    node.hidden = true;
  }, 3600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".section").forEach((section) => {
    section.classList.toggle("active", section.id === `section-${tab}`);
  });
}

function renderMetrics(status) {
  const metrics = [
    ["status", status.service?.status || "stopped"],
    ["pid", status.service?.pid || "none"],
    ["active", status.active_run || "none"],
    ["needs review", String(status.pending_review_runs?.length || 0)],
    ["failed", String(status.failed_runs?.length || 0)],
    ["confirmations", String(status.pending_confirmation_count || 0)],
  ];
  el("serviceMetrics").innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
  const source = status.source_summary || {};
  el("serviceSummary").innerHTML = [
    ["running", status.running ? "yes" : "no"],
    ["last heartbeat", status.service?.last_heartbeat_at || "-"],
    ["next scan", status.service?.next_scan_at || "-"],
    ["source", source.source_dir || "-"],
    ["input", source.input_dir || "-"],
    ["output", source.output_root || "-"],
    ["last error", status.service?.last_error || "-"],
  ]
    .map(([label, value]) => `<div class="info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
}

function renderRuns() {
  const list = el("runList");
  if (!state.runs.length) {
    list.innerHTML = '<div class="empty">No service runs yet.</div>';
    return;
  }
  list.innerHTML = state.runs
    .map((run) => `
      <button class="run-item ${run.run_id === state.selectedRunId ? "active" : ""}" data-run-id="${escapeHtml(run.run_id)}">
        <span class="run-title">${escapeHtml(run.source_name || run.run_id)}</span>
        <span class="status-pill ${escapeHtml(run.phase || "unknown")}">${escapeHtml(run.phase || "unknown")}</span>
        <small>${escapeHtml(run.run_id)}</small>
        <small>${escapeHtml(run.updated_at || "-")}</small>
      </button>
    `)
    .join("");
}

function renderRunDetail() {
  const detail = state.detail;
  if (!detail || !detail.ok) {
    el("runDetail").innerHTML = '<div class="empty">Select a run.</div>';
    el("renderRunBtn").disabled = true;
    el("cleanupPreviewBtn").disabled = true;
    return;
  }
  const run = detail.run;
  const files = detail.files || {};
  const cleanup = detail.cleanup || { targets: [] };
  el("runDetail").innerHTML = `
    <div class="info-grid">
      ${infoRow("run", run.run_id)}
      ${infoRow("phase", run.phase)}
      ${infoRow("source", run.source_path)}
      ${infoRow("local", run.local_source_path)}
      ${infoRow("run dir", run.run_dir)}
      ${infoRow("candidates", run.candidate_count || detail.candidates_count || 0)}
      ${infoRow("selected", run.selected_count || detail.selected_count || 0)}
      ${infoRow("clips", run.clip_count || detail.rendered_clip_count || 0)}
    </div>
    <h3>Files</h3>
    <div class="file-grid">
      ${Object.entries(files).map(([name, file]) => `<div class="file-row"><strong>${escapeHtml(name)}</strong><small>${file.exists ? escapeHtml(file.path) : "missing"}</small></div>`).join("")}
    </div>
    <h3>Cleanup Preview</h3>
    <div class="cleanup-list">
      ${(cleanup.targets || []).map((target) => `
        <div class="cleanup-row ${target.deletable ? "deletable" : "protected"}">
          <strong>${target.deletable ? "deletable" : "protected"} · ${escapeHtml(target.kind)}</strong>
          <small>${formatBytes(target.bytes)} · ${escapeHtml(target.path)}</small>
          <small>${escapeHtml(target.reason)}</small>
        </div>
      `).join("") || '<div class="empty">No cleanup targets.</div>'}
    </div>
  `;
  el("renderRunBtn").disabled = !detail.actions?.can_render;
  el("cleanupPreviewBtn").disabled = !detail.actions?.can_cleanup_preview;
  el("logOutput").textContent = detail.log?.log || detail.log?.tail || "No run log.";
}

function infoRow(label, value) {
  return `<div class="info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "-")}</strong></div>`;
}

function renderConfirmations() {
  const pending = state.confirmations.filter((item) => item.status === "pending");
  if (!pending.length) {
    el("confirmationList").innerHTML = '<div class="empty">No pending confirmations.</div>';
    return;
  }
  el("confirmationList").innerHTML = pending
    .map((item) => `
      <div class="confirmation-row">
        <input type="checkbox" data-confirmation-check="${escapeHtml(item.id)}" />
        <div>
          <strong>${escapeHtml(item.action)}</strong>
          <small>${escapeHtml(item.id)} · run ${escapeHtml(item.run_id)}</small>
          <small>${escapeHtml(item.target_path)}</small>
        </div>
        <span class="risk ${escapeHtml(item.risk_level)}">${escapeHtml(item.risk_level)}</span>
        <div class="button-row">
          <button class="danger-button small" data-approve="${escapeHtml(item.id)}">Approve</button>
          <button class="secondary-button small" data-reject="${escapeHtml(item.id)}">Reject</button>
        </div>
      </div>
    `)
    .join("");
}

function renderEvents() {
  el("eventStream").innerHTML = state.events
    .map((event) => `<div class="event-row"><strong>${escapeHtml(event.type)}</strong><small>${escapeHtml(event.created_at)} · ${escapeHtml(event.run_id || "-")}</small></div>`)
    .join("") || '<div class="empty">No events.</div>';
}

function renderSettings(settings) {
  const service = settings.service || {};
  const source = settings.recording_source || {};
  const web = settings.web || {};
  el("settingsView").innerHTML = [
    ["scan interval", `${service.scan_interval_minutes || "-"} min`],
    ["auto render", service.auto_render_after_selection ? "true" : "false"],
    ["cleanup mode", service.cleanup_mode || "-"],
    ["source dir", source.source_dir || "-"],
    ["input dir", source.input_dir || "-"],
    ["output root", source.output_root || "-"],
    ["web bind", `${web.host || "127.0.0.1"}:${web.port || 8765}`],
  ]
    .map(([label, value]) => infoRow(label, value))
    .join("");
}

async function loadService() {
  renderMetrics(await api("/api/service"));
}

async function loadRuns() {
  const suffix = state.phase ? `?phase=${encodeURIComponent(state.phase)}` : "";
  const payload = await api(`/api/runs${suffix}`);
  state.runs = payload.runs || [];
  if (!state.selectedRunId || !state.runs.some((run) => run.run_id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.run_id || null;
  }
  renderRuns();
  if (state.selectedRunId) await loadRunDetail(state.selectedRunId);
  else renderRunDetail();
}

async function loadRunDetail(runId) {
  state.selectedRunId = runId;
  state.detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
  renderRuns();
  renderRunDetail();
}

async function loadConfirmations() {
  const payload = await api("/api/confirmations");
  state.confirmations = payload.confirmations || [];
  renderConfirmations();
}

async function loadEvents() {
  const payload = await api("/api/events");
  state.events = payload.events || [];
  renderEvents();
}

async function loadSettings() {
  const payload = await api("/api/settings");
  renderSettings(payload.settings || {});
}

async function refreshAll() {
  await Promise.all([loadService(), loadRuns(), loadConfirmations(), loadEvents(), loadSettings()]);
}

function selectedConfirmationIds() {
  return [...document.querySelectorAll("[data-confirmation-check]:checked")].map((node) => node.dataset.confirmationCheck);
}

async function post(path, payload) {
  const options = { method: "POST" };
  if (payload !== undefined) options.body = JSON.stringify(payload);
  const result = await api(path, options);
  if (result.status === "confirmation_required") {
    el("logOutput").textContent = `confirmation_required: ${result.confirmation_id}`;
  }
  await refreshAll();
  return result;
}

document.addEventListener("click", async (event) => {
  const tab = event.target.closest("[data-tab]");
  const run = event.target.closest("[data-run-id]");
  try {
    if (tab) switchTab(tab.dataset.tab);
    if (run) await loadRunDetail(run.dataset.runId);
    if (event.target.id === "refreshBtn") await refreshAll();
    if (event.target.id === "startServiceBtn") await post("/api/service/start");
    if (event.target.id === "stopServiceBtn") await post("/api/service/stop");
    if (event.target.id === "scanNowBtn") await post("/api/service/scan-now");
    if (event.target.id === "renderRunBtn" && state.selectedRunId) {
      await post(`/api/runs/${encodeURIComponent(state.selectedRunId)}/render`);
    }
    if (event.target.id === "cleanupPreviewBtn" && state.selectedRunId) {
      const result = await post(`/api/runs/${encodeURIComponent(state.selectedRunId)}/cleanup-preview`);
      el("logOutput").textContent = JSON.stringify(result, null, 2);
    }
    if (event.target.dataset.approve) await post(`/api/confirmations/${event.target.dataset.approve}/approve`);
    if (event.target.dataset.reject) await post(`/api/confirmations/${event.target.dataset.reject}/reject`, { reason: "Rejected in Web Console" });
    if (event.target.id === "batchApproveBtn") {
      const ids = selectedConfirmationIds();
      await post("/api/confirmations/batch-approve", { ids });
      el("logOutput").textContent = `batch approve: ${ids.join(", ") || "none"}`;
    }
    if (event.target.id === "batchRejectBtn") {
      const ids = selectedConfirmationIds();
      await post("/api/confirmations/batch-reject", { ids, reason: "Rejected in Web Console" });
      el("logOutput").textContent = `batch reject: ${ids.join(", ") || "none"}`;
    }
    if (event.target.id === "clearLogBtn") el("logOutput").textContent = "";
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("click", (event) => {
  const phaseButton = event.target.closest("[data-phase]");
  if (!phaseButton) return;
  state.phase = phaseButton.dataset.phase;
  document.querySelectorAll("[data-phase]").forEach((button) => button.classList.toggle("active", button === phaseButton));
  loadRuns().catch((error) => toast(error.message));
});

refreshAll().catch((error) => toast(error.message));
setInterval(() => refreshAll().catch(() => {}), 15000);
