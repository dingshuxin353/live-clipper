import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppShell, useAppShellMobile } from "@astryxdesign/core/AppShell";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { InternationalizationProvider } from "@astryxdesign/core/i18n";
import { List, ListItem } from "@astryxdesign/core/List";
import { Spinner } from "@astryxdesign/core/Spinner";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
} from "@astryxdesign/core/SideNav";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { Toast } from "@astryxdesign/core/Toast";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";

import { api, post } from "./api";
import { Onboarding } from "./Onboarding";
import { Settings } from "./Settings";
import type { AppSnapshot, GenericRecord, Model, Run, TabId } from "./types";
import { formatLocalTime, semanticToneStyles, type SemanticTone } from "./ui/presentation";

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

const VENUS_I18N_OVERRIDES = {
  "zh-Hans": {
    "@astryx.appShell.mobileNavigation": "移动导航",
    "@astryx.dialog.close": "关闭",
    "@astryx.mobileNav.closeNavigation": "关闭导航",
    "@astryx.mobileNav.navigation": "导航菜单",
    "@astryx.mobileNav.toggle.open": "打开导航",
    "@astryx.toast.dismiss": "关闭通知",
  },
};

const phaseLabels: Record<string, string> = {
  queued: "排队中",
  processing: "处理中",
  rendering: "渲染中",
  needs_review: "待审阅",
  rendered: "已成片",
  failed: "失败",
  cleanup_ready: "已成片",
  ready_to_render: "可渲染",
  needs_codex_selection: "待审阅",
  running: "运行中",
  degraded: "服务异常，正在重试",
  paused: "已暂停",
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
  failed: "失败",
  overdue: "已逾期",
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
        <div className="info-row" key={label}><span>{label}</span><strong className="technical-value" title={String(value || "-")}>{String(value || "-")}</strong></div>
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
  return (
    <InternationalizationProvider locale="zh-Hans" overrides={VENUS_I18N_OVERRIDES}>
      <AppContent />
    </InternationalizationProvider>
  );
}

function VenusNavigationItems({
  activeTab,
  pendingCount,
  setActiveTab,
  tabs,
}: {
  activeTab: TabId;
  pendingCount: number;
  setActiveTab: (tab: TabId) => void;
  tabs: Array<[TabId, string]>;
}) {
  const { isMobileNavOpen } = useAppShellMobile();
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isMobileNavOpen) {
      contentRef.current?.scrollIntoView({ block: "start" });
    }
  }, [isMobileNavOpen]);

  return (
    <div className="venus-navigation-items" ref={contentRef}>
      <SideNavSection title="工作台" isHeaderHidden>
        {tabs.map(([id, label]) => (
          <SideNavItem
            endContent={id === "confirmations" && pendingCount > 0 ? <Badge id="confirmationBadge" label={pendingCount} /> : undefined}
            isSelected={activeTab === id}
            key={id}
            label={label}
            onClick={() => setActiveTab(id)}
          />
        ))}
      </SideNavSection>
    </div>
  );
}

function AppContent() {
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
  const [scanning, setScanning] = useState(false);
  const mounted = useRef(true);
  const pollingJobs = useRef(new Set<string>());
  const scanningRef = useRef(false);

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
          notify(job.result?.status === "selection_empty"
            ? "AI 审阅完成，但未选出可用片段；你可以重新审阅或手工选片。"
            : `AI 审阅完成，已选 ${String(job.result?.selected_count ?? "?")} 个片段`);
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

  async function runAction(path: string, payload?: unknown, showToast = true) {
    try {
      const result = await post<GenericRecord>(path, payload);
      if (result.status === "confirmation_required") setLog(`需要确认: ${String(result.confirmation_id)}`);
      if (showToast) notify(String(result.message || "操作已完成"));
      await refreshAll();
      return result;
    } catch (error) {
      if (showToast) notify((error as Error).message);
      throw error;
    }
  }

  async function scanNow() {
    if (scanningRef.current) return;
    scanningRef.current = true;
    setScanning(true);
    try {
      await runAction("/api/service/scan-now");
    } finally {
      scanningRef.current = false;
      if (mounted.current) setScanning(false);
    }
  }

  const pending = snapshot.confirmations.filter((item) => item.status === "pending");
  const tabs: Array<[TabId, string]> = [
    ["clips", "切片结果"],
    ["automation", "自动化"],
    ["confirmations", "文件清理"],
    ["settings", "设置"],
  ];
  const service = snapshot.service ?? {};
  const schedulerState = snapshot.scheduler?.scheduler ?? {};
  const running = Boolean(service.running);
  const serviceStatus = String(service.service?.status || "stopped");
  const serviceStatusLabel = serviceStatus === "degraded" ? "服务异常，正在重试" : labelFor(serviceStatus);

  return (
    <>
      <AppShell
        className="venus-app-shell"
        contentPadding={0}
        height="fill"
        mobileNav={{ breakpoint: "sm" }}
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
            <strong>{serviceStatusLabel}</strong>
            <small>PID：{String(service.service?.pid || "无")}</small>
            <small>下次扫描：{formatLocalTime(service.service?.next_scan_at)}</small>
            <small>下次定时：{formatLocalTime(schedulerState.next_due_at)}</small>
                <div className="sidebar-service-actions">
                  <Button isDisabled={scanning} label={scanning ? "扫描中…" : "立即扫描"} size="sm" variant="secondary" onClick={() => void scanNow().catch(() => undefined)} />
                  <Button label={serviceStatus === "paused" ? "恢复" : running ? "停止" : "启动"} size="sm" variant="secondary" onClick={() => void runAction(serviceStatus === "paused" ? "/api/service/start" : running ? "/api/service/stop" : "/api/service/start")} />
                </div>
              </div>
            )}
          >
            <VenusNavigationItems
              activeTab={activeTab}
              pendingCount={pending.length}
              setActiveTab={setActiveTab}
              tabs={tabs}
            />
          </SideNav>
        )}
        variant="elevated"
      >
        <div className="main-content">
          {loadError && <Text as="div" role="alert" type="supporting" xstyle={semanticToneStyles.error}>{`初次加载失败：${loadError}`}</Text>}
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
              scanNow={scanNow}
              scanning={scanning}
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
      {toast && (
        <div className="venus-toast" id="toast">
          <Toast
            autoHideDuration={3600}
            body={toast}
            isAutoHide={false}
            onDismiss={() => setToast("")}
            type={/失败|错误|不可用/.test(toast) ? "error" : "info"}
          />
        </div>
      )}
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
  runAction(path: string, payload?: unknown, showToast?: boolean): Promise<GenericRecord>;
  scanNow(): Promise<void>;
  scanning: boolean;
}

function Clips(props: ClipsProps) {
  const { phase, setPhase, runs, detail, selectedRunId, selectRun, setActiveTab, setLog, notify, refreshAll, pollAiReviewJob, runAction, scanNow, scanning } = props;
  const clipCount = runs.reduce((total, run) => total + Number(run.clip_count || 0), 0);
  const queuedCount = runs.filter((run) => String(run.phase) === "queued").length;
  const processingCount = runs.filter((run) => ["processing", "rendering", "running", "ready_to_render"].includes(String(run.phase))).length;
  const reviewCount = runs.filter((run) => canonicalPhase(run.phase) === "needs_review").length;
  const parts = [
    clipCount ? `生成 ${clipCount} 个成片` : "",
    queuedCount ? `${queuedCount} 场排队中` : "",
    processingCount ? `${processingCount} 场正在处理` : "",
    reviewCount ? `${reviewCount} 场待审阅` : "",
  ].filter(Boolean);
  const subtitle = runs.length ? `AI 已从 ${runs.length} 场直播中 ${parts.length ? parts.join("，") : "整理处理状态"}` : "查看录播处理、AI 审阅和成片结果";
  return (
    <>
      <div className="page-heading">
        <div><h2>切片结果</h2><p id="clipsSubtitle" className="muted">{subtitle}</p></div>
        <div className="button-row">
          <Button id="refreshBtn" label="刷新" onClick={() => void refreshAll()} />
          <Button id="scanNowBtn" isDisabled={scanning} label={scanning ? "扫描中…" : "立即扫描录播"} onClick={() => void scanNow().catch(() => undefined)} variant="primary" />
        </div>
      </div>
      <TabList aria-label="任务阶段筛选" className="phase-filters" id="phaseFilters" onChange={setPhase} size="sm" value={phase}>
        {[["", "全部"], ["queued", "排队中"], ["processing", "处理中"], ["needs_review", "待审阅"], ["rendered", "已成片"], ["failed", "失败"]].map(([id, label]) => (
          <Tab key={id} label={label} value={id} />
        ))}
      </TabList>
      {!runs.length && <EmptyState description="可以先点击「立即扫描录播」。" title="还没有录播任务" />}
      <List className="run-list" density="compact" hasDividers id="runList">
        {runs.map((run) => {
          const active = run.run_id === selectedRunId;
          return (
            <ListItem
              className={`run-row ${run.stuck ? "stuck" : ""}`}
              data-run-id={run.run_id}
              description={(
                <div className="run-row-detail">
                  <span className="run-meta">
                    {[formatLocalTime(run.updated_at || run.created_at), `${Number(run.clip_count || 0)} 个成片`, `${Number(run.candidate_count || 0)} 个候选`].join(" · ")}
                  </span>
                  {run.stuck && (
                    <div className="status-copy">
                      <Text as="div" role="alert" type="supporting" xstyle={semanticToneStyles.warning}>已处理较长时间仍未完成，可能已卡住。</Text>
                      <Text as="div" type="supporting" xstyle={semanticToneStyles.warning}>请展开查看日志，或重启本机服务后重试。</Text>
                    </div>
                  )}
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
                </div>
              )}
              endContent={<Badge label={labelFor(run.phase)} />}
              isSelected={active}
              key={run.run_id}
              label={<span className="run-title">{run.source_name || run.run_id}</span>}
              onClick={() => selectRun(run.run_id)}
            />
          );
        })}
      </List>
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
  runAction(path: string, payload?: unknown, showToast?: boolean): Promise<GenericRecord>;
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
          <span className="run-meta technical-value" title={String(run.run_dir || "-")}>{String(run.run_dir || "-")}</span>
          <Button label="复制目录" onClick={() => void copyText(run.run_dir)} size="sm" />
          <Button id="cleanupPreviewBtn" isDisabled={!detail.actions?.can_cleanup_preview} label="预览清理" onClick={() => void runAction(`/api/runs/${encodeURIComponent(run.run_id)}/cleanup-preview`).then((result) => { setLog(JSON.stringify(result, null, 2)); setActiveTab("automation"); })} size="sm" />
        </div>
        {detail.clips?.length ? (
          <List className="clip-rows" density="compact" hasDividers>
            {detail.clips.map((clip: GenericRecord) => (
              <ListItem
                description={<span className="technical-value" title={String(clip.description || clip.desc || clip.path || "")}>{String(clip.description || clip.desc || clip.path || "")}</span>}
                endContent={(
                  <div className="clip-actions">
                    <Button label="复制标题" onClick={() => void copyText(clip.title || clip.name)} size="sm" />
                    <Button label="复制简介" onClick={() => void copyText(clip.description || clip.desc || clip.path)} size="sm" />
                    <span className="run-meta">{formatBytes(clip.bytes)}</span>
                  </div>
                )}
                key={String(clip.path || clip.title || clip.name)}
                label={String(clip.title || clip.name || "未命名成片")}
                startContent={(
            <div className="clip-thumb">
              {clip.media_url
                ? <video src={String(clip.media_url)} aria-label={String(clip.title || clip.name || "成片")} controls preload="metadata" />
                      : null}
            </div>
                )}
              />
            ))}
          </List>
        ) : <EmptyState isCompact title="已进入成片阶段，但还没有检测到 clips/*.mp4。" />}
      </div>
    );
  }
  if (phase === "needs_review") {
    const reviewing = detail.active_job?.status === "running";
    return (
      <div className="clip-card-body">
        {detail.ai_review?.status === "failed" && <Text as="div" role="alert" type="supporting" xstyle={semanticToneStyles.error}>{`上次 AI 审阅失败：${String(detail.ai_review.error || "未知错误")}`}</Text>}
        <div className="clip-actions">
          <p className="muted" style={{ flex: 1 }}>AI 已找到 <strong>{String(run.candidate_count || detail.candidates_count || 0)}</strong> 个候选片段，审阅后即可渲染成片。</p>
          <Button label="复制审阅包路径" onClick={() => void copyText(run.run_dir)} size="sm" />
          <Button
            data-busy={reviewing ? "true" : undefined}
            icon={reviewing ? <Spinner aria-hidden="true" aria-label="AI 审阅中…" shade="inherit" size="sm" /> : undefined}
            id="aiReviewRunBtn"
            isDisabled={reviewing || !detail.actions?.can_ai_review}
            label={reviewing ? "AI 审阅中…" : "立即 AI 审阅"}
            onClick={() => void post<GenericRecord>(`/api/runs/${encodeURIComponent(run.run_id)}/ai-review`).then((payload) => { notify("AI 审阅已开始，正在后台处理…"); void pollAiReviewJob(String(run.run_id), String(payload.job?.id || "")); }).catch((error) => notify(`AI 审阅启动失败：${(error as Error).message}`))}
            size="sm"
            variant="primary"
          />
          <VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">
            {reviewing ? "AI 审阅正在进行" : ""}
          </VisuallyHidden>
          <Button id="renderRunBtn" isDisabled={!detail.actions?.can_render} label="渲染" onClick={() => void runAction(`/api/runs/${encodeURIComponent(run.run_id)}/render`)} size="sm" />
        </div>
      </div>
    );
  }
  if (phase === "failed") {
    return <div className="clip-card-body"><Text as="div" role="alert" type="supporting" xstyle={semanticToneStyles.error}>{String(run.last_error || "任务失败，暂无错误详情。")}</Text><div className="button-row"><Button label="重试处理" onClick={() => void runAction(`/api/runs/${encodeURIComponent(run.run_id)}/retry`).catch(() => undefined)} size="sm" variant="primary" /><Button label="查看日志" onClick={() => setActiveTab("automation")} size="sm" /></div></div>;
  }
  if (phase === "queued") {
    return <div className="clip-card-body"><Text as="div" type="supporting" xstyle={semanticToneStyles.info}>已进入处理队列，将在当前任务完成后自动开始。</Text></div>;
  }
  const steps = detail.steps?.length ? detail.steps : [
    { label: "拉取录像", state: "done" }, { label: "语音转写", state: "active" },
    { label: "AI 选片", state: "waiting" }, { label: "渲染", state: "waiting" },
  ];
  return (
    <div className="clip-card-body"><div className="clip-actions">
      {steps.map((step: GenericRecord) => <Badge key={String(step.label)} label={String(step.label)} />)}
      <Button label="查看日志" onClick={() => setActiveTab("automation")} size="sm" />
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
  runAction(path: string, payload?: unknown, showToast?: boolean): Promise<GenericRecord>;
  editSchedulerJob(job: GenericRecord): void;
}) {
  const serviceState = service.service ?? {};
  const source = service.source_summary ?? {};
  const schedulerState = scheduler?.scheduler ?? {};
  const review = reviewAutomation?.review_automation ?? {};
  const environment = reviewAutomation?.environment ?? {};
  const [actionStatus, setActionStatus] = useState<{ message: string; tone: SemanticTone } | null>(null);
  async function reviewAction(path: string) {
    try {
      const result = await runAction(path, undefined, false);
      if (path.endsWith("/check")) {
        setActionStatus(result.current_mode_available
          ? { message: "AI 审阅环境可用。", tone: "success" }
          : { message: "当前 AI 审阅环境不可用，请检查 Codex CLI、Claude Code 或 LLM API key。", tone: "warning" });
        return;
      }
      const message = result.skipped_reason === "review_automation_disabled"
        ? "自动 AI 审阅还没有启用。请到「设置」页打开「启用自动 AI 审阅」，保存配置后再执行。"
        : result.processed_runs?.length ? `已处理 ${result.processed_runs.length} 个待审阅任务。`
        : result.results?.length ? `AI 审阅已返回 ${result.results.length} 条结果，请查看运行日志。`
        : String(result.message || "当前没有待审阅任务。");
      setActionStatus({
        message,
        tone: result.skipped_reason === "review_automation_disabled" ? "warning" : "success",
      });
      setLog(JSON.stringify(result, null, 2));
    } catch (error) {
      setActionStatus({ message: (error as Error).message, tone: "error" });
    }
  }
  return (
    <>
      <div className="page-heading">
        <div><h2>自动化</h2><p className="muted">定时任务、AI 审阅与运行日志</p></div>
        <div className="button-row">
          <Button id="startServiceBtn" label="恢复自动化" onClick={() => void runAction("/api/service/start")} />
          <Button id="stopServiceBtn" label="暂停自动化" onClick={() => void runAction("/api/service/stop")} />
        </div>
      </div>
      <Card className="content-card" padding={4}>
        <div className="card-heading"><div><h3>自动化引擎</h3><p className="muted">随 App 一起运行：App 开着（或缩在菜单栏）就会按时间表自动处理新录播。</p></div></div>
        <div id="serviceMetrics" className="metrics-grid"><MetricRows rows={[
          ["状态", labelFor(serviceState.status || "stopped")], ["PID", serviceState.pid || "无"],
          ["待审阅", service.pending_review_runs?.length || 0], ["失败", service.failed_runs?.length || 0],
          ["待确认", service.pending_confirmation_count || 0], ["当前任务", service.active_run || "无"],
        ]} /></div>
        <div id="serviceSummary" className="info-grid service-summary-grid"><InfoRows rows={[
          ["最近心跳", serviceState.last_heartbeat_at], ["最近成功", serviceState.last_successful_tick_at],
          ["最近错误时间", serviceState.last_error_at], ["下次重试", serviceState.next_retry_at],
          ["下次扫描", serviceState.next_scan_at],
          ["录播源", source.source_dir], ["输入目录", source.input_dir], ["输出目录", source.output_root],
          ["最近错误", serviceState.last_error],
        ]} /></div>
      </Card>
      <Card className="content-card" padding={4}>
        <div className="card-heading"><div><h3>定时任务</h3><p className="muted">默认每周扫描录播，并提醒待审阅任务。</p></div></div>
        <div id="schedulerSummary" className="scheduler-summary"><InfoRows rows={[
          ["Scheduler 状态", schedulerState.enabled ? "运行中" : "未启用"], ["调度时区", schedulerState.timezone],
          ["当前系统时间", formatLocalTime(schedulerState.current_time)], ["下一次任务", schedulerState.next_due_job_id],
          ["下次执行", formatLocalTime(schedulerState.next_due_at)],
        ]} /></div>
        <List className="scheduler-jobs" density="compact" hasDividers id="schedulerJobList">
          {scheduler?.jobs?.length ? scheduler.jobs.map((job: GenericRecord) => (
            <ListItem
              className="scheduler-job"
              description={(
                <span className="scheduler-job-description">
                  {`${String(job.id)} · ${labelFor(job.type)} · ${labelFor(job.schedule)} · 下次执行：${formatLocalTime(job.next_run_at)} · 上次结果：${labelFor(job.status)}`}
                </span>
              )}
              endContent={(
                <div className="button-row scheduler-job-actions">
                  <Button label="编辑" onClick={() => editSchedulerJob(job)} size="sm" />
                  <Button label="立即执行" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/run-now`)} size="sm" variant="primary" />
                  <Button label="暂停" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/pause`)} size="sm" />
                  <Button label="启用" onClick={() => void runAction(`/api/scheduler/jobs/${encodeURIComponent(job.id)}/resume`)} size="sm" />
                </div>
              )}
              key={String(job.id)}
              label={String(job.name || job.id)}
            />
          )) : <ListItem label="还没有定时任务。" />}
        </List>
      </Card>
      <Card className="content-card" padding={4}>
        <div className="card-heading"><div><h3>AI 自动审阅</h3><p className="muted">默认不会静默开启，需要先在设置中启用。</p></div><div className="button-row">
          <Button id="checkReviewAutomationBtn" label="测试 AI 审阅环境" onClick={() => void reviewAction("/api/review-automation/check")} />
          <Button id="runDueReviewAutomationBtn" label="立即处理待审阅" onClick={() => void reviewAction("/api/review-automation/run-due")} variant="primary" />
        </div></div>
        <div id="reviewAutomationStatus" className="scheduler-summary"><InfoRows rows={[
          ["AI 审阅状态", review.enabled ? "已启用" : "未启用"], ["审阅方式", labelFor(review.mode)],
          ["Provider", labelFor(review.provider)], ["Codex CLI", environment.codex_cli?.available ? "可用" : "未检测到"],
          ["Claude Code", environment.claude_code?.available ? "可用" : "未检测到"],
          ["LLM API key", environment.llm?.api_key_configured ? "已配置" : "未配置"],
          ["最近结果", labelFor(review.last_status)], ["最近错误", review.last_error],
        ]} /></div>
        {actionStatus && (
          <Text as="div" id="reviewAutomationActionStatus" role="status" type="supporting" xstyle={semanticToneStyles[actionStatus.tone]}>
            {actionStatus.message}
          </Text>
        )}
      </Card>
      <Card className="content-card" padding={4}>
        <div className="card-heading"><div><h3>运行日志</h3><p className="muted">最近事件和当前选中任务日志。</p></div><Button id="clearLogBtn" label="清空前端显示" onClick={() => setLog("")} /></div>
        <List className="event-list" density="compact" hasDividers id="eventStream">
          {events.length
            ? events.map((event) => <ListItem description={`${formatLocalTime(event.created_at)} · ${String(event.run_id || "-")}`} key={String(event.id || `${event.created_at}-${event.type}`)} label={String(event.type)} />)
            : <ListItem label="暂无事件。" />}
        </List>
        <pre id="logOutput" className="log-output">{log}</pre>
      </Card>
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
  runAction(path: string, payload?: unknown, showToast?: boolean): Promise<GenericRecord>;
}) {
  const toggle = (id: string, checked: boolean) => setSelected(checked ? [...selected, id] : selected.filter((item) => item !== id));
  const [confirmationAction, setConfirmationAction] = useState<{
    title: string;
    description: string;
    path: string;
    payload?: unknown;
    log: string;
  } | null>(null);
  async function confirmDangerousAction() {
    if (!confirmationAction) return;
    await runAction(confirmationAction.path, confirmationAction.payload);
    setLog(confirmationAction.log);
    setConfirmationAction(null);
  }
  return (
    <>
      <div className="page-heading">
        <div><h2>待确认的清理操作</h2><p className="muted">删除成片、中间文件或本地录像副本前，需要你确认。NAS 原始录像不会被删除。</p></div>
        <div className="button-row">
          <Button id="batchApproveBtn" isDisabled={!selected.length} label="确认所选" onClick={() => setConfirmationAction({ title: "确认执行所选操作？", description: "这些操作可能删除本地文件，执行后无法从 Venus 撤销。", path: "/api/confirmations/batch-approve", payload: { ids: selected }, log: `批量确认: ${selected.join(", ") || "无"}` })} variant="destructive" />
          <Button id="batchRejectBtn" isDisabled={!selected.length} label="拒绝所选" onClick={() => void runAction("/api/confirmations/batch-reject", { ids: selected, reason: "在 Web 控制台批量拒绝" }).then(() => setLog(`批量拒绝: ${selected.join(", ") || "无"}`))} />
        </div>
      </div>
      {!pending.length && <EmptyState title="没有待确认的清理操作" />}
      <List className="confirmation-list" density="compact" hasDividers id="confirmationList">
        {pending.map((item) => (
          <ListItem
            description={(
              <div className="confirmation-description">
                <span>{String(item.id)} · 任务 {String(item.run_id)}</span>
                <span className="technical-value" title={String(item.target_path || "")}>{String(item.target_path || "")}</span>
                <span>{String(item.reason || item.message || "")}</span>
              </div>
            )}
            endContent={(
              <div className="button-row">
                <Badge label={labelFor(item.risk_level)} />
                <Button label="拒绝" onClick={() => void runAction(`/api/confirmations/${item.id}/reject`, { reason: "在 Web 控制台拒绝" })} size="sm" />
                <Button label="确认执行" onClick={() => setConfirmationAction({ title: "确认执行该操作？", description: `${labelFor(item.action)}：${String(item.target_path || "未提供目标路径")}`, path: `/api/confirmations/${item.id}/approve`, log: `确认执行: ${String(item.id)}` })} size="sm" variant="destructive" />
              </div>
            )}
            key={String(item.id)}
            label={(
              <CheckboxInput
                label={labelFor(item.action)}
                onChange={(checked) => toggle(String(item.id), checked)}
                value={selected.includes(String(item.id))}
              />
            )}
          />
        ))}
      </List>
      <AlertDialog
        actionLabel="确认执行"
        actionVariant="destructive"
        cancelLabel="取消"
        description={confirmationAction?.description || ""}
        isOpen={Boolean(confirmationAction)}
        onAction={() => void confirmDangerousAction()}
        onOpenChange={(open) => { if (!open) setConfirmationAction(null); }}
        title={confirmationAction?.title || "确认操作"}
      />
    </>
  );
}
