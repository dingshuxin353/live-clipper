const state = {
  activeTab: "service",
  phase: "",
  runs: [],
  selectedRunId: null,
  detail: null,
  confirmations: [],
  events: [],
  config: null,
  configDirty: false,
  scheduler: null,
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
    throw new Error(payload.message || payload.error || "请求失败");
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

const phaseLabels = {
  processing: "处理中",
  needs_review: "待审阅",
  rendering: "渲染中",
  rendered: "已成片",
  failed: "失败",
  cleanup_ready: "可清理",
  ready_to_render: "可渲染",
  needs_codex_selection: "待选片",
  running: "运行中",
  waiting_or_manual: "等待处理",
  missing: "缺失",
  unknown: "未知",
};

const valueLabels = {
  running: "运行中",
  stopped: "已停止",
  stale: "已失联",
  error: "异常",
  pending: "待确认",
  approved_executed: "已确认执行",
  rejected: "已拒绝",
  delete_clip: "删除成片",
  cleanup_confirm: "执行清理",
  delete_local_source: "删除本地源文件",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  scan_recordings: "扫描录播",
  review_due_check: "审阅检查",
  maintenance_check: "维护检查",
  weekly: "每周",
  daily: "每天",
  interval_minutes: "每隔 N 分钟",
  success: "成功",
  skipped: "已跳过",
};

const cleanupKindLabels = {
  audio: "中间音频",
  local_source_video: "本地源视频",
};

function labelFor(value) {
  return valueLabels[value] || phaseLabels[value] || value || "-";
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
    ["状态", labelFor(status.service?.status || "stopped")],
    ["PID", status.service?.pid || "无"],
    ["当前任务", status.active_run || "无"],
    ["待审阅", String(status.pending_review_runs?.length || 0)],
    ["失败", String(status.failed_runs?.length || 0)],
    ["待确认", String(status.pending_confirmation_count || 0)],
  ];
  el("serviceMetrics").innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
  const source = status.source_summary || {};
  el("serviceSummary").innerHTML = [
    ["运行中", status.running ? "是" : "否"],
    ["最近心跳", status.service?.last_heartbeat_at || "-"],
    ["下次扫描", status.service?.next_scan_at || "-"],
    ["录播源", source.source_dir || "-"],
    ["输入目录", source.input_dir || "-"],
    ["输出目录", source.output_root || "-"],
    ["最近错误", status.service?.last_error || "-"],
  ]
    .map(([label, value]) => `<div class="info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
}

function renderRuns() {
  const list = el("runList");
  if (!state.runs.length) {
    list.innerHTML = '<div class="empty">还没有服务任务。</div>';
    return;
  }
  list.innerHTML = state.runs
    .map((run) => `
      <button class="run-item ${run.run_id === state.selectedRunId ? "active" : ""}" data-run-id="${escapeHtml(run.run_id)}">
        <span class="run-title">${escapeHtml(run.source_name || run.run_id)}</span>
        <span class="status-pill ${escapeHtml(run.phase || "unknown")}">${escapeHtml(labelFor(run.phase || "unknown"))}</span>
        <small>${escapeHtml(run.run_id)}</small>
        <small>${escapeHtml(run.updated_at || "-")}</small>
      </button>
    `)
    .join("");
}

function renderRunDetail() {
  const detail = state.detail;
  if (!detail || !detail.ok) {
    el("runDetail").innerHTML = '<div class="empty">请选择一个任务。</div>';
    el("renderRunBtn").disabled = true;
    el("cleanupPreviewBtn").disabled = true;
    return;
  }
  const run = detail.run;
  const files = detail.files || {};
  const cleanup = detail.cleanup || { targets: [] };
  el("runDetail").innerHTML = `
    <div class="info-grid">
      ${infoRow("任务", run.run_id)}
      ${infoRow("阶段", labelFor(run.phase))}
      ${infoRow("源文件", run.source_path)}
      ${infoRow("本地副本", run.local_source_path)}
      ${infoRow("任务目录", run.run_dir)}
      ${infoRow("候选数", run.candidate_count || detail.candidates_count || 0)}
      ${infoRow("已选片段", run.selected_count || detail.selected_count || 0)}
      ${infoRow("成片数", run.clip_count || detail.rendered_clip_count || 0)}
    </div>
    <h3>文件</h3>
    <div class="file-grid">
      ${Object.entries(files).map(([name, file]) => `<div class="file-row"><strong>${escapeHtml(name)}</strong><small>${file.exists ? escapeHtml(file.path) : "缺失"}</small></div>`).join("")}
    </div>
    <h3>清理预览</h3>
    <div class="cleanup-list">
      ${(cleanup.targets || []).map((target) => `
        <div class="cleanup-row ${target.deletable ? "deletable" : "protected"}">
          <strong>${target.deletable ? "可删除" : "受保护"} · ${escapeHtml(cleanupKindLabels[target.kind] || target.kind)}</strong>
          <small>${formatBytes(target.bytes)} · ${escapeHtml(target.path)}</small>
          <small>${escapeHtml(target.reason)}</small>
        </div>
      `).join("") || '<div class="empty">没有清理目标。</div>'}
    </div>
  `;
  el("renderRunBtn").disabled = !detail.actions?.can_render;
  el("cleanupPreviewBtn").disabled = !detail.actions?.can_cleanup_preview;
  el("logOutput").textContent = detail.log?.log || detail.log?.tail || "暂无任务日志。";
}

function infoRow(label, value) {
  return `<div class="info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "-")}</strong></div>`;
}

function renderConfirmations() {
  const pending = state.confirmations.filter((item) => item.status === "pending");
  if (!pending.length) {
    el("confirmationList").innerHTML = '<div class="empty">没有待确认请求。</div>';
    return;
  }
  el("confirmationList").innerHTML = pending
    .map((item) => `
      <div class="confirmation-row">
        <input type="checkbox" data-confirmation-check="${escapeHtml(item.id)}" />
        <div>
          <strong>${escapeHtml(labelFor(item.action))}</strong>
          <small>${escapeHtml(item.id)} · 任务 ${escapeHtml(item.run_id)}</small>
          <small>${escapeHtml(item.target_path)}</small>
        </div>
        <span class="risk ${escapeHtml(item.risk_level)}">${escapeHtml(labelFor(item.risk_level))}</span>
        <div class="button-row">
          <button class="danger-button small" data-approve="${escapeHtml(item.id)}">确认</button>
          <button class="secondary-button small" data-reject="${escapeHtml(item.id)}">拒绝</button>
        </div>
      </div>
    `)
    .join("");
}

function renderEvents() {
  el("eventStream").innerHTML = state.events
    .map((event) => `<div class="event-row"><strong>${escapeHtml(event.type)}</strong><small>${escapeHtml(event.created_at)} · ${escapeHtml(event.run_id || "-")}</small></div>`)
    .join("") || '<div class="empty">暂无事件。</div>';
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

async function loadConfig(force = false) {
  if (state.configDirty && !force) return;
  const payload = await api("/api/config");
  state.config = payload.config || {};
  state.configDirty = false;
  renderConfig(payload);
}

async function loadScheduler() {
  state.scheduler = await api("/api/scheduler");
  renderScheduler(state.scheduler);
}

async function refreshAll() {
  await Promise.all([loadService(), loadRuns(), loadConfirmations(), loadEvents(), loadConfig(), loadScheduler()]);
}

function renderConfig(payload) {
  const config = payload.config || {};
  el("configMeta").textContent = `${payload.config_path || "live-clipper.toml"} · ${payload.exists ? "已存在" : "尚未创建"} · ${payload.loaded_at || ""}`;
  document.querySelectorAll("[data-config-field]").forEach((field) => {
    const value = getConfigValue(config, field.dataset.configField);
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  renderEnvStatus(payload.env_status || {});
  renderConfigNotice(payload.warnings || [], "warning");
}

function renderScheduler(payload) {
  if (!payload?.ok) return;
  const scheduler = payload.scheduler || {};
  el("schedulerSummary").innerHTML = [
    ["Scheduler 状态", scheduler.enabled ? "运行中" : "未启用"],
    ["调度时区", scheduler.timezone || "-"],
    ["当前系统时间", scheduler.current_time || "-"],
    ["下一次任务", scheduler.next_due_job_id || "-"],
    ["下次执行", scheduler.next_due_at || "-"],
  ].map(([label, value]) => infoRow(label, value)).join("");
  el("schedulerJobList").innerHTML = (payload.jobs || []).map((job) => `
    <div class="scheduler-job">
      <div>
        <strong>${escapeHtml(job.name || job.id)}</strong>
        <small>${escapeHtml(job.id)} · ${escapeHtml(labelFor(job.type))} · ${escapeHtml(labelFor(job.schedule))}</small>
        <small>下次执行：${escapeHtml(job.next_run_at || "-")} · 上次结果：${escapeHtml(labelFor(job.status) || "-")}</small>
      </div>
      <div class="button-row">
        <button class="secondary-button small" data-scheduler-edit="${escapeHtml(job.id)}">编辑</button>
        <button class="primary-button small" data-scheduler-run="${escapeHtml(job.id)}">立即执行</button>
        <button class="secondary-button small" data-scheduler-pause="${escapeHtml(job.id)}">暂停</button>
        <button class="secondary-button small" data-scheduler-resume="${escapeHtml(job.id)}">启用</button>
      </div>
    </div>
  `).join("") || '<div class="empty">还没有定时任务。</div>';
}

function renderEnvStatus(envStatus) {
  const entries = Object.entries(envStatus);
  el("envStatus").innerHTML = entries.length
    ? entries.map(([name, configured]) => `
      <div class="env-row ${configured ? "ok" : "missing"}">
        <strong>${escapeHtml(name)}</strong>
        <span>${configured ? "已配置" : "未配置"}</span>
      </div>
    `).join("")
    : '<div class="empty">没有需要展示的 API key 环境变量。</div>';
}

function renderConfigNotice(items, type = "info") {
  const node = el("configNotice");
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    node.hidden = true;
    node.innerHTML = "";
    return;
  }
  node.hidden = false;
  node.className = `notice ${type}`;
  node.innerHTML = list.map((item) => `<div>${escapeHtml(item.message || item)}</div>`).join("");
}

function getConfigValue(config, field) {
  const [section, key] = field.split(".");
  return config?.[section]?.[key];
}

function setConfigValue(config, field, value) {
  const [section, key] = field.split(".");
  config[section] ||= {};
  config[section][key] = value;
}

function collectConfigForm() {
  const draft = structuredClone(state.config || {});
  document.querySelectorAll("[data-config-field]").forEach((field) => {
    let value;
    if (field.type === "checkbox") value = field.checked;
    else if (field.type === "number") value = field.value === "" ? "" : Number(field.value);
    else value = field.value;
    setConfigValue(draft, field.dataset.configField, value);
  });
  return draft;
}

function defaultConfig() {
  return {
    paths: { input_dir: "input", output_root: "output", work_dir: "work", glossary_path: "glossary/common_terms.json" },
    recording_source_default: {
      source_dir: "",
      input_dir: "input",
      output_root: "output",
      since_hours: 168,
      min_age_minutes: 10,
      stable_check_seconds: 60,
    },
    llm: {
      provider_label: "OpenAI-compatible LLM",
      api_base: "https://apihub.agnes-ai.com/v1",
      api_key_env: "CHEAP_MODEL_API_KEY",
      model: "agnes-2.0-flash",
      timeout_seconds: 300,
      request_attempts: 5,
      retry_delay_seconds: 3.0,
    },
    asr: {
      backend: "mlx_whisper",
      model: "mlx-community/whisper-large-v3-turbo",
      language: "zh",
      api_base: "https://api.openai.com/v1",
      api_key_env: "ASR_API_KEY",
      hf_token_env: "HF_TOKEN",
    },
    service: { enabled: true, scan_interval_minutes: 30, auto_render_after_selection: true, cleanup_mode: "preview_only" },
    scheduler: { enabled: true, timezone: "Asia/Shanghai", tick_seconds: 30, missed_policy: "run_once", state_dir: "work/service" },
    scheduler_jobs: [
      {
        id: "weekly_recording_scan",
        name: "每周录播扫描",
        enabled: true,
        type: "scan_recordings",
        schedule: "weekly",
        day_of_week: "sun",
        time: "00:00",
        skip_if_running: true,
      },
      {
        id: "weekly_review_due",
        name: "每周审阅检查",
        enabled: true,
        type: "review_due_check",
        schedule: "weekly",
        day_of_week: "sun",
        time: "12:00",
        skip_if_running: true,
      },
    ],
    web: { host: "127.0.0.1", port: 8765 },
  };
}

function schedulerJobFromForm() {
  const schedule = el("schedulerJobSchedule").value;
  const job = {
    id: el("schedulerJobId").value.trim(),
    name: el("schedulerJobName").value.trim(),
    enabled: el("schedulerJobEnabled").checked,
    type: el("schedulerJobType").value,
    schedule,
    skip_if_running: el("schedulerJobSkip").checked,
  };
  if (schedule === "weekly") job.day_of_week = el("schedulerJobDay").value;
  if (schedule === "weekly" || schedule === "daily") job.time = el("schedulerJobTime").value.trim();
  if (schedule === "interval_minutes") job.interval_minutes = Number(el("schedulerJobInterval").value);
  return job;
}

function fillSchedulerJobForm(job) {
  el("schedulerJobId").value = job.id || "";
  el("schedulerJobName").value = job.name || "";
  el("schedulerJobEnabled").checked = Boolean(job.enabled);
  el("schedulerJobType").value = job.type || "scan_recordings";
  el("schedulerJobSchedule").value = job.schedule || "weekly";
  el("schedulerJobDay").value = job.day_of_week || "sun";
  el("schedulerJobTime").value = job.time || "00:00";
  el("schedulerJobInterval").value = job.interval_minutes || 60;
  el("schedulerJobSkip").checked = job.skip_if_running !== false;
}

async function validateConfig() {
  const result = await api("/api/config/validate", { method: "POST", body: JSON.stringify({ config: collectConfigForm() }) });
  const messages = result.ok
    ? [{ message: result.warnings?.length ? "配置检查通过，但有提醒：" : "配置检查通过。" }, ...(result.warnings || [])]
    : result.errors;
  renderConfigNotice(messages, result.ok ? "success" : "error");
  return result;
}

async function saveConfig() {
  const result = await api("/api/config", { method: "POST", body: JSON.stringify({ config: collectConfigForm() }) });
  const messages = [
    { message: result.message || "配置已保存。" },
    { message: `备份文件：${result.backup_path || "无"}` },
  ];
  if (result.requires_web_restart) messages.push({ message: "Web host/port 已变化，需要手动重启 Web 控制台。" });
  renderConfigNotice(messages, "success");
  await loadConfig();
  return result;
}

function selectedConfirmationIds() {
  return [...document.querySelectorAll("[data-confirmation-check]:checked")].map((node) => node.dataset.confirmationCheck);
}

async function post(path, payload) {
  const options = { method: "POST" };
  if (payload !== undefined) options.body = JSON.stringify(payload);
  const result = await api(path, options);
  if (result.status === "confirmation_required") {
    el("logOutput").textContent = `需要确认: ${result.confirmation_id}`;
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
    if (event.target.dataset.reject) await post(`/api/confirmations/${event.target.dataset.reject}/reject`, { reason: "在 Web 控制台拒绝" });
    if (event.target.id === "batchApproveBtn") {
      const ids = selectedConfirmationIds();
      await post("/api/confirmations/batch-approve", { ids });
      el("logOutput").textContent = `批量确认: ${ids.join(", ") || "无"}`;
    }
    if (event.target.id === "batchRejectBtn") {
      const ids = selectedConfirmationIds();
      await post("/api/confirmations/batch-reject", { ids, reason: "在 Web 控制台批量拒绝" });
      el("logOutput").textContent = `批量拒绝: ${ids.join(", ") || "无"}`;
    }
    if (event.target.id === "validateConfigBtn") await validateConfig();
    if (event.target.id === "saveConfigBtn") await saveConfig();
    if (event.target.id === "reloadConfigBtn") {
      await loadConfig(true);
      toast("已重新读取配置文件");
    }
    if (event.target.id === "resetConfigBtn") {
      if (window.confirm("恢复默认只会修改当前表单，保存前不会写入文件。继续吗？")) {
        state.config = defaultConfig();
        state.configDirty = true;
        renderConfig({ config: state.config, config_path: "live-clipper.toml", exists: true, env_status: {}, warnings: [] });
      }
    }
    if (event.target.id === "restartServiceBtn") {
      const result = await post("/api/config/restart-service");
      renderConfigNotice([{ message: result.restarted ? "服务已重启。" : "服务未运行，无需重启。" }], "success");
    }
    if (event.target.id === "saveSchedulerJobBtn") {
      await post("/api/scheduler/jobs", { job: schedulerJobFromForm() });
      renderConfigNotice([{ message: "定时任务已保存。为了让服务使用新配置，请重启服务。" }], "success");
    }
    if (event.target.dataset.schedulerRun) {
      const result = await post(`/api/scheduler/jobs/${encodeURIComponent(event.target.dataset.schedulerRun)}/run-now`);
      renderConfigNotice([{ message: result.result?.message || "定时任务已执行。" }], "success");
    }
    if (event.target.dataset.schedulerPause) {
      await post(`/api/scheduler/jobs/${encodeURIComponent(event.target.dataset.schedulerPause)}/pause`);
      renderConfigNotice([{ message: "定时任务已暂停。" }], "success");
    }
    if (event.target.dataset.schedulerResume) {
      await post(`/api/scheduler/jobs/${encodeURIComponent(event.target.dataset.schedulerResume)}/resume`);
      renderConfigNotice([{ message: "定时任务已启用。" }], "success");
    }
    if (event.target.dataset.schedulerEdit) {
      const job = (state.scheduler?.jobs || []).find((item) => item.id === event.target.dataset.schedulerEdit);
      if (job) fillSchedulerJobForm(job);
    }
    if (event.target.id === "clearLogBtn") el("logOutput").textContent = "";
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("input", (event) => {
  if (!event.target.matches("[data-config-field]")) return;
  state.configDirty = true;
  el("configMeta").textContent = `${el("configMeta").textContent.replace(" · 有未保存改动", "")} · 有未保存改动`;
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
