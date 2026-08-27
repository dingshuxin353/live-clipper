import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import type { FormOptionsPayload, ProjectConfig, ProjectMainStatus, ProjectSummary, Run, RunStage, ScanEvent } from "./project-dto";
import { formatLocalTime } from "./ui/presentation";

export const DRAFT_KEY = "venus.project-draft.v1";
export const VIDEO_EXTENSIONS = [".m4v", ".mkv", ".mov", ".mp4", ".webm"];
export const STAGES: Array<[RunStage, string]> = [["read_source", "读取录像"], ["transcribe", "语音转写"], ["analyze", "内容分析"], ["arbitrate", "结果仲裁"], ["review", "AI 审阅"], ["render", "渲染成片"]];
export const STATUS_LABELS: Record<ProjectMainStatus, string> = { blocked: "需要处理", failed: "处理失败", processing: "处理中", queued: "排队中", new_results: "有新成片", paused: "已暂停", inactive: "未启用", idle: "空闲" };
export const RUN_LABELS: Record<Run["status"], string> = { queued: "排队中", processing: "处理中", awaiting_review: "等待审阅", failed: "处理失败", completed: "已完成" };

export interface ProjectDraft {
  name: string; description: string; sourceDirectory: string; outputDirectory: string;
  firstScanMode: ProjectConfig["source"]["first_scan_mode"]; lookbackDays: 3 | 7 | 30;
  scheduleEnabled: boolean; scheduleMode: "daily" | "interval"; dailyTime: string; intervalMinutes: 30 | 60 | 180 | 360 | 720;
  asrRef: string; analysisRef: string; reviewRef: string; retention: ProjectConfig["output"]["intermediate_retention"];
}

export function emptyDraft(options?: FormOptionsPayload): ProjectDraft {
  const asr = options?.resources.find((item) => item.resource_type === "asr")?.resource_id ?? "";
  const analysis = options?.resources.find((item) => item.resource_type === "analysis")?.resource_id ?? "";
  return { name: "", description: "", sourceDirectory: "", outputDirectory: "", firstScanMode: options?.defaults.first_scan_mode ?? "new_only", lookbackDays: options?.defaults.lookback_days ?? 7, scheduleEnabled: options?.defaults.schedule_enabled ?? false, scheduleMode: options?.defaults.schedule_mode ?? "daily", dailyTime: options?.defaults.daily_time ?? "22:00", intervalMinutes: 60, asrRef: asr, analysisRef: analysis, reviewRef: analysis, retention: options?.defaults.intermediate_retention ?? "remind_after_7_days" };
}

export function configFromDraft(draft: ProjectDraft, timezone: string): ProjectConfig {
  return {
    schema_version: 2,
    source: { directory: draft.sourceDirectory, supported_extensions: VIDEO_EXTENSIONS, include_patterns: [], exclude_patterns: [], first_scan_mode: draft.firstScanMode, lookback_days: draft.firstScanMode === "recent" ? draft.lookbackDays : null },
    schedule: { enabled: draft.scheduleEnabled, mode: draft.scheduleMode, daily_time: draft.scheduleMode === "daily" ? draft.dailyTime : null, interval_minutes: draft.scheduleMode === "interval" ? draft.intervalMinutes : null, timezone },
    resources: { asr_ref: draft.asrRef, analysis_ref: draft.analysisRef, review_ref: draft.reviewRef, arbitration_mode: "reuse_analysis", arbitration_ref: null },
    processing: { review_strategy: "ai_auto", output_profile: "current_renderer", naming_policy: "system_safe", review_policy_version: "auto_review_v1", material_policy_version: "publish_material_v1" },
    output: { directory: draft.outputDirectory, intermediate_retention: draft.retention, original_media_policy: "never_delete", final_media_policy: "keep" },
  };
}

export function draftFromProject(project: ProjectSummary): ProjectDraft {
  const config = project.config!.config;
  return { name: project.name, description: project.description, sourceDirectory: config.source.directory, outputDirectory: config.output.directory, firstScanMode: config.source.first_scan_mode, lookbackDays: config.source.lookback_days ?? 7, scheduleEnabled: config.schedule.enabled, scheduleMode: config.schedule.mode, dailyTime: config.schedule.daily_time ?? "22:00", intervalMinutes: config.schedule.interval_minutes ?? 60, asrRef: config.resources.asr_ref, analysisRef: config.resources.analysis_ref, reviewRef: config.resources.review_ref, retention: config.output.intermediate_retention };
}

export interface PollingState<T> {
  data: T | null; error: string; loading: boolean; refresh(): Promise<void>;
  setData: React.Dispatch<React.SetStateAction<T | null>>;
}

export function usePolling<T>(load: (signal: AbortSignal) => Promise<T>, interval: number, resourceKey = "default"): PollingState<T> {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState(""); const [loading, setLoading] = useState(true); const loadRef = useRef(load); const controllerRef = useRef<AbortController | null>(null); const sequenceRef = useRef(0); loadRef.current = load;
  const refresh = useCallback(async () => {
    controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller; const sequence = ++sequenceRef.current;
    try { const next = await loadRef.current(controller.signal); if (sequence === sequenceRef.current) { setData(next); setError(""); } }
    catch (reason) { if (!(reason instanceof DOMException && reason.name === "AbortError") && sequence === sequenceRef.current) setError((reason as Error).message); }
    finally { if (sequence === sequenceRef.current) setLoading(false); }
  }, []);
  useEffect(() => { setData(null); setError(""); setLoading(true); void refresh(); return () => controllerRef.current?.abort(); }, [refresh, resourceKey]);
  useEffect(() => {
    let timer = 0; const schedule = () => { window.clearInterval(timer); if (!document.hidden) timer = window.setInterval(() => void refresh(), interval); };
    const visibilityChanged = () => { if (!document.hidden) void refresh(); schedule(); };
    schedule(); document.addEventListener("visibilitychange", visibilityChanged);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", visibilityChanged); };
  }, [interval, refresh]);
  return { data, error, loading, refresh, setData };
}

export function statusTone(status: ProjectMainStatus | Run["status"] | ScanEvent["status"]) { if (["blocked", "failed"].includes(status)) return "error"; if (["awaiting_review", "partial", "paused", "inactive"].includes(status)) return "warning"; if (["processing", "queued", "running"].includes(status)) return "accent"; if (["idle", "completed", "success", "new_results"].includes(status)) return "success"; return "neutral"; }
export function StatusPill({ status, label }: { status: ProjectMainStatus | Run["status"] | ScanEvent["status"]; label?: string }) { const known = STATUS_LABELS[status as ProjectMainStatus] ?? RUN_LABELS[status as Run["status"]] ?? ({ running: "扫描中", success: "已完成", partial: "部分完成" } as Record<string, string>)[status]; if (!known && !label) console.warn("Venus received an unknown status enum", String(status)); return <span className={`status-pill tone-${statusTone(status)}`}><span className="status-dot" />{label ?? known ?? `未知状态（${String(status)}）`}</span>; }
export function basename(path: string) { return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path; }
export function time(value?: string | null) { return value ? formatLocalTime(value) : "—"; }
export function formatBytes(bytes: number) { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
export function scanMessage(scan: ScanEvent) { if (scan.status === "running") return "正在扫描录像目录"; if (scan.status === "failed") return scan.error_summary ?? "扫描失败"; return `新增 ${scan.created_count ?? 0} · 已存在 ${scan.duplicate_count ?? 0} · 写入中 ${scan.unstable_count ?? 0}`; }

export function PageHeading({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: React.ReactNode }) { return <header className="page-heading"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1><p>{description}</p></div>{actions && <div className="actions">{actions}</div>}</header>; }
export function SectionHeading({ title, subtitle, action }: { title: string; subtitle: string; action?: React.ReactNode }) { return <div className="section-heading"><div><h2>{title}</h2><p>{subtitle}</p></div>{action}</div>; }
export function Metric({ label, value, tone = "default" }: { label: string; value: number; tone?: string }) { return <div className={`metric tone-${tone}`}><span>{label}</span><strong>{value}</strong></div>; }
export function LoadingState() { return <div className="empty-state" role="status">正在读取本机工作台数据…</div>; }
export function ErrorState({ message, retry }: { message: string; retry(): void }) { return <div className="error-state" role="alert"><strong>暂时无法读取数据</strong><p>{message}</p><button className="button" onClick={retry}>重试</button></div>; }
export function RunCard({ run, project }: { run: Run; project?: ProjectSummary }) { return <Link className="run-card" to={`/projects/${run.project_id}/runs/${run.run_id}`}><div><span className="overline">{project?.name ?? "项目"}</span><strong>{run.source_name}</strong><small>{time(run.updated_at)}</small></div><StatusPill status={run.status} label={run.status === "queued" && run.queue_position ? `队列第 ${run.queue_position} 位` : undefined} /></Link>; }

const PRIORITY: Record<ProjectMainStatus, number> = { blocked: 0, failed: 1, processing: 2, queued: 3, new_results: 4, paused: 5, inactive: 6, idle: 7 };
export function sortProjects(projects: ProjectSummary[]) { return [...projects].sort((a, b) => (PRIORITY[a.main_status] ?? 99) - (PRIORITY[b.main_status] ?? 99) || b.updated_at.localeCompare(a.updated_at) || a.project_id.localeCompare(b.project_id)); }
export function ProjectRow({ project, detailed = false }: { project: ProjectSummary; detailed?: boolean }) { const next = project.schedule?.enabled ? `下次扫描 ${time(project.schedule.next_scan_at)}` : "仅手动扫描"; return <Link className="project-row" to={`/projects/${project.project_id}`}><span className={`project-indicator tone-${statusTone(project.main_status)}`} /><div className="project-copy"><strong>{project.name}</strong><p>{project.description || "未填写项目描述"}</p>{detailed && <small>{next} · 更新于 {time(project.updated_at)}</small>}</div><div className="workload"><span>{project.workload.processing}<small>处理中</small></span><span>{project.workload.queued}<small>排队</small></span><span>{project.workload.failed}<small>失败</small></span><span>{project.workload.completed}<small>已完成</small></span><span>{project.workload.new_results}<small>新成片</small></span></div><StatusPill status={project.main_status} /><b>›</b></Link>; }
