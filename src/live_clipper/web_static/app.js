const state = {
  runs: [],
  selectedRunId: null,
  detail: null,
};

const el = (id) => document.getElementById(id);

const phaseLabels = {
  running: "运行中",
  needs_codex_selection: "等待 Codex",
  ready_to_render: "可渲染",
  cleanup_ready: "可清理",
  waiting_or_manual: "待处理",
  missing: "缺失",
};

const stepHints = [
  "监听 NAS 目录，检测新录制文件",
  "从 NAS 复制到本地工作目录",
  "Whisper 本地模型转写",
  "Agnes 扫描全文，发现高价值片段",
  "Agnes 精炼候选，去重合并并排序",
  "Codex 基于策略选择最终片段",
  "剪辑、字幕、封面、导出成片",
  "清理中间文件，归档成品",
];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || "请求失败");
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
  }, 4200);
}

function shortName(run) {
  return run.source_name || run.run_id;
}

function phaseLabel(phase) {
  return phaseLabels[phase] || phase || "未知";
}

function renderRunList() {
  const query = el("runSearch").value.trim().toLowerCase();
  const list = el("runList");
  const runs = state.runs.filter((run) => shortName(run).toLowerCase().includes(query));
  if (!runs.length) {
    list.innerHTML = '<div class="run-item"><div class="run-title">暂无任务</div><div class="run-subtitle">等待流水线生成 output 目录</div></div>';
    return;
  }

  list.innerHTML = runs
    .map((run) => {
      const active = run.run_id === state.selectedRunId ? " active" : "";
      const phase = run.phase || "waiting_or_manual";
      const percent = progressFor(run);
      return `
        <button class="run-item${active}" data-run-id="${escapeHtml(run.run_id)}">
          <span class="run-title">${escapeHtml(shortName(run))}</span>
          <span class="progress-value">${percent}%</span>
          <span class="run-subtitle">${escapeHtml(run.run_id)}</span>
          <span class="status-pill ${escapeHtml(phase)}">${escapeHtml(phaseLabel(phase))}</span>
        </button>
      `;
    })
    .join("");
}

function progressFor(run) {
  if (run.phase === "cleanup_ready") return 100;
  if (run.clip_count > 0) return 100;
  if (run.phase === "ready_to_render") return 86;
  if (run.phase === "needs_codex_selection") return 78;
  if (run.candidate_count > 0) return 64;
  if (run.running) return 42;
  return 0;
}

function renderDetail() {
  const detail = state.detail;
  if (!detail || !detail.ok) {
    el("pipelineMeta").textContent = "请选择一个任务";
    el("steps").innerHTML = "";
    el("metrics").innerHTML = "";
    el("taskTitle").textContent = "暂无任务";
    el("taskSubtitle").textContent = "左侧选择一次运行";
    el("commandText").textContent = "等待选择任务";
    el("fileList").innerHTML = "";
    el("clipList").innerHTML = "";
    el("cleanupList").innerHTML = "";
    el("logOutput").textContent = "等待选择任务...";
    el("renderBtn").disabled = true;
    el("cleanupPreviewBtn").disabled = true;
    el("cleanupConfirmBtn").disabled = true;
    el("deleteLocalSourceBtn").disabled = true;
    return;
  }

  const run = detail.run;
  el("currentRun").textContent = `当前运行：${shortName(run)} · ${phaseLabel(run.phase)}`;
  el("pipelineMeta").innerHTML = `
    <span>运行目录：${escapeHtml(run.run_dir)}</span>
    <span>PID：${run.pid || "无"}</span>
    <span>下一步：${escapeHtml(run.next_step)}</span>
  `;

  el("steps").innerHTML = detail.steps
    .map((step, index) => {
      const rowState = step.state || (step.done ? "done" : "pending");
      return `
        <div class="step-row ${escapeHtml(rowState)}">
          <div class="step-index">${String(index + 1).padStart(2, "0")}</div>
          <div class="step-icon">${iconFor(index)}</div>
          <div class="step-main">
            <strong>${escapeHtml(step.label)}${step.agnes ? '<span class="agnes-tag">Agnes</span>' : ""}</strong>
            <small>${escapeHtml(stepHints[index] || "")}</small>
          </div>
          <div>${step.count === undefined || step.count === null ? "—" : `${step.count} 个`}</div>
          <div class="step-state">${stateText(rowState)}</div>
        </div>
      `;
    })
    .join("");

  renderMetrics(run);
  renderCodexPanel(detail);
  renderClips(detail);
  renderCleanup(detail);
  renderLog(detail);
}

function renderMetrics(run) {
  const metrics = [
    ["候选片段", `${run.candidate_count || 0}`],
    ["已选片段", `${run.selected_count || 0}`],
    ["成片数量", `${run.clip_count || 0}`],
    ["运行状态", phaseLabel(run.phase)],
    ["Codex", run.requires_codex ? "需要介入" : "暂无介入"],
  ];
  el("metrics").innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
}

function renderCodexPanel(detail) {
  const run = detail.run;
  const command = commandFor(run);
  el("decisionBadge").textContent = run.requires_codex ? "需要决策" : "自动推进";
  el("taskTitle").textContent = taskTitleFor(run);
  el("taskSubtitle").textContent = run.next_step;
  el("commandText").textContent = command;
  el("renderBtn").disabled = !detail.actions.can_render;
  el("cleanupPreviewBtn").disabled = !detail.actions.can_cleanup_preview;
  el("cleanupConfirmBtn").disabled = !detail.actions.can_cleanup;
  el("deleteLocalSourceBtn").disabled = !detail.actions.can_delete_local_source;

  const fileNames = ["codex_brief.json", "refined_candidates.json", "selected_clips.json", "run_report.json"];
  el("fileList").innerHTML = fileNames
    .map((name) => {
      const file = detail.files[name] || {};
      return `
        <div class="file-row">
          <strong>${escapeHtml(name)}</strong>
          <small>${file.exists ? escapeHtml(file.path) : "暂未生成"}</small>
        </div>
      `;
    })
    .join("");
}

function renderClips(detail) {
  const clips = detail.clips || [];
  const preview = el("clipPreview");
  el("clipCount").textContent = `${clips.length} 个`;
  if (!clips.length) {
    preview.hidden = true;
    preview.removeAttribute("src");
    preview.dataset.runId = "";
    el("clipList").innerHTML = '<div class="file-row"><strong>暂无成片</strong><small>渲染完成后会出现在这里</small></div>';
    return;
  }

  el("clipList").innerHTML = clips
    .map((clip, index) => `
      <div class="clip-row">
        <div>
          <strong>${escapeHtml(clip.name)}</strong>
          <small>${formatBytes(clip.bytes)} · ${escapeHtml(clip.path)}</small>
        </div>
        <div class="clip-actions">
          <button class="mini-button" data-preview-clip="${escapeHtml(clip.url)}">查看</button>
          <button class="mini-button" data-copy-path="${escapeHtml(clip.path)}">路径</button>
          <button class="mini-button danger" data-delete-clip="${escapeHtml(clip.name)}">删除</button>
        </div>
      </div>
    `)
    .join("");

  if (preview.dataset.runId !== detail.run.run_id) {
    preview.src = clips[0].url;
    preview.dataset.runId = detail.run.run_id;
    preview.hidden = false;
  }
}

function renderCleanup(detail) {
  const cleanup = detail.cleanup || { targets: [], deletable_bytes: 0 };
  el("cleanupBytes").textContent = `可释放 ${formatBytes(cleanup.deletable_bytes || 0)}`;
  if (!cleanup.targets.length) {
    el("cleanupList").innerHTML = '<div class="cleanup-row protected"><strong>暂无可清理文件</strong><small>需要 run_metadata.json 才能识别本地副本</small></div>';
    return;
  }
  el("cleanupList").innerHTML = cleanup.targets
    .map((target) => `
      <div class="cleanup-row ${target.deletable ? "deletable" : "protected"}">
        <strong>${target.deletable ? "可删除" : "受保护"} · ${cleanupKind(target.kind)}</strong>
        <small>${formatBytes(target.bytes)} · ${escapeHtml(target.path)}</small>
        <small>${escapeHtml(target.reason)}</small>
      </div>
    `)
    .join("");
}

function renderLog(detail) {
  el("logPath").textContent = detail.log.path || "暂无日志文件";
  el("logOutput").textContent = detail.log.tail || "暂无日志输出";
}

function iconFor(index) {
  return ["▰", "□", "▮", "A", "✦", "&lt;/&gt;", "▶", "⌫"][index] || "•";
}

function stateText(value) {
  if (value === "done") return "完成";
  if (value === "waiting") return "等待 Codex";
  if (value === "active") return "可执行";
  return "待处理";
}

function taskTitleFor(run) {
  if (run.phase === "needs_codex_selection") return "选择最终导出片段";
  if (run.phase === "ready_to_render") return "渲染 selected_clips.json";
  if (run.phase === "cleanup_ready") return "确认本地大文件清理";
  if (run.running) return "流水线正在运行";
  return "检查任务状态";
}

function commandFor(run) {
  if (!run.run_dir) return "等待选择任务";
  if (run.phase === "needs_codex_selection") {
    return `codex select \\\n  --input ${run.run_dir}/refined_candidates.json \\\n  --output ${run.run_dir}/selected_clips.json \\\n  --strategy balanced`;
  }
  if (run.phase === "ready_to_render") {
    return `.venv/bin/live-clipper render ${run.run_dir}/selected_clips.json`;
  }
  if (run.phase === "cleanup_ready") {
    return `.venv/bin/live-clipper cleanup ${run.run_dir}\n.venv/bin/live-clipper cleanup ${run.run_dir} --confirm`;
  }
  return ".venv/bin/live-clipper automation check";
}

async function loadRuns({ keepSelection = true } = {}) {
  const payload = await api("/api/runs");
  state.runs = payload.runs || [];
  if (!keepSelection || !state.selectedRunId || !state.runs.some((run) => run.run_id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.run_id || null;
  }
  renderRunList();
  if (state.selectedRunId) {
    await loadDetail(state.selectedRunId);
  } else {
    renderDetail();
  }
}

async function loadDetail(runId) {
  state.selectedRunId = runId;
  state.detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
  renderRunList();
  renderDetail();
}

async function postAction(path, successMessage) {
  const payload = await api(path, { method: "POST" });
  toast(successMessage);
  await loadRuns();
  return payload;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function cleanupKind(kind) {
  if (kind === "local_source_video") return "本机原录像副本";
  if (kind === "audio") return "ASR 中间音频";
  return kind || "文件";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.addEventListener("click", async (event) => {
  const runItem = event.target.closest(".run-item[data-run-id]");
  try {
    if (runItem) {
      await loadDetail(runItem.dataset.runId);
      return;
    }
    if (event.target.id === "refreshBtn") await loadRuns({ keepSelection: true });
    if (event.target.id === "checkBtn") await postAction("/api/automation/check", "自动化状态已刷新");
    if (event.target.id === "renderBtn" && state.selectedRunId) {
      await postAction(`/api/runs/${encodeURIComponent(state.selectedRunId)}/render`, "渲染已完成或已开始");
    }
    if (event.target.id === "cleanupPreviewBtn" && state.selectedRunId) {
      const report = await postAction(`/api/runs/${encodeURIComponent(state.selectedRunId)}/cleanup-preview`, "清理预演完成");
      el("logOutput").textContent = JSON.stringify(report, null, 2);
    }
    if (event.target.id === "cleanupConfirmBtn" && state.selectedRunId) {
      if (confirm("确认删除本地缓存和中间大文件？NAS 原始录像不会被删除。")) {
        await postAction(`/api/runs/${encodeURIComponent(state.selectedRunId)}/cleanup-confirm`, "本地缓存清理完成");
      }
    }
    if (event.target.id === "deleteLocalSourceBtn" && state.selectedRunId) {
      if (confirm("确认删除本机 input 目录里的原录像副本？NAS 原始录像会保留。")) {
        await postAction(`/api/runs/${encodeURIComponent(state.selectedRunId)}/delete-local-source`, "本机原录像副本已删除");
      }
    }
    if (event.target.dataset.previewClip) {
      const preview = el("clipPreview");
      preview.src = event.target.dataset.previewClip;
      preview.hidden = false;
      await preview.play().catch(() => {});
    }
    if (event.target.dataset.copyPath) {
      await navigator.clipboard.writeText(event.target.dataset.copyPath);
      toast("路径已复制");
    }
    if (event.target.dataset.deleteClip && state.selectedRunId) {
      const clipName = event.target.dataset.deleteClip;
      if (confirm(`确认删除成片 ${clipName}？`)) {
        await postAction(
          `/api/runs/${encodeURIComponent(state.selectedRunId)}/clips/${encodeURIComponent(clipName)}/delete`,
          "成片已删除",
        );
      }
    }
    if (event.target.id === "copyCommandBtn") {
      await navigator.clipboard.writeText(el("commandText").textContent);
      toast("命令已复制");
    }
    if (event.target.id === "clearLogBtn") {
      el("logOutput").textContent = "";
    }
  } catch (error) {
    toast(error.message);
  }
});

el("runSearch").addEventListener("input", renderRunList);

loadRuns({ keepSelection: false }).catch((error) => toast(error.message));
setInterval(() => loadRuns({ keepSelection: true }).catch(() => {}), 15000);
