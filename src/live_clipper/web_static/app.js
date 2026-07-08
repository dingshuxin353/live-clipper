const state = {
  activeTab: "clips",
  phase: "",
  runs: [],
  selectedRunId: null,
  detail: null,
  confirmations: [],
  events: [],
  config: null,
  configPayload: null,
  serviceStatus: null,
  configDirty: false,
  scheduler: null,
  reviewAutomation: null,
  jobPolls: {},
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
  rendering: "渲染中",
  needs_review: "待审阅",
  rendered: "已成片",
  failed: "失败",
  cleanup_ready: "已成片",
  ready_to_render: "可渲染",
  needs_codex_selection: "待审阅",
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
  ai_review: "AI 自动审阅",
  maintenance_check: "维护检查",
  weekly: "每周",
  daily: "每天",
  interval_minutes: "每隔 N 分钟",
  success: "成功",
  skipped: "已跳过",
  local_agent: "本地 Agent",
  model: "配置模型直连",
  codex_cli: "Codex CLI",
  claude_code: "Claude Code",
  keep_needs_review: "保留待审阅",
  mark_failed: "标记失败",
};

const cleanupKindLabels = {
  audio: "中间音频",
  local_source_video: "本地源视频",
};

function labelFor(value) {
  return valueLabels[value] || phaseLabels[value] || value || "-";
}

function canonicalPhase(phase) {
  if (phase === "rendering" || phase === "running" || phase === "ready_to_render") return "processing";
  if (phase === "cleanup_ready") return "rendered";
  if (phase === "needs_codex_selection") return "needs_review";
  return phase || "unknown";
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("active", section.id === `section-${tab}`);
  });
}

function infoRow(label, value) {
  return `<div class="info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "-")}</strong></div>`;
}

function renderServiceStatus(status) {
  const service = status.service || {};
  const source = status.source_summary || {};
  const metrics = [
    ["状态", labelFor(service.status || "stopped")],
    ["PID", service.pid || "无"],
    ["待审阅", String(status.pending_review_runs?.length || 0)],
    ["失败", String(status.failed_runs?.length || 0)],
    ["待确认", String(status.pending_confirmation_count || 0)],
    ["当前任务", status.active_run || "无"],
  ];
  el("serviceMetrics").innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
  el("serviceSummary").innerHTML = [
    ["最近心跳", service.last_heartbeat_at || "-"],
    ["下次扫描", service.next_scan_at || "-"],
    ["录播源", source.source_dir || "-"],
    ["输入目录", source.input_dir || "-"],
    ["输出目录", source.output_root || "-"],
    ["最近错误", service.last_error || "-"],
  ].map(([label, value]) => infoRow(label, value)).join("");
  renderSidebarServiceCard(status);
}

function renderSidebarServiceCard(status = {}) {
  const service = status.service || {};
  const scheduler = state.scheduler?.scheduler || {};
  const running = Boolean(status.running);
  el("sidebarServiceCard").innerHTML = `
    <small>本机服务</small>
    <strong>${escapeHtml(running ? "服务运行中" : labelFor(service.status || "已停止"))}</strong>
    <small>PID：${escapeHtml(service.pid || "无")}</small>
    <small>下次扫描：${escapeHtml(service.next_scan_at || "-")}</small>
    <small>下次定时：${escapeHtml(scheduler.next_due_at || "-")}</small>
    <div class="button-row" style="margin-top: 12px;">
      <button class="secondary-button small" data-sidebar-scan type="button">立即扫描</button>
      <button class="secondary-button small" data-sidebar-service-toggle="${running ? "stop" : "start"}" type="button">${running ? "停止" : "启动"}</button>
    </div>
  `;
}

function renderClipsSubtitle() {
  const runCount = state.runs.length;
  const clipCount = state.runs.reduce((total, run) => total + Number(run.clip_count || 0), 0);
  const processingCount = state.runs.filter((run) => ["processing", "rendering", "running", "ready_to_render"].includes(run.phase)).length;
  const reviewCount = state.runs.filter((run) => canonicalPhase(run.phase) === "needs_review").length;
  const parts = [];
  if (clipCount) parts.push(`生成 ${clipCount} 个成片`);
  if (processingCount) parts.push(`${processingCount} 场正在处理`);
  if (reviewCount) parts.push(`${reviewCount} 场待审阅`);
  el("clipsSubtitle").textContent = runCount
    ? `AI 已从 ${runCount} 场直播中 ${parts.length ? parts.join("，") : "整理处理状态"}`
    : "查看录播处理、AI 审阅和成片结果";
}

function runMeta(run) {
  const bits = [
    run.updated_at || run.created_at || "-",
    `${Number(run.clip_count || 0)} 个成片`,
    `${Number(run.candidate_count || 0)} 个候选`,
    labelFor(run.phase || "unknown"),
  ];
  return bits.filter(Boolean).join(" · ");
}

function renderRuns() {
  renderClipsSubtitle();
  const list = el("runList");
  if (!state.runs.length) {
    list.innerHTML = '<div class="empty">还没有录播任务。可以先点击「立即扫描录播」。</div>';
    return;
  }
  list.innerHTML = state.runs.map((run) => {
    const active = run.run_id === state.selectedRunId;
    const expanded = active && state.detail?.ok;
    const stuckNotice = run.stuck
      ? `<div class="run-stuck-notice">⚠️ 已处理较长时间仍未完成，可能已卡住。请点击展开后「查看日志」，或重启本机服务后重试。</div>`
      : "";
    return `
      <article class="clip-card ${active ? "active" : ""} ${run.stuck ? "stuck" : ""}">
        <button class="clip-card-main" data-run-id="${escapeHtml(run.run_id)}" type="button">
          <span>
            <span class="run-title">${escapeHtml(run.source_name || run.run_id)}</span>
            <span class="run-meta">${escapeHtml(runMeta(run))}</span>
          </span>
          <span class="status-pill ${escapeHtml(canonicalPhase(run.phase))}">${escapeHtml(labelFor(run.phase || "unknown"))}</span>
        </button>
        ${stuckNotice}
        ${expanded ? renderRunExpandedContent(state.detail) : ""}
      </article>
    `;
  }).join("");
}

function renderRunExpandedContent(detail) {
  const run = detail.run || {};
  const phase = canonicalPhase(run.phase);
  if (phase === "rendered") return renderRenderedContent(detail);
  if (phase === "needs_review") return renderReviewContent(detail);
  if (phase === "failed") return renderFailedContent(detail);
  return renderProcessingContent(detail);
}

function renderRenderedContent(detail) {
  const run = detail.run || {};
  const clips = detail.clips || [];
  return `
    <div class="clip-card-body">
      <div class="clip-actions">
        <span class="run-meta">${escapeHtml(run.run_dir || "-")}</span>
        <button class="secondary-button small" data-copy-text="${escapeHtml(run.run_dir || "")}" type="button">复制目录</button>
        <button id="cleanupPreviewBtn" class="secondary-button small" type="button" ${detail.actions?.can_cleanup_preview ? "" : "disabled"}>预览清理</button>
      </div>
      ${clips.length ? clips.map((clip) => renderClipRow(clip)).join("") : '<div class="empty">已进入成片阶段，但还没有检测到 clips/*.mp4。</div>'}
    </div>
  `;
}

function renderClipRow(clip) {
  const title = clip.title || clip.name || "未命名成片";
  const description = clip.description || clip.desc || clip.path || "";
  return `
    <div class="clip-row">
      <div class="clip-thumb">▶</div>
      <div>
        <div class="clip-title">${escapeHtml(title)}</div>
        <div class="clip-desc">${escapeHtml(description)}</div>
      </div>
      <div class="clip-actions">
        <button class="secondary-button small" data-copy-text="${escapeHtml(title)}" type="button">复制标题</button>
        <button class="secondary-button small" data-copy-text="${escapeHtml(description)}" type="button">复制简介</button>
        <span class="run-meta">${escapeHtml(formatBytes(clip.bytes))}</span>
      </div>
    </div>
  `;
}

function renderReviewContent(detail) {
  const run = detail.run || {};
  const candidates = run.candidate_count || detail.candidates_count || 0;
  const aiReview = detail.ai_review;
  const aiError = aiReview && aiReview.status === "failed"
    ? `<div class="notice error" style="margin-bottom: 12px;">上次 AI 审阅失败：${escapeHtml(aiReview.error || "未知错误")}</div>`
    : "";
  const reviewing = detail.active_job && detail.active_job.status === "running";
  const aiButton = reviewing
    ? `<button id="aiReviewRunBtn" class="primary-button small" type="button" disabled>AI 审阅中…</button>`
    : `<button id="aiReviewRunBtn" class="primary-button small" type="button" ${detail.actions?.can_ai_review ? "" : "disabled"}>立即 AI 审阅</button>`;
  return `
    <div class="clip-card-body">
      ${aiError}
      <div class="clip-actions">
        <p class="muted" style="flex: 1;">AI 已找到 <strong>${escapeHtml(candidates)}</strong> 个候选片段，审阅后即可渲染成片。</p>
        <button class="secondary-button small" data-copy-text="${escapeHtml(run.run_dir || "")}" type="button">复制审阅包路径</button>
        ${aiButton}
        <button id="renderRunBtn" class="secondary-button small" type="button" ${detail.actions?.can_render ? "" : "disabled"}>渲染</button>
      </div>
    </div>
  `;
}

function renderProcessingContent(detail) {
  const steps = detail.steps?.length
    ? detail.steps
    : [
        { label: "拉取录像", state: "done" },
        { label: "语音转写", state: "active" },
        { label: "AI 选片", state: "waiting" },
        { label: "渲染", state: "waiting" },
      ];
  return `
    <div class="clip-card-body">
      <div class="clip-actions">
        ${steps.map((step) => `<span class="status-pill ${step.done || step.state === "done" ? "rendered" : step.state === "active" ? "processing" : ""}">${escapeHtml(step.label)}</span>`).join("")}
        <button class="secondary-button small" data-show-log type="button">查看日志</button>
      </div>
    </div>
  `;
}

function renderFailedContent(detail) {
  const run = detail.run || {};
  return `
    <div class="clip-card-body">
      <div class="notice error">${escapeHtml(run.last_error || "任务失败，暂无错误详情。")}</div>
      <div class="button-row" style="margin-top: 12px;">
        <button class="secondary-button small" data-show-log type="button">查看日志</button>
      </div>
    </div>
  `;
}

function renderRunDetail() {
  const detail = state.detail;
  const hidden = el("runDetail");
  if (!detail || !detail.ok) {
    hidden.innerHTML = '<div class="empty">请选择一个任务。</div>';
    el("logOutput").textContent = "选择一个任务查看日志。";
    return;
  }
  hidden.innerHTML = `
    <div class="info-grid">
      ${infoRow("任务", detail.run?.run_id)}
      ${infoRow("阶段", labelFor(detail.run?.phase))}
      ${infoRow("源文件", detail.run?.source_path)}
      ${infoRow("本地副本", detail.run?.local_source_path)}
      ${infoRow("任务目录", detail.run?.run_dir)}
      ${infoRow("候选数", detail.run?.candidate_count || detail.candidates_count || 0)}
      ${infoRow("已选片段", detail.run?.selected_count || detail.selected_count || 0)}
      ${infoRow("成片数", detail.run?.clip_count || detail.rendered_clip_count || 0)}
    </div>
  `;
  el("logOutput").textContent = detail.log?.log || detail.log?.tail || "暂无任务日志。";
}

function renderConfirmations() {
  const pending = state.confirmations.filter((item) => item.status === "pending");
  const badge = el("confirmationBadge");
  badge.textContent = String(pending.length);
  badge.hidden = pending.length === 0;
  if (!pending.length) {
    el("confirmationList").innerHTML = '<div class="empty">没有待确认的操作</div>';
    return;
  }
  el("confirmationList").innerHTML = pending.map((item) => `
    <article class="confirmation-row">
      <input type="checkbox" data-confirmation-check="${escapeHtml(item.id)}" />
      <div>
        <strong>${escapeHtml(labelFor(item.action))}</strong>
        <small>${escapeHtml(item.id)} · 任务 ${escapeHtml(item.run_id)}</small>
        <small>${escapeHtml(item.target_path)}</small>
        <small>${escapeHtml(item.reason || item.message || "")}</small>
      </div>
      <span class="risk ${escapeHtml(item.risk_level)}">${escapeHtml(labelFor(item.risk_level))}</span>
      <div class="button-row">
        <button class="secondary-button small" data-reject="${escapeHtml(item.id)}" type="button">拒绝</button>
        <button class="danger-button small" data-approve="${escapeHtml(item.id)}" type="button">确认执行</button>
      </div>
    </article>
  `).join("");
}

function renderEvents() {
  el("eventStream").innerHTML = state.events.map((event) => `
    <div class="event-row">
      <strong>${escapeHtml(event.type)}</strong>
      <small>${escapeHtml(event.created_at)} · ${escapeHtml(event.run_id || "-")}</small>
    </div>
  `).join("") || '<div class="empty">暂无事件。</div>';
}

async function loadService() {
  state.serviceStatus = await api("/api/service");
  renderServiceStatus(state.serviceStatus);
  renderConfigHealth();
}

async function loadRuns() {
  const suffix = state.phase ? `?phase=${encodeURIComponent(state.phase)}` : "";
  const payload = await api(`/api/runs${suffix}`);
  state.runs = payload.runs || [];
  if (!state.selectedRunId || !state.runs.some((run) => run.run_id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.run_id || null;
  }
  if (state.selectedRunId) await loadRunDetail(state.selectedRunId);
  else {
    state.detail = null;
    renderRuns();
    renderRunDetail();
  }
}

async function pollAiReviewJob(runId, jobId) {
  if (!jobId || state.jobPolls[jobId]) return;
  state.jobPolls[jobId] = true;
  try {
    for (let i = 0; i < 200; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      let job;
      try {
        const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        job = payload.job;
      } catch (_error) {
        continue; // 瞬时网络错误，继续轮询
      }
      if (!job || job.status === "running") continue;
      if (job.status === "succeeded") {
        const count = job.result && job.result.selected_count != null ? job.result.selected_count : "?";
        toast(`AI 审阅完成，已选 ${count} 个片段`);
      } else if (job.status === "interrupted") {
        toast("AI 审阅在服务重启时被中断，请重试。");
      } else {
        toast(`AI 审阅失败：${job.error || "未知错误"}`);
      }
      break;
    }
  } finally {
    delete state.jobPolls[jobId];
    await refreshAll();
    if (state.selectedRunId === runId) await loadRunDetail(runId);
  }
}

async function loadRunDetail(runId) {
  state.selectedRunId = runId;
  state.detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
  renderRuns();
  renderRunDetail();
  const job = state.detail && state.detail.active_job;
  if (job && job.status === "running") pollAiReviewJob(runId, job.id);
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

async function loadReviewAutomation() {
  state.reviewAutomation = await api("/api/review-automation");
  renderReviewAutomation(state.reviewAutomation);
}

async function refreshAll() {
  await Promise.all([loadService(), loadRuns(), loadConfirmations(), loadEvents(), loadConfig(), loadScheduler(), loadReviewAutomation()]);
}

function renderConfig(payload) {
  const config = payload.config || {};
  state.configPayload = payload;
  el("configMeta").textContent = `${payload.config_path || "live-clipper.toml"} · ${payload.exists ? "已存在" : "尚未创建"} · API Key 只保存环境变量名`;
  document.querySelectorAll("[data-config-field]").forEach((field) => {
    const value = getConfigValue(config, field.dataset.configField);
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  renderEnvStatus(payload.env_status || {});
  renderWebAccessTokenStatus(config.web?.access_token_configured);
  renderConfigNotice(payload.warnings || [], "warning");
  renderConfigHealth();
  renderDirtyBar();
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
        <button class="secondary-button small" data-scheduler-edit="${escapeHtml(job.id)}" type="button">编辑</button>
        <button class="primary-button small" data-scheduler-run="${escapeHtml(job.id)}" type="button">立即执行</button>
        <button class="secondary-button small" data-scheduler-pause="${escapeHtml(job.id)}" type="button">暂停</button>
        <button class="secondary-button small" data-scheduler-resume="${escapeHtml(job.id)}" type="button">启用</button>
      </div>
    </div>
  `).join("") || '<div class="empty">还没有定时任务。</div>';
  renderSidebarServiceCard(state.serviceStatus || {});
  renderConfigHealth();
}

function renderReviewAutomation(payload) {
  if (!payload?.ok) return;
  const review = payload.review_automation || {};
  const environment = payload.environment || {};
  el("reviewAutomationStatus").innerHTML = [
    ["AI 审阅状态", review.enabled ? "已启用" : "未启用"],
    ["审阅方式", labelFor(review.mode)],
    ["Provider", labelFor(review.provider)],
    ["Codex CLI", environment.codex_cli?.available ? "可用" : "未检测到"],
    ["Claude Code", environment.claude_code?.available ? "可用" : "未检测到"],
    ["LLM API key", environment.llm?.api_key_configured ? "已配置" : "未配置"],
    ["最近结果", labelFor(review.last_status)],
    ["最近错误", review.last_error || "-"],
  ].map(([label, value]) => infoRow(label, value)).join("");
  renderConfigHealth();
}

function renderConfigHealth() {
  const node = el("configHealth");
  if (!node) return;
  const config = state.config || {};
  const envStatus = state.configPayload?.env_status || {};
  const sourceDir = config.recording_source_default?.source_dir;
  const inputDir = config.recording_source_default?.input_dir || config.paths?.input_dir;
  const outputRoot = config.recording_source_default?.output_root || config.paths?.output_root;
  const llmKey = config.llm?.api_key_env;
  const asrKey = config.asr?.api_key_env;
  const scheduler = state.scheduler?.scheduler || {};
  const review = state.reviewAutomation?.review_automation || {};
  const reviewEnv = state.reviewAutomation?.environment || {};
  const reviewAvailable = reviewEnv.current_mode_available ?? reviewEnv.ok;
  const cards = [
    { label: "录播源", status: sourceDir ? "已配置" : "未配置", detail: sourceDir || "未填写录播源目录", tone: sourceDir ? "ok" : "warning" },
    { label: "本地项目库", status: inputDir && outputRoot ? "正常" : "待配置", detail: `输入：${inputDir || "-"} · 输出：${outputRoot || "-"}`, tone: inputDir && outputRoot ? "ok" : "warning" },
    { label: "LLM", status: envStatus[llmKey] ? "已配置" : "未配置", detail: llmKey || "未填写 API key 环境变量名", tone: envStatus[llmKey] ? "ok" : "warning" },
    { label: "ASR", status: envStatus[asrKey] ? "已配置" : "未配置", detail: `${config.asr?.backend || "-"} · ${asrKey || "未填写 API key 环境变量名"}`, tone: envStatus[asrKey] ? "ok" : "warning" },
    { label: "服务", status: state.serviceStatus?.running ? "运行中" : "未运行", detail: state.serviceStatus?.service?.pid ? `PID ${state.serviceStatus.service.pid}` : "可在自动化页启动", tone: state.serviceStatus?.running ? "ok" : "neutral" },
    { label: "定时任务", status: scheduler.enabled ? "已启用" : "未启用", detail: scheduler.next_due_at ? `下次：${scheduler.next_due_at}` : "暂无下一次任务", tone: scheduler.enabled ? "ok" : "neutral" },
    { label: "AI 审阅", status: review.enabled ? (reviewAvailable ? "可用" : "不可用") : "未启用", detail: `${labelFor(review.mode) || "-"} · ${labelFor(review.provider) || "-"}`, tone: review.enabled ? (reviewAvailable ? "ok" : "warning") : "neutral" },
  ];
  node.innerHTML = cards.map((card) => `
    <div class="health-card ${card.tone}">
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.status)}</strong>
      <small>${escapeHtml(card.detail)}</small>
    </div>
  `).join("");
}

function renderWebAccessTokenStatus(configured) {
  const node = el("webAccessTokenStatus");
  if (!node) return;
  node.textContent = `Web access token：${configured ? "已配置" : "未配置"}。这里只显示状态，不展示明文。`;
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

function renderReviewAutomationActionStatus(message, type = "info") {
  const node = el("reviewAutomationActionStatus");
  if (!node) return;
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.className = `notice ${type}`;
  node.textContent = message;
}

function reviewAutomationRunDueMessage(result) {
  if (result.skipped_reason === "review_automation_disabled") {
    return "自动 AI 审阅还没有启用。请到「设置」页打开「启用自动 AI 审阅」，保存配置后再执行。";
  }
  if (Array.isArray(result.processed_runs) && result.processed_runs.length > 0) {
    return `已处理 ${result.processed_runs.length} 个待审阅任务。`;
  }
  if (Array.isArray(result.results) && result.results.length > 0) {
    return `AI 审阅已返回 ${result.results.length} 条结果，请查看运行日志。`;
  }
  return result.message || "当前没有待审阅任务。";
}

function renderDirtyBar() {
  const bar = el("settingsDirtyBar");
  if (bar) bar.hidden = !state.configDirty;
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
    recording_source_default: { source_dir: "", input_dir: "input", output_root: "output", since_hours: 168, min_age_minutes: 10, stable_check_seconds: 60 },
    llm: { provider_label: "OpenAI-compatible LLM", api_base: "https://apihub.agnes-ai.com/v1", api_key_env: "CHEAP_MODEL_API_KEY", model: "agnes-2.0-flash", timeout_seconds: 300, request_attempts: 5, retry_delay_seconds: 3.0 },
    asr: { backend: "mlx_whisper", model: "mlx-community/whisper-large-v3-turbo", language: "zh", api_base: "https://api.openai.com/v1", api_key_env: "ASR_API_KEY", hf_token_env: "HF_TOKEN" },
    service: { enabled: true, scan_interval_minutes: 30, auto_render_after_selection: true, cleanup_mode: "preview_only" },
    scheduler: { enabled: true, timezone: "Asia/Shanghai", tick_seconds: 30, missed_policy: "run_once", state_dir: "work/service" },
    scheduler_jobs: [
      { id: "weekly_recording_scan", name: "每周录播扫描", enabled: true, type: "scan_recordings", schedule: "weekly", day_of_week: "sun", time: "00:00", skip_if_running: true },
      { id: "weekly_review_due", name: "每周审阅检查", enabled: true, type: "review_due_check", schedule: "weekly", day_of_week: "sun", time: "12:00", skip_if_running: true },
    ],
    review_automation: { enabled: false, mode: "local_agent", max_runs_per_tick: 1, auto_render_after_selection: true, on_failure: "keep_needs_review", timeout_minutes: 60, prompt_template: "default_clip_review" },
    review_automation_local_agent: { provider: "codex_cli", command_timeout_minutes: 60, include_review_package_inline: true, allow_agent_file_writes: false },
    review_automation_model: { provider: "openai_compatible", use_llm_config: true, model: "", max_candidates: 40, temperature: 0.2, max_tokens: 4096, retry_attempts: 2 },
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
  state.configDirty = false;
  renderDirtyBar();
  renderConfigNotice(messages, "success");
  await loadConfig(true);
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

async function copyText(text) {
  if (!text) return toast("没有可复制的内容");
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (_error) {
    toast(text);
  }
}

document.addEventListener("click", async (event) => {
  const tab = event.target.closest("[data-tab]");
  const run = event.target.closest("[data-run-id]");
  const copy = event.target.closest("[data-copy-text]");
  try {
    if (tab) switchTab(tab.dataset.tab);
    if (run) await loadRunDetail(run.dataset.runId);
    if (copy) await copyText(copy.dataset.copyText);
    if (event.target.id === "refreshBtn") await refreshAll();
    if (event.target.id === "startServiceBtn" || event.target.dataset.sidebarServiceToggle === "start") await post("/api/service/start");
    if (event.target.id === "stopServiceBtn" || event.target.dataset.sidebarServiceToggle === "stop") await post("/api/service/stop");
    if (event.target.id === "scanNowBtn" || event.target.dataset.sidebarScan !== undefined) await post("/api/service/scan-now");
    if (event.target.id === "renderRunBtn" && state.selectedRunId) await post(`/api/runs/${encodeURIComponent(state.selectedRunId)}/render`);
    if (event.target.id === "cleanupPreviewBtn" && state.selectedRunId) {
      const result = await post(`/api/runs/${encodeURIComponent(state.selectedRunId)}/cleanup-preview`);
      el("logOutput").textContent = JSON.stringify(result, null, 2);
      switchTab("automation");
    }
    if (event.target.id === "aiReviewRunBtn" && state.selectedRunId) {
      const button = event.target;
      const runId = state.selectedRunId;
      button.disabled = true;
      button.textContent = "AI 审阅中…";
      try {
        const payload = await api(`/api/runs/${encodeURIComponent(runId)}/ai-review`, { method: "POST" });
        toast("AI 审阅已开始，正在后台处理…");
        pollAiReviewJob(runId, payload.job && payload.job.id);
      } catch (err) {
        button.disabled = false;
        button.textContent = "立即 AI 审阅";
        toast(`AI 审阅启动失败：${err && err.message ? err.message : err}`);
      }
    }
    if (event.target.dataset.showLog !== undefined) switchTab("automation");
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
    if (event.target.id === "saveConfigBtn" || event.target.id === "saveConfigStickyBtn") await saveConfig();
    if (event.target.id === "reloadConfigBtn" || event.target.id === "discardConfigBtn") {
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
    if (event.target.id === "checkReviewAutomationBtn") {
      const result = await post("/api/review-automation/check");
      renderConfigNotice([{ message: result.current_mode_available ? "AI 审阅环境可用。" : "当前 AI 审阅环境不可用，请检查 Codex CLI、Claude Code 或 LLM API key。" }], result.current_mode_available ? "success" : "warning");
      switchTab("settings");
    }
    if (event.target.id === "runDueReviewAutomationBtn") {
      const result = await post("/api/review-automation/run-due");
      const message = reviewAutomationRunDueMessage(result);
      el("logOutput").textContent = JSON.stringify(result, null, 2);
      renderReviewAutomationActionStatus(message, result.skipped_reason ? "warning" : "success");
      toast(message);
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
      if (job) {
        fillSchedulerJobForm(job);
        switchTab("settings");
      }
    }
    if (event.target.id === "clearLogBtn") el("logOutput").textContent = "";
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("input", (event) => {
  if (!event.target.matches("[data-config-field]")) return;
  state.configDirty = true;
  const meta = el("configMeta");
  meta.textContent = `${meta.textContent.replace(" · 有未保存改动", "")} · 有未保存改动`;
  renderDirtyBar();
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
