import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@astryxdesign/core/AppShell";
import { Button } from "@astryxdesign/core/Button";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
} from "@astryxdesign/core/SideNav";

import { api, post } from "./api";
import { Onboarding } from "./Onboarding";
import { Settings } from "./Settings";
import type { AppSnapshot, GenericRecord, Model, Run, TabId } from "./types";

const EMPTY_SNAPSHOT: AppSnapshot = {
  service: null,
  runs: [],
  confirmations: [],
  events: [],
  configPayload: null,
  scheduler: null,
  reviewAutomation: null,
  models: [],
};

const phaseLabels: Record<string, string> = {
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

const valueLabels: Record<string, string> = {
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
};

function labelFor(value: unknown) {
  const key = String(value || "");
  return valueLabels[key] || phaseLabels[key] || key || "-";
}

function canonicalPhase(phase: unknown) {
  const value = String(phase || "unknown");
  if (["rendering", "running", "ready_to_render"].includes(value)) return "processing";
  if (value === "cleanup_ready") return "rendered";
  if (value === "needs_codex_selection") return "needs_review";
  return value;
}

function formatBytes(bytes: unknown) {
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function InfoRows({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <>
      {rows.map(([label, value]) => (
        <div className="info-row" key={label}><span>{label}</span><strong>{String(value || "-")}</strong></div>
      ))}
    </>
  );
}

function MetricRows({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <>
      {rows.map(([label, value]) => (
        <div className="metric" key={label}><span>{label}</span><strong>{String(value || "-")}</strong></div>
      ))}
    </>
  );
}

export function App() {
  const [activeTab, setActiveTab] = useState<TabId>("clips");
  const [phase, setPhase] = useState("");
  const [snapshot, setSnapshot] = useState<AppSnapshot>(EMPTY_SNAPSHOT);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<GenericRecord | null>(null);
  const [selectedConfirmations, setSelectedConfirmations] = useState<string[]>([]);
  const [schedulerDraft, setSchedulerDraft] = useState<GenericRecord | null>(null);
  const [log, setLog] = useState("选择一个任务查看日志。");
  const [toast, setToast] = useState("");
  const [loadError, setLoadError] = useState("");
  const mounted = useRef(true);
  const pollingJobs = useRef(new Set<string>());

  const notify = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => {
      if (mounted.current) setToast((current) => current === message ? "" : current);
    }, 3600);
  }, []);

  const refreshModels = useCallback(async () => {
    const payload = await api<{ models?: Model[] }>("/api/asr/models");
    if (mounted.current) {
      setSnapshot((current) => ({ ...current, models: payload.models ?? [] }));
    }
  }, []);

  const loadConfig = useCallback(async () => {
    const payload = await api<GenericRecord>("/api/config");
    if (mounted.current) setSnapshot((current) => ({ ...current, configPayload: payload }));
  }, []);

  const refreshAll = useCallback(async (signal?: AbortSignal) => {
    const suffix = phase ? `?phase=${encodeURIComponent(phase)}` : "";
    try {
      const [service, runsPayload, confirmationsPayload, eventsPayload, configPayload, scheduler, reviewAutomation, modelPayload] = await Promise.all([
        api<GenericRecord>("/api/service", {}, signal),
        api<{ runs?: Run[] }>(`/api/runs${suffix}`, {}, signal),
        api<{ confirmations?: GenericRecord[] }>("/api/confirmations", {}, signal),
        api<{ events?: GenericRecord[] }>("/api/events", {}, signal),
        api<GenericRecord>("/api/config", {}, signal),
        api<GenericRecord>("/api/scheduler", {}, signal),
        api<GenericRecord>("/api/review-automation", {}, signal),
        api<{ models?: Model[] }>("/api/asr/models", {}, signal),
      ]);
      if (!mounted.current || signal?.aborted) return;
      const runs = runsPayload.runs ?? [];
      setSnapshot({
        service,
        runs,
        confirmations: confirmationsPayload.confirmations ?? [],
        events: eventsPayload.events ?? [],
        configPayload,
        scheduler,
        reviewAutomation,
        models: modelPayload.models ?? [],
      });
      setSelectedRunId((current) => current && runs.some((run) => run.run_id === current)
        ? current
        : runs[0]?.run_id ?? null);
      setLoadError("");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (mounted.current) setLoadError((error as Error).message);
    }
  }, [phase]);

  useEffect(() => {
    mounted.current = true;
    if (window.liveClipperShell) document.body.classList.add("in-app-shell");
    const controller = new AbortController();
    void refreshAll(controller.signal);
    const timer = window.setInterval(() => void refreshAll(controller.signal), 15000);
    return () => {
      mounted.current = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [refreshAll]);

  const pollAiReviewJob = useCallback(async (runId: string, jobId: string) => {
    if (!jobId || pollingJobs.current.has(jobId)) return;
    pollingJobs.current.add(jobId);
    try {
      for (let attempt = 0; attempt < 200 && mounted.current; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        let job: GenericRecord | undefined;
        try {
          const payload = await api<{ job?: GenericRecord }>(`/api/jobs/${encodeURIComponent(jobId)}`);
          job = payload.job;
        } catch {
          continue;
        }
        if (!job || job.status === "running") continue;
        if (job.status === "succeeded") {
          notify(`AI 审阅完成，已选 ${String(job.result?.selected_count ?? "?")} 个片段`);
        } else if (job.status === "interrupted") {
          notify("AI 审阅在服务重启时被中断，请重试。");
        } else {
          notify(`AI 审阅失败：${String(job.error || "未知错误")}`);
        }
        break;
      }
    } finally {
      pollingJobs.current.delete(jobId);
      if (mounted.current) {
        await refreshAll();
        if (selectedRunId === runId) {
          const payload = await api<GenericRecord>(`/api/runs/${encodeURIComponent(runId)}`);
          if (mounted.current) setDetail(payload);
        }
      }
    }
  }, [notify, refreshAll, selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setLog("选择一个任务查看日志。");
      return;
    }
    const controller = new AbortController();
    api<GenericRecord>(`/api/runs/${encodeURIComponent(selectedRunId)}`, {}, controller.signal)
      .then((payload) => {
        if (!mounted.current || controller.signal.aborted) return;
        setDetail(payload);
        setLog(String(payload.log?.log || payload.log?.tail || "暂无任务日志。"));
        const job = payload.active_job;
        if (job?.status === "running" && job.id) void pollAiReviewJob(selectedRunId, String(job.id));
      })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) notify((error as Error).message);
      });
    return () => controller.abort();
  }, [notify, pollAiReviewJob, selectedRunId]);

  async function runAction(path: string, payload?: unknown) {
    try {
      const result = await post<GenericRecord>(path, payload);
      if (result.status === "confirmation_required") setLog(`需要确认: ${String(result.confirmation_id)}`);
      await refreshAll();
      return result;
    } catch (error) {
      notify((error as Error).message);
      throw error;
    }
  }

  const pending = snapshot.confirmations.filter((item) => item.status === "pending");
  const tabs: Array<[TabId, string]> = [
    ["clips", "切片结果"],
    ["automation", "自动化"],
    ["confirmations", "确认"],
    ["settings", "设置"],
  ];
  const service = snapshot.service ?? {};
  const schedulerState = snapshot.scheduler?.scheduler ?? {};
  const running = Boolean(service.running);

  return (
    <>
      <AppShell
        className="venus-app-shell"
        contentPadding={0}
        height="fill"
        mobileNav={{ breakpoint: "none", hasToggle: false }}
        sideNav={(
          <SideNav
            aria-label="控制台页面"
            className="venus-side-nav"
            header={(
              <SideNavHeading
                heading="Venus"
                subheading="直播切片 · 本地控制台"
                icon={<img className="brand-mark" src="/static/venus-mark.png" alt="" />}
              />
            )}
            footer={(
              <div id="sidebarServiceCard" className="sidebar-service">
            <small>本机服务</small>
            <strong>{running ? "服务运行中" : labelFor(service.service?.status || "已停止")}</strong>
            <small>PID：{String(service.service?.pid || "无")}</small>
            <small>下次扫描：{String(service.service?.next_scan_at || "-")}</small>
            <small>下次定时：{String(schedulerState.next_due_at || "-")}</small>
                <div className="sidebar-service-actions">
                  <Button label="立即扫描" size="sm" variant="secondary" onClick={() => void runAction("/api/service/scan-now")} />
                  <Button label={running ? "停止" : "启动"} size="sm" variant="secondary" onClick={() => void runAction(running ? "/api/service/stop" : "/api/service/start")} />
                </div>
              </div>
            )}
          >
            <SideNavSection title="工作台" isHeaderHidden>
              {tabs.map(([id, label]) => (
                <SideNavItem
                  endContent={id === "confirmations" && pending.length > 0 ? <span id="confirmationBadge" className="nav-badge">{pending.length}</span> : undefined}
                  isSelected={activeTab === id}
                  key={id}
                  label={label}
                  onClick={() => setActiveTab(id)}
                />
              ))}
            </SideNavSection>
          </SideNav>
        )}
        variant="elevated"
      >
        <div className="main-content">
          {loadError && <div className="notice error" role="alert">初次加载失败：{loadError}</div>}
          <section className={`page ${activeTab === "clips" ? "active" : ""}`} id="section-clips">
            <Clips
              phase={phase}
              setPhase={setPhase}
              runs={snapshot.runs}
              detail={detail}
              selectedRunId={selectedRunId}
              selectRun={setSelectedRunId}
              setActiveTab={setActiveTab}
              setLog={setLog}
              notify={notify}
              refreshAll={refreshAll}
              pollAiReviewJob={pollAiReviewJob}
              runAction={runAction}
            />
          </section>
          <section className={`page ${activeTab === "automation" ? "active" : ""}`} id="section-automation">
            <Automation
              service={service}
              scheduler={snapshot.scheduler}
              reviewAutomation={snapshot.reviewAutomation}
              events={snapshot.events}
              log={log}
              setLog={setLog}
              notify={notify}
              runAction={runAction}
              editSchedulerJob={(job) => {
                setSchedulerDraft(job);
                setActiveTab("settings");
              }}
            />
          </section>
          <section className={`page ${activeTab === "confirmations" ? "active" : ""}`} id="section-confirmations">
            <Confirmations
              pending={pending}
              selected={selectedConfirmations}
              setSelected={setSelectedConfirmations}
              setLog={setLog}
              runAction={runAction}
            />
          </section>
          <section className={`page ${activeTab === "settings" ? "active" : ""}`} id="section-settings">
            <Settings
              configPayload={snapshot.configPayload}
              service={snapshot.service}
              scheduler={snapshot.scheduler}
              reviewAutomation={snapshot.reviewAutomation}
              models={snapshot.models}
              reloadConfig={loadConfig}
              refreshModels={refreshModels}
              refreshAll={() => refreshAll()}
              notify={notify}
              schedulerDraft={schedulerDraft}
            />
          </section>
            </div>
      </AppShell>
      <Onboarding notify={notify} />
      {toast && <div id="toast" className="toast">{toast}</div>}
    </>
  );
}

interface ClipsProps {
  phase: string;
  setPhase(value: string): void;
  runs: Run[];
  detail: GenericRecord | null;
  selectedRunId: string | null;
  selectRun(value: string): void;
  setActiveTab(value: TabId): void;
  setLog(value: string): void;
  notify(message: string): void;
  refreshAll(): Promise<void>;
  pollAiReviewJob(runId: string, jobId: string): Promise<void>;
  runAction(path: string, payload?: unknown): Promise<GenericRecord>;
}

function Clips(props: ClipsProps) {
  const { phase, setPhase, runs, detail, selectedRunId, selectRun, setActiveTab, setLog, notify, refreshAll, pollAiReviewJob, runAction } = props;
  const clipCount = runs.reduce((total, run) => total + Number(run.clip_count || 0), 0);
  const processingCount = runs.filter((run) => ["processing", "rendering", "running", "ready_to_render"].includes(String(run.phase))).length;
  const reviewCount = runs.filter((run) => canonicalPhase(run.phase) === "needs_review").length;
  const parts = [
    clipCount ? `生成 ${clipCount} 个成片` : "",
    processingCount ? `${processingCount} 场正在处理` : "",
    reviewCount ? `${reviewCount} 场待审阅` : "",
  ].filter(Boolean);
  const subtitle = runs.length ? `AI 已从 ${runs.length} 场直播中 ${parts.length ? parts.join("，") : "整理处理状态"}` : "查看录播处理、AI 审阅和成片结果";
  return (
    <>
      <div className="page-heading">
        <div><h2>切片结果</h2><p id="clipsSubtitle" className="muted">{subtitle}</p></div>
        <div className="button-row">
          <button id="refreshBtn" className="secondary-button" onClick={() => void refreshAll()} type="button">刷新</button>
          <button id="scanNowBtn" className="primary-button" onClick={() => void runAction("/api/service/scan-now")} type="button">立即扫描录播</button>
        </div>
      </div>
      <div className="segmented" id="phaseFilters" aria-label="任务阶段筛选">
        {[["", "全部"], ["processing", "处理中"], ["needs_review", "待审阅"], ["rendered", "已成片"], ["failed", "失败"]].map(([id, label]) => (
          <button className={phase === id ? "active" : ""} data-phase={id} onClick={() => setPhase(id)} key={id} type="button">{label}</button>
        ))}
      </div>
      <div id="runList" className="run-list">
        {!runs.length && <div className="empty">还没有录播任务。可以先点击「立即扫描录播」。</div>}
        {runs.map((run) => {
          const active = run.run_id === selectedRunId;
          return (
            <article className={`clip-card ${active ? "active" : ""} ${run.stuck ? "stuck" : ""}`} key={run.run_id}>
              <button className="clip-card-main" data-run-id={run.run_id} onClick={() => selectRun(run.run_id)} type="button">
                <span><span className="run-title">{run.source_name || run.run_id}</span><span className="run-meta">{[run.updated_at || run.created_at || "-", `${Number(run.clip_count || 0)} 个成片`, `${Number(run.candidate_count || 0)} 个候选`, labelFor(run.phase)].join(" · ")}</span></span>
                <span className={`status-pill ${canonicalPhase(run.phase)}`}>{labelFor(run.phase)}</span>
              </button>
              {run.stuck && <div className="run-stuck-notice">⚠️ 已处理较长时间仍未完成，可能已卡住。请点击展开后「查看日志」，或重启本机服务后重试。</div>}
              {active && detail?.ok && (
                <RunExpanded
                  detail={detail}
                  setActiveTab={setActiveTab}
                  setLog={setLog}
                  notify={notify}
                  pollAiReviewJob={pollAiReviewJob}
                  runAction={runAction}
                />
              )}
            </article>
          );
        })}
      </div>
      <div id="runDetail" className="visually-hidden" aria-hidden="true">
        {detail?.ok && <InfoRows rows={[
          ["任务", detail.run?.run_id], ["阶段", labelFor(detail.run?.phase)], ["源文件", detail.run?.source_path],
          ["本地副本", detail.run?.local_source_path], ["任务目录", detail.run?.run_dir],
          ["候选数", detail.run?.candidate_count || detail.candidates_count || 0],
          ["已选片段", detail.run?.selected_count || detail.selected_count || 0],
          ["成片数", detail.run?.clip_count || detail.rendered_clip_count || 0],
        ]} />}
      </div>
    </>
  );
}

function RunExpanded({
  detail,
  setActiveTab,
  setLog,
  notify,
  pollAiReviewJob,
  runAction,
}: {
  detail: GenericRecord;
  setActiveTab(value: TabId): void;
  setLog(value: string): void;
  notify(message: string): void;
  pollAiReviewJob(runId: string, jobId: string): Promise<void>;
  runAction(path: string, payload?: unknown): Promise<GenericRecord>;
}) {
  const run = detail.run ?? {};
  const phase = canonicalPhase(run.phase);
  async function copyText(text: unknown) {
    if (!text) return notify("没有可复制的内容");
    try {
      await navigator.clipboard.writeText(String(text));
      notify("已复制");
    } catch {
      notify(String(text));
    }
  }
  if (phase === "rendered") {
    return (
      <div className="clip-card-body">
        <div className="clip-actions">
          <span className="run-meta">{String(run.run_dir || "-")}</span>
          <button className="secondary-button small" onClick={() => void copyText(run.run_dir)} type="button">复制目录</button>
          <button id="cleanupPreviewBtn" className="secondary-button small" disabled={!detail.actions?.can_cleanup_preview} onClick={() => void runAction(`/api/runs/${encodeURIComponent(run.run_id)}/cleanup-preview`).then((result) => { setLog(JSON.stringify(result, null, 2)); setActiveTab("automation"); })} type="button">预览清理</button>
        </div>
        {detail.clips?.length ? detail.clips.map((clip: GenericRecord) => (
          <div className="clip-row" key={String(clip.path || clip.title || clip.name)}>
            <div className="clip-thumb">
              {clip.media_url
                ? <video src={String(clip.media_url)} aria-label={String(clip.title || clip.name || "成片")} controls preload="metadata" />
                : "▶"}
            </div>
            <div><div className="clip-title">{String(clip.title || clip.name || "未命名成片")}</div><div className="clip-desc">{String(clip.description || clip.desc || clip.path || "")}</div></div>
            <div className="clip-actions">
              <button className="secondary-button small" onClick={() => void copyText(clip.title || clip.name)} type="button">复制标题</button>
              <button className="secondary-button small" onClick={() => void copyText(clip.description || clip.desc || clip.path)} type="button">复制简介</button>
              <span className="run-meta">{formatBytes(clip.bytes)}</span>
            </div>
          </div>
        )) : <div className="empty">已进入成片阶段，但还没有检测到 clips/*.mp4。</div>}
      </div>
    );
  }
  if (phase === "needs_review") {
    const reviewing = detail.active_job?.status === "running";
    return (
      <div className="clip-card-body">
        {detail.ai_review?.status === "failed" && <div className="notice error" style={{ marginBottom: 12 }}>上次 AI 审阅失败：{String(detail.ai_review.error || "未知错误")}</div>}
        <div className="clip-actions">
          <p className="muted" style={{ flex: 1 }}>AI 已找到 <strong>{String(run.candidate_count || detail.candidates_count || 0)}</strong> 个候选片段，审阅后即可渲染成片。</p>
          <button className="secondary-button small" onClick={() => void copyText(run.run_dir)} type="button">复制审阅包路径</button>
          <button id="aiReviewRunBtn" className="primary-button small" disabled={reviewing || !detail.actions?.can_ai_review} onClick={() => void post<GenericRecord>(`/api/runs/${encodeURIComponent(run.run_id)}/ai-review`).then((payload) => { notify("AI 审阅已开始，正在后台处理…"); void pollAiReviewJob(String(run.run_id), String(payload.job?.id || "")); }).catch((error) => notify(`AI 审阅启动失败：${(error as Error).message}`))} type="button">{reviewing ? "AI 审阅中…" : "立即 AI 审阅"}</button>
          <button id="renderRunBtn" className="secondary-button small" disabled={!detail.actions?.can_render} onClick={() => void runAction(`/api/runs/${encodeURIComponent(run.run_id)}/render`)} type="button">渲染</button>
        </div>
      </div>
    );
  }
  if (phase === "failed") {
    return <div className="clip-card-body"><div className="notice error">{String(run.last_error || "任务失败，暂无错误详情。")}</div><div className="button-row" style={{ marginTop: 12 }}><button className="secondary-button small" onClick={() => setActiveTab("automation")} type="button">查看日志</button></div></div>;
  }
  const steps = detail.steps?.length ? detail.steps : [
    { label: "拉取录像", state: "done" }, { label: "语音转写", state: "active" },
    { label: "AI 选片", state: "waiting" }, { label: "渲染", state: "waiting" },
  ];
  return (
    <div className="clip-card-body"><div className="clip-actions">
      {steps.map((step: GenericRecord) => <span className={`status-pill ${step.done || step.state === "done" ? "rendered" : step.state === "active" ? "processing" : ""}`} key={String(step.label)}>{String(step.label)}</span>)}
      <button className="secondary-button small" onClick={() => setActiveTab("automation")} type="button">查看日志</button>
    </div></div>
  );
}

function Automation({
  service,
  scheduler,
  reviewAutomation,
  events,
  log,
  setLog,
  notify,
  runAction,
  editSchedulerJob,
}: {
  service: GenericRecord;
  scheduler: GenericRecord | null;
  reviewAutomation: GenericRecord | null;
  events: GenericRecord[];
  log: string;
  setLog(value: string): void;
  notify(message: string): void;
  runAction(path: string, payload?: unknown): Promise<GenericRecord>;
  editSchedulerJob(job: GenericRecord): void;
}) {
  const serviceState = service.service ?? {};
  const source = service.source_summary ?? {};
  const schedulerState = scheduler?.scheduler ?? {};
  const review = reviewAutomation?.review_automation ?? {};
  const environment = reviewAutomation?.environment ?? {};
  const [actionStatus, setActionStatus] = useState("");
  async function reviewAction(path: string) {
    const result = await runAction(path);
    if (path.endsWith("/check")) {
      notify(result.current_mode_available ? "AI 审阅环境可用。" : "当前 AI 审阅环境不可用，请检查 Codex CLI、Claude Code 或 LLM API key。");
      return;
    }
    const message = result.skipped_reason === "review_automation_disabled"
      ? "自动 AI 审阅还没有启用。请到「设置」页打开「启用自动 AI 审阅」，保存配置后再执行。"
      : result.processed_runs?.length ? `已处理 ${result.processed_runs.length} 个待审阅任务。`
      : result.results?.length ? `AI 审阅已返回 ${result.results.length} 条结果，请查看运行日志。`
      : String(result.message || "当前没有待审阅任务。");
    setActionStatus(message);
    setLog(JSON.stringify(result, null, 2));
    notify(message);
  }
  return (
    <>
      <div className="page-heading">
        <div><h2>自动化</h2><p className="muted">定时任务、AI 审阅与运行日志</p></div>
        <div className="button-row">
          <button id="startServiceBtn" className="secondary-button" onClick={() => void runAction("/api/service/start")} type="button">恢复自动化</button>
          <button id="stopServiceBtn" className="secondary-button" onClick={() => void runAction("/api/service/stop")} type="button">暂停自动化</button>
        </div>
      </div>
      <section className="content-card">
        <div className="card-heading"><div><h3>自动化引擎</h3><p className="muted">随 App 一起运行：App 开着（或缩在菜单栏）就会按时间表自动处理新录播。</p></div></div>
        <div id="serviceMetrics" className="metrics-grid"><MetricRows rows={[
          ["状态", labelFor(serviceState.status || "stopped")], ["PID", serviceState.pid || "无"],
          ["待审阅", service.pending_review_runs?.length || 0], ["失败", service.failed_runs?.length || 0],
          ["待确认", service.pending_confirmation_count || 0], ["当前任务", service.active_run || "无"],
        ]} /></div>
        <div id="serviceSummary" className="info-grid"><InfoRows rows={[
          ["最近心跳", serviceState.last_heartbeat_at], ["下次扫描", serviceState.next_scan_at],
          ["录播源", source.source_dir], ["输入目录", source.input_dir], ["输出目录", source.output_root],
          ["最近错误", serviceState.last_error],
        ]} /></div>
      </section>
      <section className="content-card">
        <div className="card-heading"><div><h3>定时任务</h3><p className="muted">默认每周扫描录播，并提醒待审阅任务。</p></div></div>
        <div id="schedulerSummary" className="scheduler-summary"><InfoRows rows={[
          ["Scheduler 状态", schedulerState.enabled ? "运行中" : "未启用"], ["调度时区", schedulerState.timezone],
          ["当前系统时间", schedulerState.current_time], ["下一次任务", schedulerState.next_due_job_id],
          ["下次执行", schedulerState.next_due_at],
        ]} /></div>
        <div id="schedulerJobList" className="scheduler-jobs">
          {scheduler?.jobs?.length ? scheduler.jobs.map((job: GenericRecord) => (
            <div className="scheduler-job" key={String(job.id)}>
              <div><strong>{String(job.name || job.id)}</strong><small>{String(job.id)} · {labelFor(job.type)} · {labelFor(job.schedule)}</small><small>下次执行：{String(job.next_run_at || "-")} · 上次结果：{labelFor(job.status)}</small></div>
              <div className="button-row">
                <button className="secondary-button small" onClick={() => editSchedulerJob(job)} type="button">编辑</button>
                <button className="primary-button small" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/run-now`)} type="button">立即执行</button>
                <button className="secondary-button small" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/pause`)} type="button">暂停</button>
                <button className="secondary-button small" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/resume`)} type="button">启用</button>
              </div>
            </div>
          )) : <div className="empty">还没有定时任务。</div>}
        </div>
      </section>
      <section className="content-card">
        <div className="card-heading"><div><h3>AI 自动审阅</h3><p className="muted">默认不会静默开启，需要先在设置中启用。</p></div><div className="button-row">
          <button id="checkReviewAutomationBtn" className="secondary-button" onClick={() => void reviewAction("/api/review-automation/check")} type="button">测试 AI 审阅环境</button>
          <button id="runDueReviewAutomationBtn" className="primary-button" onClick={() => void reviewAction("/api/review-automation/run-due")} type="button">立即处理待审阅</button>
        </div></div>
        <div id="reviewAutomationStatus" className="scheduler-summary"><InfoRows rows={[
          ["AI 审阅状态", review.enabled ? "已启用" : "未启用"], ["审阅方式", labelFor(review.mode)],
          ["Provider", labelFor(review.provider)], ["Codex CLI", environment.codex_cli?.available ? "可用" : "未检测到"],
          ["Claude Code", environment.claude_code?.available ? "可用" : "未检测到"],
          ["LLM API key", environment.llm?.api_key_configured ? "已配置" : "未配置"],
          ["最近结果", labelFor(review.last_status)], ["最近错误", review.last_error],
        ]} /></div>
        {actionStatus && <div id="reviewAutomationActionStatus" className="notice subtle">{actionStatus}</div>}
      </section>
      <section className="content-card">
        <div className="card-heading"><div><h3>运行日志</h3><p className="muted">最近事件和当前选中任务日志。</p></div><button id="clearLogBtn" className="secondary-button" onClick={() => setLog("")} type="button">清空前端显示</button></div>
        <div id="eventStream" className="event-list">{events.length ? events.map((event) => <div className="event-row" key={String(event.id || `${event.created_at}-${event.type}`)}><strong>{String(event.type)}</strong><small>{String(event.created_at)} · {String(event.run_id || "-")}</small></div>) : <div className="empty">暂无事件。</div>}</div>
        <pre id="logOutput" className="log-output">{log}</pre>
      </section>
    </>
  );
}

function Confirmations({
  pending,
  selected,
  setSelected,
  setLog,
  runAction,
}: {
  pending: GenericRecord[];
  selected: string[];
  setSelected(value: string[]): void;
  setLog(value: string): void;
  runAction(path: string, payload?: unknown): Promise<GenericRecord>;
}) {
  const toggle = (id: string, checked: boolean) => setSelected(checked ? [...selected, id] : selected.filter((item) => item !== id));
  return (
    <>
      <div className="page-heading">
        <div><h2>确认</h2><p className="muted">以下操作会删除文件，需要你手动确认后才会执行。</p></div>
        <div className="button-row">
          <button id="batchApproveBtn" className="danger-button" onClick={() => void runAction("/api/confirmations/batch-approve", { ids: selected }).then(() => setLog(`批量确认: ${selected.join(", ") || "无"}`))} type="button">确认所选</button>
          <button id="batchRejectBtn" className="secondary-button" onClick={() => void runAction("/api/confirmations/batch-reject", { ids: selected, reason: "在 Web 控制台批量拒绝" }).then(() => setLog(`批量拒绝: ${selected.join(", ") || "无"}`))} type="button">拒绝所选</button>
        </div>
      </div>
      <div id="confirmationList" className="confirmation-list">
        {!pending.length && <div className="empty">没有待确认的操作</div>}
        {pending.map((item) => (
          <article className="confirmation-row" key={String(item.id)}>
            <input type="checkbox" checked={selected.includes(String(item.id))} onChange={(event) => toggle(String(item.id), event.target.checked)} />
            <div><strong>{labelFor(item.action)}</strong><small>{String(item.id)} · 任务 {String(item.run_id)}</small><small>{String(item.target_path || "")}</small><small>{String(item.reason || item.message || "")}</small></div>
            <span className={`risk ${String(item.risk_level)}`}>{labelFor(item.risk_level)}</span>
            <div className="button-row">
              <button className="secondary-button small" onClick={() => void runAction(`/api/confirmations/${item.id}/reject`, { reason: "在 Web 控制台拒绝" })} type="button">拒绝</button>
              <button className="danger-button small" onClick={() => void runAction(`/api/confirmations/${item.id}/approve`)} type="button">确认执行</button>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}
