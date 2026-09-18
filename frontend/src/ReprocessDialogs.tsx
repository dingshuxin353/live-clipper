import { Button } from '@astryxdesign/core/Button';
import { RemixIcon } from "./ui/RemixIcon";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "./api";
import { DialogFrame } from "./ProjectDialogs";
import { projectApi, requestId } from "./project-api";
import type { ProjectSummary, ReprocessBlockerAction, ReprocessPreflight, ReprocessSettingsSummary, ReprocessVersion, ReprocessVersionsPayload, Run } from "./project-dto";
import { RUN_LABELS, STAGES, formatBytes, time } from "./workbench-shared";


const FIELD_LABELS: Record<keyof ReprocessSettingsSummary | "result_summary", string> = {
  asr: "语音识别", analysis: "内容分析模型", ai_review: "片段筛选", render: "成片规格", naming: "文件命名",
  output_directory: "成片保存位置", retention: "临时文件保留方式", result_summary: "处理结果",
};
const ACTION_LABELS: Record<ReprocessBlockerAction, string> = {
  source_repair: "重新选择原始录像", project_settings: "项目设置", asr_settings: "项目设置",
  ai_settings: "项目设置", active_run: "查看进行中的记录", recheck: "重新检查",
};
const SUMMARY_PART_LABELS = { name: "名称", backend: "识别方式", provider_name: "供应商", model: "模型", language: "识别语言", endpoint: "服务地址" };
const ENUM_LABELS: Record<string, string> = { mlx_whisper: "本机识别", cloud: "云端识别", openai: "云端识别", zh: "中文", en: "英语", auto: "自动识别", current_renderer: "默认成片规格", system_safe: "自动生成文件名", keep: "保留文件，不提醒清理", remind_immediately: "处理完成后提醒清理", remind_after_7_days: "处理完成 7 天后提醒清理" };
const BLOCKER_REASONS: Record<string, string> = { run_not_terminal: "这条记录尚未结束。", project_inactive: "项目尚未启用。", project_not_ready: "项目尚未准备好，请检查项目设置。", resource_unavailable: "处理所需的模型或工具不可用，请在项目设置中检查所选项。", source_missing: "找不到原始录像，或无法读取文件。", source_identity_mismatch: "录像文件已变化，与这条记录使用的录像不一致。", storage_full: "工作目录的可用空间不足以保存录像副本。" };
function versionLabel(sequence: number) { return `第 ${sequence} 次处理`; }
type PendingRequest = { id: string; revision: string };
function requestKey(runId: string) { return `venus.reprocess.request.${runId}`; }
function getPending(runId: string): PendingRequest | null { try { const pending = JSON.parse(sessionStorage.getItem(requestKey(runId)) || 'null'); return pending && typeof pending.id === 'string' && typeof pending.revision === 'string' ? pending : null; } catch { return null; } }
function keepPending(runId: string, pending: PendingRequest) { try { sessionStorage.setItem(requestKey(runId), JSON.stringify(pending)); } catch { /* The mounted controller retains the original request. */ } }
function clearPendingId(runId: string) { try { sessionStorage.removeItem(requestKey(runId)); } catch { /* nothing else to clear */ } }
function value(input: unknown): string {
  if (input === null || input === undefined || input === "") return "未记录";
  if (typeof input !== "object" || Array.isArray(input)) return ENUM_LABELS[String(input)] || String(input);
  const snapshot = input as Record<string, unknown>;
  const details = { ...snapshot, ...(typeof snapshot.config === 'object' && snapshot.config ? snapshot.config : {}) } as Record<string, unknown>;
  if (!details.provider_name && details.provider) details.provider_name = details.provider === 'OpenAI-compatible LLM' ? 'OpenAI 兼容服务' : '未记录供应商名称';
  const parts = Object.entries(SUMMARY_PART_LABELS).flatMap(([key, label]) => {
    const item = details[key];
    return item === null || item === undefined || item === "" ? [] : [`${label}：${value(item)}`];
  });
  return parts.length ? parts.join(" · ") : "未记录";
}
function bytes(value: number) { return value === 0 ? "0 KB" : formatBytes(value); }
function resultValue(version: ReprocessVersion) {
  const result = version.result_summary;
  if (!result) return "尚无处理结果";
  const label = { clips_ready: '成片已生成', no_clip: '本次未选出适合的片段', partial: '部分成片可用', unavailable: '结果暂时不可用' }[result.result_type];
  return `${label || '未识别的结果状态'} · 已选 ${result.selected_count} 个片段 · ${result.available_output_count} 个可用成片 · ${result.failed_output_count} 个不可用成片${result.available_output_count > 0 ? ` · 总时长 ${Math.round(result.total_duration_ms / 1000)} 秒` : ''}`;
}

export function ReprocessControls({ run, project }: { run: Run; project: ProjectSummary }) {
  const location = useLocation(); const navigate = useNavigate();
  const [preflight, setPreflight] = useState<ReprocessPreflight | null>(null); const [versions, setVersions] = useState<ReprocessVersionsPayload | null>(null);
  const [compare, setCompare] = useState<ReprocessVersion | null>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false); const [uncertain, setUncertain] = useState(Boolean(getPending(run.run_id))); const [reconfirm, setReconfirm] = useState(false);
  const submittingRef = useRef(false); const requestRef = useRef(getPending(run.run_id));
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
  useEffect(() => { if (!reprocessRequested) setPreflight(null); }, [reprocessRequested]);
  useEffect(() => { if (reprocessRequested && terminal && !preflight && !loading) void loadPreflight(); }, [loadPreflight, loading, preflight, reprocessRequested, terminal]);
  useEffect(() => { requestRef.current = getPending(run.run_id); setPreflight(null); setVersions(null); setCompare(null); setUncertain(Boolean(requestRef.current)); }, [run.run_id]);

  const closePreflight = () => { if (submittingRef.current || uncertain) return; setError(""); setReconfirm(false); setQuery((next) => next.delete("reprocess")); };
  const openVersions = async () => { setLoading(true); setError(""); try { setVersions(await projectApi.reprocessVersions(run.run_id)); } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); } };
  const continueRun = () => { if (!run.active_issue_summary) return; setQuery((next) => next.set("issue", run.active_issue_summary!.issue_id)); };
  const start = async () => {
    if (!preflight || !requestRef.current && (!preflight.can_reprocess || reconfirm) || submittingRef.current) return;
    submittingRef.current = true; setSubmitting(true); setError(""); setUncertain(false);
    const pending = requestRef.current ?? { id: requestId("run-reprocess"), revision: preflight.preflight_revision }; requestRef.current = pending; keepPending(run.run_id, pending);
    try { const response = await projectApi.createReprocess(run.run_id, pending.id, pending.revision); clearPendingId(run.run_id); requestRef.current = null; navigate(`/projects/${project.project_id}/runs/${response.run.run_id}`); }
    catch (reason) {
      const apiError = reason as ApiError; const unknown = apiError.outcomeUnknown || apiError.code === "request_id_conflict";
      if (unknown) { setUncertain(true); setError("暂时无法确认是否已创建处理记录。点击“继续本次操作”重试，不会重复创建。"); }
      else { clearPendingId(run.run_id); requestRef.current = null; if (apiError.status === 409) { try { setPreflight(await projectApi.reprocessPreflight(run.run_id)); setReconfirm(true); setError("检查结果有变化，请核对后再开始。"); } catch { setError(apiError.message); } } else setError(apiError.message); }
      submittingRef.current = false; setSubmitting(false);
    }
  };
  const blocker = async (action: ReprocessBlockerAction) => {
    if (!preflight) return;
    if (action === "recheck") { setPreflight(null); await loadPreflight(); return; }
    if (action === "active_run" && preflight.active_run) { navigate(`/projects/${project.project_id}/runs/${preflight.active_run.run_id}`); return; }
    if (["project_settings", "asr_settings", "ai_settings"].includes(action)) { const search = new URLSearchParams({ dialog: "project-settings", returnTo: returnTo() }); navigate({ pathname: `/projects/${project.project_id}`, search: search.toString() }); return; }
    if (action === "source_repair") { try { const response = await projectApi.repairReprocessSource(run.run_id); setPreflight(null); setQuery((next) => { next.delete("reprocess"); next.set("issue", response.issue.issue_id); next.set("reprocessAfterRepair", "1"); }); } catch (reason) { setError((reason as Error).message); } }
  };

  return <>
    <div className="reprocess-toolbar" aria-label="处理版本与操作">
      <Button isDisabled={loading} onClick={() => void openVersions()} label={(versionLabel(run.processing_sequence))} />
      {terminal && (resumable ? <><Button onClick={continueRun} label={"继续处理"} variant="primary" /><Button onClick={() => setQuery((next) => next.set("reprocess", "1"))} label={"按当前项目设置重新处理"} /></> : <Button onClick={() => setQuery((next) => next.set("reprocess", "1"))} label={"重新处理"} />)}
    </div>
    {error && !preflight && <p className="stale-warning" role="alert">{error}</p>}
    {reprocessRequested && preflight && <ReprocessDialog preflight={preflight} project={project} submitting={submitting} uncertain={uncertain} reconfirm={reconfirm} error={error} close={closePreflight} adjust={() => void blocker("project_settings")} act={(action) => void blocker(action)} confirm={() => { setReconfirm(false); setError(""); }} start={() => void start()} />}
    {versions && !compare && <VersionsDrawer currentRunId={run.run_id} payload={versions} project={project} close={() => setVersions(null)} compare={setCompare} />}
    {versions && compare && <CompareDialog current={versions.versions.find((item) => item.run_id === run.run_id)} other={compare} close={() => setCompare(null)} />}
  </>;
}

function ReprocessDialog({ preflight, project, submitting, uncertain, reconfirm, error, close, adjust, act, confirm, start }: { preflight: ReprocessPreflight; project: ProjectSummary; submitting: boolean; uncertain: boolean; reconfirm: boolean; error: string; close(): void; adjust(): void; act(action: ReprocessBlockerAction): void; confirm(): void; start(): void }) {
  const blocked = !preflight.can_reprocess;
  return <DialogFrame wide closeDisabled={submitting || uncertain} onClose={close} title="重新处理这段录像" description="将按当前项目设置创建新的剪辑记录，从头处理。已有记录、成片和发布文案会保留。调用云端模型或 Claude Code 可能产生费用。" footer={<><Button isDisabled={submitting || uncertain} onClick={close} label={"取消"} /><span className="footer-spacer" />{!blocked && <Button isDisabled={submitting || uncertain} onClick={adjust} label={"调整设置"} />}{reconfirm && <Button onClick={confirm} label={"已核对，继续"} variant="primary" />}<Button isLoading={submitting} isDisabled={submitting || !uncertain && (blocked || reconfirm)} onClick={start} label={(submitting ? "正在创建处理记录…" : uncertain ? "继续本次操作" : "开始重新处理")} variant="primary" /></>}>
    {error && <p className="form-error" role="alert">{error}</p>}
    <section className={`reprocess-readiness ${blocked ? "blocked" : "ready"}`}><strong>{blocked ? "暂时无法重新处理" : "检查通过"}</strong><p>{blocked ? "请先解决以下问题，再点击“重新检查”。" : `将开始${versionLabel(preflight.next_processing_sequence)}。`}</p></section>
    {blocked && <div className="reprocess-blockers">{preflight.blockers.map((item, index) => <p key={index}>{BLOCKER_REASONS[item.code] || (item.action === "asr_settings" || item.action === "ai_settings" ? "所选模型尚未准备好，请在项目设置中检查。" : "项目配置尚未通过检查，请核对目录和处理设置。")}</p>)}{[...new Set([...preflight.blockers.map((item) => item.action === "asr_settings" || item.action === "ai_settings" ? "project_settings" as const : item.action), "recheck" as const])].map((action) => <Button key={action} isDisabled={submitting || uncertain} onClick={() => act(action)} label={(ACTION_LABELS[action])} />)}</div>}
    <div className="reprocess-summary-grid"><section><span>录像</span><h2>{preflight.source.name}</h2><dl><div><dt>所属项目</dt><dd>{project.name}</dd></div><div><dt>当前查看的处理版本</dt><dd>{versionLabel(preflight.run.processing_sequence)}</dd></div><div><dt>录像状态</dt><dd>{preflight.source.state === "ready" ? "原文件可用" : preflight.source.state === "identity_mismatch" ? "录像文件已变化" : "找不到录像或无法读取"}</dd></div></dl><p>处理时会复制一份录像，不会修改原文件。</p></section><section><span>与本次记录所用设置的差异</span>{preflight.changes.some(item => value(item.before) !== value(item.after)) ? <dl>{preflight.changes.filter(item => value(item.before) !== value(item.after)).map((item) => <div key={item.field}><dt>{FIELD_LABELS[item.field]}</dt><dd><small>{value(item.before)}</small><b>改为</b><strong>{value(item.after)}</strong></dd></div>)}</dl> : <p>未发现可比较设置的差异；未记录的信息无法比较。</p>}</section></div>
    <section className="reprocess-phases" aria-label="重新处理阶段">{STAGES.map(([, phase], index) => <span key={phase}>{phase}{index < STAGES.length - 1 && <b aria-hidden="true"><RemixIcon name="chevronRight" /></b>}</span>)}</section>
    <div className="reprocess-facts"><div><span>处理所需临时空间</span><strong>工作目录：{preflight.space.work_directory}</strong><small>可用空间：{bytes(preflight.space.available_bytes)}</small><small>{preflight.source.bytes === null ? '无法读取录像大小，暂时无法判断所需空间。' : `录像副本至少需要 ${bytes(preflight.space.required_bytes)}，处理过程中还会使用额外空间。`}</small>{preflight.source.bytes !== null && <small>{preflight.space.sufficient ? "可容纳录像副本" : "空间不足以保存录像副本"}</small>}</div><div><span>成片保存位置</span><strong>{value(preflight.current_settings.summary.output_directory)}</strong></div><div><span>原有结果</span><strong>保留</strong><small>处理记录、成片和发布文案不会被覆盖</small></div></div>
  </DialogFrame>;
}

function VersionsDrawer({ currentRunId, payload, project, close, compare }: { currentRunId: string; payload: ReprocessVersionsPayload; project: ProjectSummary; close(): void; compare(version: ReprocessVersion): void }) {
  return <DialogFrame onClose={close} title="处理版本" description="查看这段录像每次处理时使用的设置和结果。"><div className="reprocess-version-source"><strong>{project.name}</strong><span>共 {payload.versions.length} 次处理</span></div><div className="reprocess-version-list">{payload.versions.map((item) => <article className={item.run_id === currentRunId ? "current" : ""} key={item.run_id}><Link to={`/projects/${project.project_id}/runs/${item.run_id}`}><strong>{versionLabel(item.processing_sequence)}</strong><span>{item.completed_at ? "完成于" : "更新于"} {time(item.completed_at ?? item.updated_at)} · {RUN_LABELS[item.status]}</span><small>语音识别：{value(item.settings_summary.asr)}；内容分析：{value(item.settings_summary.analysis)}</small><small>{resultValue(item)}</small></Link>{item.run_id !== currentRunId && <Button onClick={() => compare(item)} label="与当前查看的版本比较" aria-label={`比较${versionLabel(payload.versions.find(v => v.run_id === currentRunId)?.processing_sequence || 0)}与${versionLabel(item.processing_sequence)}`} isDisabled={!payload.versions.some(v => v.run_id === currentRunId)} />}</article>)}</div></DialogFrame>;
}

function CompareDialog({ current, other, close }: { current?: ReprocessVersion; other: ReprocessVersion; close(): void }) {
  const fields = current ? (Object.keys(FIELD_LABELS) as Array<keyof typeof FIELD_LABELS>).filter(field => field === 'result_summary' ? resultValue(current) !== resultValue(other) : value(current.settings_summary[field]) !== value(other.settings_summary[field])) : [];
  const incomplete = current && [current, other].some(version => (Object.keys(FIELD_LABELS) as Array<keyof typeof FIELD_LABELS>).some(field => field !== 'result_summary' && value(version.settings_summary[field]) === '未记录'));
  return <DialogFrame wide onClose={close} title="比较两次处理" description="比较这两次处理的设置、保存位置和结果摘要。这里只显示有差异的项目。" footer={<><span className="footer-spacer" /><Button onClick={close} label="关闭" variant="primary" /></>}>
    {!current ? <p role="alert">暂时无法读取当前查看的版本，无法比较。</p> : <><div className="reprocess-compare-head"><strong>{versionLabel(current.processing_sequence)}</strong><strong>{versionLabel(other.processing_sequence)}</strong></div>{fields.length ? <div className="reprocess-compare-list">{fields.map(field => <div key={field}><span>{FIELD_LABELS[field]}</span><strong>{field === "result_summary" ? resultValue(current) : value(current.settings_summary[field])}</strong><strong>{field === "result_summary" ? resultValue(other) : value(other.settings_summary[field])}</strong></div>)}</div> : <p className="quiet-state">{incomplete ? '部分历史设置未记录，无法完整比较。已记录的内容相同。' : '所比较的设置和结果摘要相同。'}</p>}</>}
  </DialogFrame>;
}
