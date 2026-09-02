import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "./api";
import { DialogFrame } from "./ProjectDialogs";
import { projectApi, requestId } from "./project-api";
import type { ProjectSummary, ReprocessBlockerAction, ReprocessPreflight, ReprocessSettingsSummary, ReprocessVersion, ReprocessVersionsPayload, Run } from "./project-dto";
import { RUN_LABELS, formatBytes, time } from "./workbench-shared";

const PHASES = ["读取录像", "语音转写", "内容分析", "结果仲裁", "AI 审阅", "渲染成片"];
const FIELD_LABELS: Record<keyof ReprocessSettingsSummary | "result_summary", string> = {
  asr: "语音识别", analysis: "AI 模型", ai_review: "AI 审阅", render: "输出规格", naming: "命名方式",
  output_directory: "输出目录", retention: "中间产物", result_summary: "处理结果",
};
const ACTION_LABELS: Record<ReprocessBlockerAction, string> = {
  source_repair: "找回原录像", project_settings: "打开项目设置", asr_settings: "打开语音识别设置",
  ai_settings: "打开 AI 设置", active_run: "查看正在处理的记录", recheck: "重新检查",
};
const SUMMARY_PART_LABELS = { backend: "识别方式", provider: "服务", model: "模型", language: "语言", endpoint: "地址" };

function versionLabel(sequence: number) { return sequence === 1 ? "初次处理" : `第 ${sequence} 次处理`; }
function requestKey(runId: string) { return `venus.reprocess.request.${runId}`; }
function getPendingId(runId: string) { try { return sessionStorage.getItem(requestKey(runId)); } catch { return null; } }
function keepPendingId(runId: string, id: string) { try { sessionStorage.setItem(requestKey(runId), id); } catch { /* the in-memory request still protects this render */ } }
function clearPendingId(runId: string) { try { sessionStorage.removeItem(requestKey(runId)); } catch { /* nothing else to clear */ } }
function value(value: unknown) {
  if (value === null || value === undefined || value === "") return "无法获取";
  if (typeof value !== "object" || Array.isArray(value)) return String(value);
  const parts = Object.entries(SUMMARY_PART_LABELS).flatMap(([key, label]) => {
    const item = (value as Record<string, unknown>)[key];
    return item === null || item === undefined || item === "" ? [] : [`${label}：${String(item)}`];
  });
  return parts.length ? parts.join(" · ") : "无法获取";
}
function bytes(value: number) { return value === 0 ? "0 KB" : formatBytes(value); }
function resultValue(version: ReprocessVersion) {
  const result = version.result_summary;
  return result ? `${result.available_output_count} 个成片，${result.selected_count} 个入选，${Math.round(result.total_duration_ms / 1000)} 秒` : "尚未产生结果";
}

export function ReprocessControls({ run, project }: { run: Run; project: ProjectSummary }) {
  const location = useLocation(); const navigate = useNavigate();
  const [preflight, setPreflight] = useState<ReprocessPreflight | null>(null); const [versions, setVersions] = useState<ReprocessVersionsPayload | null>(null);
  const [compare, setCompare] = useState<ReprocessVersion | null>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false); const [uncertain, setUncertain] = useState(false); const [reconfirm, setReconfirm] = useState(false);
  const submittingRef = useRef(false); const requestRef = useRef(getPendingId(run.run_id));
  const params = new URLSearchParams(location.search); const terminal = run.status === "completed" || run.status === "failed"; const reprocessRequested = params.get("reprocess") === "1"; const repairReturn = params.get("reprocessAfterRepair") === "1"; const issueOpen = params.has("issue");
  const resumable = run.status === "failed" && Boolean(run.active_issue_summary?.available_actions.includes("continue_run"));
  const setQuery = useCallback((mutate: (next: URLSearchParams) => void) => { const next = new URLSearchParams(location.search); mutate(next); navigate({ pathname: location.pathname, search: next.toString() }, { replace: true }); }, [location.pathname, location.search, navigate]);
  const returnTo = useCallback(() => { const next = new URLSearchParams(location.search); next.delete("issue"); next.delete("reprocessAfterRepair"); next.set("reprocess", "1"); return `${location.pathname}?${next}`; }, [location.pathname, location.search]);
  const loadPreflight = useCallback(async () => {
    if (loading) return; setLoading(true); setError("");
    try { const next = await projectApi.reprocessPreflight(run.run_id); if (next.active_run) { navigate(`/projects/${project.project_id}/runs/${next.active_run.run_id}`); return; } setPreflight(next); }
    catch (reason) { setError((reason as Error).message); setQuery((next) => next.delete("reprocess")); }
    finally { setLoading(false); }
  }, [loading, navigate, project.project_id, run.run_id, setQuery]);

  useEffect(() => { if (!repairReturn || issueOpen) return; setQuery((next) => { next.delete("reprocessAfterRepair"); next.set("reprocess", "1"); }); }, [issueOpen, repairReturn, setQuery]);
  useEffect(() => { if (reprocessRequested && terminal && !preflight && !loading) void loadPreflight(); }, [loadPreflight, loading, preflight, reprocessRequested, terminal]);
  useEffect(() => { requestRef.current = getPendingId(run.run_id); setPreflight(null); setVersions(null); setCompare(null); setUncertain(false); }, [run.run_id]);

  const closePreflight = () => { if (submittingRef.current) return; setPreflight(null); setError(""); setReconfirm(false); setQuery((next) => next.delete("reprocess")); };
  const openVersions = async () => { setLoading(true); setError(""); try { setVersions(await projectApi.reprocessVersions(run.run_id)); } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); } };
  const continueRun = () => { if (!run.active_issue_summary) return; setQuery((next) => next.set("issue", run.active_issue_summary!.issue_id)); };
  const start = async () => {
    if (!preflight || !preflight.can_reprocess || submittingRef.current || reconfirm) return;
    submittingRef.current = true; setSubmitting(true); setError(""); setUncertain(false);
    const id = requestRef.current ?? requestId("run-reprocess"); requestRef.current = id; keepPendingId(run.run_id, id);
    try { const response = await projectApi.createReprocess(run.run_id, id, preflight.preflight_revision); clearPendingId(run.run_id); requestRef.current = null; navigate(`/projects/${project.project_id}/runs/${response.run.run_id}`); }
    catch (reason) {
      const apiError = reason as ApiError; const unknown = apiError.status === 0 || apiError.status >= 500 || apiError.code === "invalid_response";
      if (unknown) { setUncertain(true); setError("暂时无法确认是否已创建。再次尝试会继续本次操作，不会重复创建。"); }
      else { clearPendingId(run.run_id); requestRef.current = null; if (apiError.status === 409) { try { setPreflight(await projectApi.reprocessPreflight(run.run_id)); setReconfirm(true); setError("检查结果已经变化，请确认最新状态后再开始。"); } catch { setError(apiError.message); } } else setError(apiError.message); }
      submittingRef.current = false; setSubmitting(false);
    }
  };
  const blocker = async (action: ReprocessBlockerAction) => {
    if (!preflight) return;
    if (action === "recheck") { setPreflight(null); await loadPreflight(); return; }
    if (action === "active_run" && preflight.active_run) { navigate(`/projects/${project.project_id}/runs/${preflight.active_run.run_id}`); return; }
    if (action === "project_settings") { const search = new URLSearchParams({ dialog: "project-settings", returnTo: returnTo() }); navigate({ pathname: `/projects/${project.project_id}`, search: search.toString() }); return; }
    if (action === "asr_settings" || action === "ai_settings") { const search = new URLSearchParams({ focus: action === "asr_settings" ? "asr" : "ai", returnTo: returnTo() }); navigate({ pathname: "/settings", search: search.toString() }); return; }
    if (action === "source_repair") { try { const response = await projectApi.repairReprocessSource(run.run_id); setPreflight(null); setQuery((next) => { next.delete("reprocess"); next.set("issue", response.issue.issue_id); next.set("reprocessAfterRepair", "1"); }); } catch (reason) { setError((reason as Error).message); } }
  };

  return <>
    <div className="reprocess-toolbar" aria-label="处理版本与操作">
      <button className="button version-button" disabled={loading} onClick={() => void openVersions()}>{versionLabel(run.processing_sequence)}</button>
      {terminal && (resumable ? <><button className="button primary" onClick={continueRun}>继续处理</button><button className="button" onClick={() => setQuery((next) => next.set("reprocess", "1"))}>按当前设置重新处理</button></> : <button className="button" onClick={() => setQuery((next) => next.set("reprocess", "1"))}>重新处理</button>)}
    </div>
    {error && !preflight && <p className="stale-warning" role="alert">{error}</p>}
    {preflight && <ReprocessDialog preflight={preflight} project={project} submitting={submitting} uncertain={uncertain} reconfirm={reconfirm} error={error} close={closePreflight} adjust={() => void blocker("project_settings")} act={(action) => void blocker(action)} confirm={() => { setReconfirm(false); setError(""); }} start={() => void start()} />}
    {versions && !compare && <VersionsDrawer currentRunId={run.run_id} payload={versions} project={project} close={() => setVersions(null)} compare={setCompare} />}
    {versions && compare && <CompareDialog current={versions.versions.find((item) => item.run_id === run.run_id) ?? versions.versions[0]} other={compare} close={() => setCompare(null)} />}
  </>;
}

function ReprocessDialog({ preflight, project, submitting, uncertain, reconfirm, error, close, adjust, act, confirm, start }: { preflight: ReprocessPreflight; project: ProjectSummary; submitting: boolean; uncertain: boolean; reconfirm: boolean; error: string; close(): void; adjust(): void; act(action: ReprocessBlockerAction): void; confirm(): void; start(): void }) {
  const blocked = !preflight.can_reprocess;
  return <DialogFrame wide closeDisabled={submitting} onClose={close} title="重新处理这条录像" description="将使用当前设置创建新的处理记录。原有记录和成片不会被修改。" footer={<><button className="button" disabled={submitting} onClick={close}>取消</button><span className="footer-spacer" />{!blocked && <button className="button" disabled={submitting} onClick={adjust}>调整设置</button>}{reconfirm && <button className="button primary" onClick={confirm}>确认最新检查结果</button>}<button className="button primary" disabled={blocked || submitting || reconfirm} onClick={start}>{submitting ? "正在创建…" : "开始重新处理"}</button></>}>
    {error && <p className="form-error" role="alert">{error}</p>}
    <section className={`reprocess-readiness ${blocked ? "blocked" : "ready"}`}><strong>{blocked ? "暂时不能重新处理" : "可以重新处理"}</strong><p>{blocked ? "请先完成下方修复，再重新检查。" : `将创建${versionLabel(preflight.next_processing_sequence)}，旧结果继续保留。`}</p></section>
    {blocked && <div className="reprocess-blockers">{[...new Set(preflight.blockers.map((item) => item.action))].map((action) => <button className="button" key={action} onClick={() => act(action)}>{ACTION_LABELS[action]}</button>)}</div>}
    <div className="reprocess-summary-grid"><section><span>录像</span><h2>{preflight.source.name}</h2><dl><div><dt>所属项目</dt><dd>{project.name}</dd></div><div><dt>来源版本</dt><dd>{versionLabel(preflight.run.processing_sequence)}</dd></div><div><dt>录像状态</dt><dd>{preflight.source.state === "ready" ? "原文件可用" : "需要修复"}</dd></div></dl><p>处理时会复制一份录像，不会修改原文件。</p></section><section><span>当前设置变化</span>{preflight.changes.length ? <dl>{preflight.changes.map((item) => <div key={item.field}><dt>{FIELD_LABELS[item.field]}</dt><dd><small>{value(item.before)}</small><b>至</b><strong>{value(item.after)}</strong></dd></div>)}</dl> : <p>当前设置与来源版本相同。</p>}</section></div>
    <section className="reprocess-phases" aria-label="重新处理阶段">{PHASES.map((phase, index) => <span key={phase}>{phase}{index < PHASES.length - 1 && <b aria-hidden="true">›</b>}</span>)}</section>
    <div className="reprocess-facts"><div><span>临时空间</span><strong>{bytes(preflight.space.available_bytes)} 可用</strong><small>预计至少需要 {bytes(preflight.space.required_bytes)}</small></div><div><span>输出目录</span><strong>{value(preflight.current_settings.summary.output_directory)}</strong><small>{preflight.space.sufficient ? "空间检查通过" : "空间不足"}</small></div><div><span>原有结果</span><strong>保留</strong><small>处理记录、成片和发布物料不会被覆盖</small></div></div>
    {uncertain && <p className="reprocess-uncertain">关闭或重新打开页面后，再次尝试仍会继续本次操作。</p>}
  </DialogFrame>;
}

function useFocusTrap(ref: React.RefObject<HTMLElement | null>, close: () => void) {
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; const focusable = () => [...(ref.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])') ?? [])]; window.setTimeout(() => focusable()[0]?.focus(), 0); const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); if (event.key !== "Tab") return; const items = focusable(); if (!items.length) return; const first = items[0]; const last = items.at(-1)!; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }; window.addEventListener("keydown", keydown); return () => { window.removeEventListener("keydown", keydown); previous?.focus(); }; }, [close, ref]);
}

function VersionsDrawer({ currentRunId, payload, project, close, compare }: { currentRunId: string; payload: ReprocessVersionsPayload; project: ProjectSummary; close(): void; compare(version: ReprocessVersion): void }) {
  const ref = useRef<HTMLElement>(null); useFocusTrap(ref, close);
  return <div className="reprocess-drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><aside ref={ref} className="reprocess-drawer" role="dialog" aria-modal="true" aria-labelledby="versions-title"><header><div><h1 id="versions-title">处理版本</h1><p>每次处理的设置和结果都会单独保留。</p></div><button className="modal-close" aria-label="关闭处理版本" onClick={close}>×</button></header><div className="reprocess-version-source"><strong>{project.name}</strong><span>共 {payload.versions.length} 次处理</span></div><div className="reprocess-version-list">{payload.versions.map((item) => <article className={item.run_id === currentRunId ? "current" : ""} key={item.run_id}><Link to={`/projects/${project.project_id}/runs/${item.run_id}`}><strong>{versionLabel(item.processing_sequence)}</strong><span>{time(item.completed_at ?? item.updated_at)}，{RUN_LABELS[item.status]}</span><small>{value(item.settings_summary.asr)}，{value(item.settings_summary.analysis)}</small><small>{item.result_summary ? `${item.result_summary.available_output_count} 个可用成片` : "尚未产生结果"}</small></Link>{item.run_id !== currentRunId && <button className="text-button" aria-label={`比较${versionLabel(item.processing_sequence)}`} onClick={() => compare(item)}>比较</button>}</article>)}</div></aside></div>;
}

function CompareDialog({ current, other, close }: { current: ReprocessVersion; other: ReprocessVersion; close(): void }) {
  const fields = other.changed_fields;
  return <DialogFrame wide onClose={close} title="比较两次处理" description="只列出两次处理在设置、存储位置和结果上的差异。" footer={<><span className="footer-spacer" /><button className="button primary" onClick={close}>关闭</button></>}><div className="reprocess-compare-head"><strong>{versionLabel(current.processing_sequence)}</strong><strong>{versionLabel(other.processing_sequence)}</strong></div>{fields.length ? <div className="reprocess-compare-list">{fields.map((field) => <div key={field}><span>{FIELD_LABELS[field]}</span><strong>{field === "result_summary" ? resultValue(current) : value(current.settings_summary[field])}</strong><b>至</b><strong>{field === "result_summary" ? resultValue(other) : value(other.settings_summary[field])}</strong></div>)}</div> : <p className="quiet-state">这两次处理没有差异。</p>}</DialogFrame>;
}
