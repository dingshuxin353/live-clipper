import { RemixIcon } from "../../ui/RemixIcon";
import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Field } from "@astryxdesign/core/Field";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";

import { ApiError } from "../../api";
import { projectApi, requestId } from "../../project-api";
import type { MigrationChoices, MigrationSavedHistory, MigrationPlan, MigrationReport, MigrationSession, MigrationSnapshot, MigrationStartupSummary } from "../../project-dto";
import { PathField } from "../../workbench-shared";

const STEPS = [
  ["检查旧版数据", "检查设置和记录"],
  ["核对升级内容", "核对项目和历史记录"],
  ["确认升级", "备份并导入数据"],
  ["完成", "进入项目"],
] as const;

type Props = { startup: MigrationStartupSummary; onEnter(projectId: string): void };
type Screen = "check" | "differences" | "confirm" | "executing" | "complete" | "failed" | "incomplete" | "diagnostic";

function entryFor(session: MigrationSession): MigrationStartupSummary["entry"] {
  if (session.state.startsWith("completed_")) return "completed";
  if (session.state === "failed_rolled_back") return "failed";
  if (session.state === "diagnostic_required") return session.project_id ? "incomplete" : "diagnostic";
  return "executing";
}
function bytes(value: number) { if (!Number.isFinite(value) || value <= 0) return "0 B"; const units = ["B", "KB", "MB", "GB", "TB"]; const rank = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** rank).toFixed(rank ? 1 : 0)} ${units[rank]}`; }
function diagnosticId(error: unknown) { return error instanceof ApiError && error.code !== "unknown_error" ? error.code.replaceAll("_", "-").toUpperCase() : null; }
function message(error: unknown, fallback = "操作未完成，请重试。") { return error instanceof ApiError && error.code !== "unknown_error" ? error.message : fallback; }
type MigrationRequest = { kind: 'execute'; id: string; plan?: MigrationPlan } | { kind: 'retry' | 'acknowledge'; id: string; migrationId: string; revision: number };
const PENDING_KEY = 'venus.migration.pending';
function readPending(): { request: MigrationRequest | null; issue: string } {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (raw === null) return { request: null, issue: '' };
    const value = JSON.parse(raw);
    const identifier = (item: unknown): item is string => typeof item === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(item);
    if (!value || typeof value !== 'object' || Array.isArray(value) || !identifier(value.id)) throw new Error();
    const keys = Object.keys(value).sort().join(',');
    if (value.kind === 'execute' && keys === 'id,kind') return { request: { kind: 'execute', id: value.id }, issue: '' };
    if ((value.kind === 'retry' || value.kind === 'acknowledge') && keys === 'id,kind,migrationId,revision' && identifier(value.migrationId) && Number.isSafeInteger(value.revision) && value.revision >= 0) {
      return { request: { kind: value.kind, id: value.id, migrationId: value.migrationId, revision: value.revision }, issue: '' };
    }
    throw new Error();
  } catch { return { request: null, issue: '无法读取原操作标识，暂时无法确认升级结果。请重新读取状态；仍无法确认时，请联系开发者排查。' }; }
}
function savePending(operation: MigrationRequest) {
  const identity = operation.kind === 'execute' ? { kind: operation.kind, id: operation.id }
    : { kind: operation.kind, id: operation.id, migrationId: operation.migrationId, revision: operation.revision };
  sessionStorage.setItem(PENDING_KEY, JSON.stringify(identity));
}
function discoveryLabel(source: Pick<MigrationChoices, "trigger_mode" | "schedule_mode" | "daily_time" | "interval_minutes">) {
  if (source.trigger_mode === "manual") return "手动扫描";
  if (source.schedule_mode === "interval") return source.interval_minutes ? `每 ${source.interval_minutes} 分钟自动扫描` : '扫描间隔未记录';
  return source.daily_time ? `每天 ${source.daily_time} 自动扫描` : '扫描时间未记录';
}
function normalizedChoices(value: MigrationChoices): MigrationChoices {
  if (value.trigger_mode === "manual") return { ...value, schedule_mode: null, daily_time: null, interval_minutes: null };
  if (value.schedule_mode === "interval") return { ...value, schedule_mode: "interval", daily_time: null, interval_minutes: value.interval_minutes ?? 60 };
  return { ...value, schedule_mode: "daily", daily_time: value.daily_time ?? "22:00", interval_minutes: null };
}

export function MigrationFlow({ startup, onEnter }: Props) {
  const [stored] = useState(readPending); const [storageIssue, setStorageIssue] = useState<{ phase: 'read' | 'clear'; message: string } | null>(stored.issue ? { phase: 'read', message: stored.issue } : null);
  const [summary, setSummary] = useState(startup); const [source, setSource] = useState<MigrationSnapshot["source"] | null>(null);
  const [inspectionPlan, setInspectionPlan] = useState<MigrationPlan | null>(null); const [validatedPlan, setValidatedPlan] = useState<MigrationPlan | null>(null);
  const [choices, setChoices] = useState<MigrationChoices | null>(null); const [localScreen, setLocalScreen] = useState<Screen>("check");
  const [loading, setLoading] = useState(startup.entry !== "completed"); const [busy, setBusy] = useState(""); const [error, setError] = useState("");
  const [errorId, setErrorId] = useState<string | null>(null); const [fields, setFields] = useState<Record<string, string>>({}); const [connection, setConnection] = useState("");
  const [historyLimit, setHistoryLimit] = useState(20); const dialogRef = useRef<HTMLElement>(null); const titleRef = useRef<HTMLHeadingElement>(null);
  const pending = useRef(stored.request); const [uncertain, setUncertain] = useState(Boolean(pending.current)); const [enterFailed, setEnterFailed] = useState(false);
  const clearOperation = useCallback(() => {
    pending.current = null; setUncertain(false);
    try { sessionStorage.removeItem(PENDING_KEY); setStorageIssue(null); }
    catch { setStorageIssue({ phase: 'clear', message: '已收到操作结果，但无法清除本地操作标识。请重新读取状态后继续，不要重新提交升级。' }); }
  }, []); const loadRevision = useRef(0); const entered = useRef(false);

  const adopt = useCallback((next: MigrationSnapshot | MigrationStartupSummary) => {
    const operation = pending.current;
    if (operation && next.session && (operation.kind === 'execute' || operation.migrationId === next.session.migration_id && (operation.kind === 'acknowledge' ? Boolean(next.report?.acknowledged_at) : next.session.revision > operation.revision))) clearOperation();
    const inconsistent = next.entry === 'completed' && (!next.report?.backup_created || next.session?.backup_status !== 'completed');
    setSummary({ entry: inconsistent ? 'diagnostic' : next.entry, session: next.session, report: next.report });
    if ("source" in next) setSource(next.source);
  }, [clearOperation]);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const revision = ++loadRevision.current; const next = await projectApi.migration(signal);
    if (revision !== loadRevision.current) return next; adopt(next); return next;
  }, [adopt]);
  const enterProject = useCallback((projectId: string | null | undefined) => {
    if (!projectId?.trim() || entered.current) return;
    entered.current = true; loadRevision.current += 1; onEnter(projectId);
  }, [onEnter]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal).catch((caught) => {
      if (!controller.signal.aborted) { setError(message(caught, "无法读取升级状态，请重试。")); setErrorId(diagnosticId(caught)); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { loadRevision.current += 1; controller.abort(); };
  }, [refresh]);

  const active = summary.entry === "executing"; const confirming = busy === "acknowledge";
  useEffect(() => {
    if (!active && !confirming && !uncertain) return;
    let stopped = false; let timer = 0; let inFlight = false; let controller: AbortController | null = null;
    const poll = async () => {
      if (stopped || inFlight) return; inFlight = true; controller = new AbortController();
      try {
        const entering = confirming || pending.current?.kind === "acknowledge"; const next = await refresh(controller.signal); const projectId = entering && next.report?.acknowledged_at ? next.report.project.project_id : null;
        if (projectId) { stopped = true; enterProject(projectId); return; }
        if (!stopped) setConnection("");
      }
      catch (caught) { if (!stopped && !(caught instanceof DOMException && caught.name === "AbortError")) setConnection("进度刷新失败，当前显示的是上次读取的进度。"); }
      finally { inFlight = false; controller = null; if (!stopped) timer = window.setTimeout(() => void poll(), document.hidden ? 4000 : 1000); }
    };
    const visible = () => { if (!document.hidden) { if (timer) window.clearTimeout(timer); void poll(); } };
    void poll(); document.addEventListener("visibilitychange", visible);
    return () => { stopped = true; if (timer) window.clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visible); };
  }, [active, confirming, uncertain, enterProject, refresh]);

  const screen: Screen = summary.entry === "completed" ? summary.report?.backup_created && summary.session?.backup_status === "completed" ? "complete" : "diagnostic" : summary.entry === "incomplete" ? "incomplete" : summary.entry === "failed" ? "failed" : summary.entry === "diagnostic" ? "diagnostic" : summary.entry === "executing" ? "executing" : localScreen;
  const step = screen === "check" ? 0 : screen === "differences" ? 1 : ["confirm", "executing", "failed", "incomplete", "diagnostic"].includes(screen) ? 2 : 3;
  useEffect(() => { titleRef.current?.focus(); }, [screen]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const controls = [...dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]')];
      if (!controls.length) return; const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown); return () => document.removeEventListener("keydown", keydown);
  }, []);

  const clearError = () => { setError(""); setErrorId(null); setFields({}); };
  const backToCheck = useCallback(() => { if (pending.current) return; setInspectionPlan(null); setValidatedPlan(null); setChoices(null); setSummary((current) => ({ ...current, entry: "review" })); setLocalScreen("check");  clearError(); }, []);
  async function inspect() {
    if (busy) return; setBusy("inspect"); clearError();
    try { const result = await projectApi.migrationInspect(); setSource(result.source); setInspectionPlan(result.plan); setChoices(result.plan.choices); setValidatedPlan(null); setLocalScreen("differences"); }
    catch (caught) { setError(message(caught, "检查未完成，请重试。")); setErrorId(diagnosticId(caught)); }
    finally { setBusy(""); setLoading(false); }
  }
  function updateChoice(patch: Partial<MigrationChoices>) {
    if (pending.current) return;
    setChoices((current) => {
      if (!current) return current; let next = { ...current, ...patch };
      if (patch.trigger_mode === "scheduled" && next.schedule_mode === null) next = { ...next, schedule_mode: "daily", daily_time: "22:00" };
      return normalizedChoices(next);
    });
    setValidatedPlan(null); clearError();
  }
  async function selectDirectory(field: "source_directory" | "output_directory") {
    try { if (!window.liveClipperShell?.selectFolder) throw new Error(); const selected = await window.liveClipperShell.selectFolder(field === "source_directory" ? "选择录像文件夹" : "选择成片保存位置"); if (selected) updateChoice({ [field]: selected }); } catch (caught) { setError(message(caught, "无法选择文件夹，请重试。")); }
  }
  async function validate() {
    if (!inspectionPlan || !choices || busy) return; setBusy("validate"); clearError();
    try { const result = await projectApi.migrationValidate(inspectionPlan.source_fingerprint, inspectionPlan.plan_hash, normalizedChoices(choices)); setValidatedPlan(result.plan); setChoices(result.plan.choices); setLocalScreen("confirm"); }
    catch (caught) {
      if (caught instanceof ApiError && ["migration_source_changed", "migration_plan_changed"].includes(caught.code)) { backToCheck(); setError("旧版数据或升级设置已变化，请重新检查。"); }
      else { setError(message(caught, "检查未通过，请查看提示并修改。")); setErrorId(diagnosticId(caught)); if (caught instanceof ApiError) setFields(caught.fields); }
    } finally { setBusy(""); }
  }
  async function submit(operation: MigrationRequest) {
    if (busy) return;
    if (storageIssue) return;
    const recovering = Boolean(pending.current); const original = pending.current ?? operation;
    if (original.kind === 'execute' && !original.plan) return;
    try { savePending(original); }
    catch { setError('本次请求未发送：无法保存操作标识，请重试。'); return; }
    pending.current = original; setBusy(original.kind); clearError();
    let writing = false;
    try {
      if (recovering) {
        const current = await refresh();
        if (!pending.current) { if (original.kind === 'acknowledge' && current.report?.acknowledged_at) enterProject(current.report.project.project_id); return; }
        if (original.kind !== 'execute' && current.session?.migration_id !== original.migrationId) { setUncertain(true); setError('无法核对原升级会话，请重新读取状态。'); return; }
      }
      writing = true;
      const result = original.kind === 'execute' ? await projectApi.migrationExecute(original.id, original.plan!)
        : original.kind === 'retry' ? await projectApi.migrationRetry(original.id, original.migrationId, original.revision)
        : await projectApi.migrationAcknowledge(original.id, original.migrationId, original.revision);
      if (original.kind === 'acknowledge') {
        if (!('project_id' in result) || typeof result.project_id !== 'string' || !result.project_id.trim()) throw new ApiError('无法读取进入项目的结果。', 200, 'invalid_response');
        clearOperation(); enterProject(result.project_id); return;
      }
      clearOperation();
      adopt({ entry: result.session.state.startsWith("completed_") ? "executing" : entryFor(result.session), session: result.session, report: null });
      if (result.session.state.startsWith('completed_')) { try { await refresh(); } catch { setConnection('无法读取升级状态，请重试。'); } }
    } catch (caught) {
      const unknown = !writing || !(caught instanceof ApiError) || caught.outcomeUnknown;
      const conflict = caught instanceof ApiError && ['request_id_conflict', 'migration_conflict'].includes(caught.code);
      if (unknown || conflict) {
        setUncertain(true);
        try { const current = await refresh(); if (!pending.current) { if (original.kind === 'acknowledge' && current.report?.acknowledged_at) enterProject(current.report.project.project_id); return; } } catch { /* Retain the submitted identity until an outcome can be read. */ }
        setError(original.kind === 'acknowledge' ? '暂时无法确认能否进入项目，请点击“重试进入项目”。数据升级已完成。' : original.kind === 'retry' ? '暂时无法确认重试结果。请保留此页面，点击“继续本次操作”。' : '暂时无法确认升级是否已开始。请保留此页面，点击“继续本次操作”重试。');
      } else {
        clearOperation(); setError(message(caught)); setErrorId(diagnosticId(caught));
        if (caught instanceof ApiError) {
          setFields(Object.fromEntries(Object.entries(caught.fields).map(([field, reason]) => [field, reason === 'required' ? '请补充此项设置。' : reason])));
          if (original.kind === 'execute' && ['migration_source_changed', 'migration_plan_changed'].includes(caught.code)) { backToCheck(); setError('旧版数据或升级设置已变化，请重新检查。'); }
          else if (original.kind === 'execute' && Object.keys(caught.fields).length) { setLocalScreen('differences'); }
          else { try { await refresh(); } catch { setConnection('无法读取升级状态，请重试。'); } }
        }
      }
      if (original.kind === 'acknowledge') setEnterFailed(true);
    } finally { setBusy(''); }
  }
  function execute() { if (pending.current) return submit(pending.current); if (validatedPlan) return submit({ kind: 'execute', id: requestId('migration-execute'), plan: validatedPlan }); }
  function retry() { if (pending.current) return submit(pending.current); if (summary.session) return submit({ kind: 'retry', id: requestId('migration-retry'), migrationId: summary.session.migration_id, revision: summary.session.revision }); }
  function acknowledge() { if (pending.current) return submit(pending.current); if (summary.session) return submit({ kind: 'acknowledge', id: requestId('migration-acknowledge'), migrationId: summary.session.migration_id, revision: summary.session.revision }); }
  const unresolved = Boolean(pending.current && (pending.current.kind === 'execute' ? !pending.current.plan : summary.session?.migration_id !== pending.current.migrationId));
  async function reread() {
    if (busy) return; setBusy('read'); clearError();
    try { const next = await refresh(); if (storageIssue && (storageIssue.phase === 'clear' || next.session)) clearOperation(); }
    catch (caught) { setError(message(caught, '无法读取升级状态，请重试。')); }
    finally { setBusy(''); }
  }
  async function showBackup() { const id = summary.session?.migration_id; if (!id || !window.liveClipperShell?.showBackup) return; setBusy('backup'); clearError(); try { const result = await window.liveClipperShell.showBackup(id); if (!result.ok) { setError(result.message || '无法显示升级备份，请稍后重试。'); setErrorId(result.code?.replaceAll('_', '-').toUpperCase() || null); } } catch (caught) { setError(message(caught, '无法在 Finder 中显示备份，请稍后重试。')); } finally { setBusy(''); } }

  return <div className="migration-layer"><section className="migration-shell" role="dialog" aria-modal="true" aria-labelledby="migration-title" ref={dialogRef}>
    <header className="migration-header"><div className="migration-brand"><img src="/static/venus-mark.png" alt="" /><strong>Venus</strong></div><div><span>旧版数据升级</span>{active && <small>正在升级，请保持 Venus 运行。</small>}</div></header>
    <div className="migration-layout"><aside className="migration-steps" aria-label="升级步骤">{STEPS.map(([label, note], index) => <div className={index === step ? "active" : index < step ? "done" : ""} key={label}><span>{index < step ? <RemixIcon name="check" /> : index + 1}</span><div><strong>{label}</strong><small>{note}</small></div></div>)}<p>升级不会移动或删除原始录像。</p></aside>
      <main className="migration-content">{error && <div className="migration-error" role="alert"><span>{error}</span>{errorId && <small>问题编号：{errorId}</small>}</div>}{connection && <div className="migration-connection" role="status">{connection}</div>}
        {storageIssue || unresolved ? <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>正在核对升级结果</h1><p>{storageIssue?.message || '原请求内容已不在当前页面，暂时无法确认升级结果。请重新读取状态；仍无法确认时，请联系开发者排查。'}</p></div><footer className="migration-footer"><button className="button primary" disabled={Boolean(busy)} onClick={() => void reread()}>重新读取状态</button></footer></div>
          : loading && !source && screen === "check" ? <Loading /> : screen === "check" ? <CheckStep source={source} busy={busy} inspect={inspect} quit={() => void window.liveClipperShell?.quitApp?.()} quitAvailable={Boolean(window.liveClipperShell?.quitApp)} error={error} titleRef={titleRef} />
          : screen === "differences" && inspectionPlan && choices ? <DifferenceStep plan={inspectionPlan} choices={choices} fields={fields} busy={busy} historyLimit={historyLimit} update={updateChoice} select={selectDirectory} more={() => setHistoryLimit((value) => value + 20)} back={backToCheck} next={validate} titleRef={titleRef} />
          : screen === "confirm" && validatedPlan ? <ConfirmStep plan={validatedPlan} uncertain={uncertain} busy={busy} back={() => { if (!pending.current) setLocalScreen("differences"); }} execute={execute} titleRef={titleRef} />
          : screen === "executing" && summary.session ? <ExecutingStep session={summary.session} titleRef={titleRef} />
          : screen === "complete" && summary.session && summary.report ? <CompleteStep retryEnter={enterFailed || uncertain} session={summary.session} report={summary.report} busy={busy} enter={acknowledge} backup={showBackup} canShowBackup={Boolean(window.liveClipperShell?.showBackup)} titleRef={titleRef} />
          : screen === "incomplete" && summary.session ? <IncompleteStep session={summary.session} busy={busy} retry={retry} backup={showBackup} canShowBackup={Boolean(window.liveClipperShell?.showBackup)} titleRef={titleRef} />
          : screen === "failed" && summary.session ? <FailedStep readState={() => { void refresh().catch(caught => setError(message(caught, "无法读取升级状态，请重试。"))); }} uncertain={uncertain} session={summary.session} busy={busy} retry={retry} quit={() => void window.liveClipperShell?.quitApp?.()} quitAvailable={Boolean(window.liveClipperShell?.quitApp)} titleRef={titleRef} />
          : <Diagnostic titleRef={titleRef} code={summary.session?.failure?.code ?? errorId ?? (summary.entry === "diagnostic" ? "migration_integrity_failed" : null)} />}
      </main></div>
  </section></div>;
}

function Loading() { return <div className="migration-loading" role="status"><span className="migration-spinner" /><strong>正在读取升级进度…</strong></div>; }
function CheckStep({ source, busy, inspect, quit, quitAvailable, error, titleRef }: { source: MigrationSnapshot["source"] | null; busy: string; inspect(): void; quit(): void; quitAvailable: boolean; error: string; titleRef: RefObject<HTMLHeadingElement | null> }) {
  return <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>检查旧版数据</h1><p>发现旧版设置和处理记录。先检查这些内容，再由你确认是否升级。检查不会修改旧版数据。</p><section className="migration-source-card"><div><span>发现的旧版数据</span><strong>{source ? `${source.display_summary.metadata_file_count} 个设置和记录文件` : "旧版数据"}</strong><small>{source?.display_summary.history_count !== undefined ? `${source.display_summary.history_count} 条历史记录` : "检查后可查看历史记录的处理方式。"}</small></div></section><details className="migration-details"><summary>检查哪些内容？</summary><p>检查旧版设置、处理记录和定时扫描设置，不读取录像内容。本次检查在本机完成，不向外部服务发送这些数据。</p></details>{error && <p className="migration-safe-note">本次检查未完成，未修改旧版数据。</p>}</div><footer className="migration-footer"><button className="button" disabled={!quitAvailable || Boolean(busy)} onClick={quit}>退出 Venus</button><span /><button className="button primary" disabled={Boolean(busy)} onClick={inspect}>{busy === "inspect" ? "检查中…" : error ? "重新检查" : "开始检查"}</button></footer></div>;
}
function DifferenceStep({ plan, choices, fields, busy, historyLimit, update, select, more, back, next, titleRef }: { plan: MigrationPlan; choices: MigrationChoices; fields: Record<string, string>; busy: string; historyLimit: number; update(value: Partial<MigrationChoices>): void; select(field: "source_directory" | "output_directory"): void; more(): void; back(): void; next(): void; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const [historyOpen, setHistoryOpen] = useState(false); const required = new Set(plan.required_choices); const counts = plan.history.counts; const total = counts.importable + counts.compatibility + counts.quarantined;
  const incomplete = [...required].some((field) => field === "project_name" ? !choices.project_name.trim() : field === "source_directory" ? !choices.source_directory : field === "output_directory" ? !choices.output_directory : false);
  return <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>{required.size ? "核对升级内容" : "核对升级内容"}</h1><p>{required.size ? "请补充标出的设置，并核对项目、模型和历史记录的处理方式。" : "请核对项目、模型和历史记录的处理方式。"}</p>
    <div className="migration-grid"><PlanCard title="升级后的项目" state={plan.readiness.source_status === "ready" && plan.readiness.output_status === "ready" ? "" : "待补充"}><Fact label="项目名称" value={choices.project_name} /><Fact label="录像文件夹" value={choices.source_directory} /><Fact label="成片保存位置" value={choices.output_directory} />{required.has("project_name") && <TextInput label="项目名称" onChange={(value) => update({ project_name: value })} status={fields.project_name ? { type: "error", message: fields.project_name } : undefined} value={choices.project_name} width="100%" />}{required.has("source_directory") && <PathField choose={() => select("source_directory")} error={fields.source_directory} isReadOnly label="录像文件夹" value={choices.source_directory} />}{required.has("output_directory") && <PathField choose={() => select("output_directory")} error={fields.output_directory} isReadOnly label="成片保存位置" value={choices.output_directory} />}</PlanCard>
      <PlanCard title="模型设置" state="升级后检查">{Object.entries(plan.resources).map(([id, resource]) => <div className="migration-resource" key={id}><div><strong>{resource.label}</strong><small>{resource.model || '旧设置中未找到模型'} · {resource.connection_type === 'local' ? '本机识别' : resource.credential_present ? '旧设置中已填写 API Key' : '旧设置中未找到 API Key'}</small></div><span className="status-pill attention">{resource.status === 'ready' ? '待检查' : '待补充'}</span></div>)}<p className="migration-card-note">升级后项目会保持未启用。请先检查模型和项目设置，再启用项目。</p></PlanCard>
      <PlanCard title="历史记录" state={`${total} 条`}><div className="migration-counts"><Fact label="可导入记录" value={String(counts.importable)} /><Fact label="旧版待处理记录" value={String(counts.compatibility)} /><Fact label="不导入项目" value={String(counts.quarantined)} /><Fact label="待核验成片" value={String(counts.safe_result)} /></div>{counts.quarantined > 0 && <p>不导入项目的记录将保留在升级备份中。</p>}<details className="migration-details" open={historyOpen}><summary aria-expanded={historyOpen} onClick={(event) => { event.preventDefault(); setHistoryOpen((value) => !value); }}>查看记录明细</summary><div className="migration-history">{plan.history.entries.slice(0, historyLimit).map((item) => <div key={item.display_identity}><span>{item.display_identity}</span><small>{item.reason_label}</small></div>)}{historyLimit < plan.history.entries.length && <button className="text-button" onClick={more}>显示更多</button>}</div></details></PlanCard>
      <PlanCard title="扫描方式" state={required.has("trigger_mode") ? "待补充" : ""}><p className="migration-card-value">{discoveryLabel(choices)}{choices.trigger_mode === "scheduled" ? `（${plan.project.timezone}）` : ""}</p>{required.has("trigger_mode") && <fieldset className="migration-choice"><legend>升级后如何扫描新录像？</legend><label><input type="radio" checked={choices.trigger_mode === "manual"} onChange={() => update({ trigger_mode: "manual" })} />手动扫描</label><label><input type="radio" checked={choices.trigger_mode === "scheduled"} onChange={() => update({ trigger_mode: "scheduled" })} />定时扫描（也可手动）</label>{choices.trigger_mode === "scheduled" && <FormLayout className="form-subgroup form-surface"><FormLayout className="form-pair"><Selector label="定时方式" onChange={(value) => update({ schedule_mode: value as "daily" | "interval" })} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "固定间隔" }]} value={choices.schedule_mode || "daily"} width="100%" />{choices.schedule_mode === "interval" ? <Selector label="扫描间隔" onChange={(value) => update({ interval_minutes: Number(value) })} options={[{ value: "30", label: "30 分钟" }, { value: "60", label: "1 小时" }, { value: "180", label: "3 小时" }, { value: "360", label: "6 小时" }, { value: "720", label: "12 小时" }]} value={String(choices.interval_minutes ?? 60)} width="100%" /> : <Field inputID="migration-daily-time" label="扫描时间" width="100%"><input className="form-control" id="migration-daily-time" type="time" value={choices.daily_time ?? "22:00"} onChange={(event) => update({ daily_time: event.target.value })} /></Field>}</FormLayout></FormLayout>}</fieldset>}</PlanCard>
    </div><section className={`migration-backup ${plan.backup.space_status}`}><div><strong>升级前会备份旧版设置和记录</strong><p>备份文件夹：{plan.backup.target_display} · 所需可用空间： {bytes(plan.backup.required_bytes)} · 当前可用 {bytes(plan.backup.available_bytes)}</p></div><span>{plan.backup.space_status === "ready" ? "备份空间充足" : "备份空间不足，请释放磁盘空间后返回重新检查。"}</span></section></div><footer className="migration-footer"><button className="button" disabled={Boolean(busy)} onClick={back}>上一步</button><span /><small>下一步核对最终设置，确认后才会开始升级。</small><button className="button primary" disabled={Boolean(busy) || incomplete || plan.backup.space_status !== "ready"} onClick={next}>{busy === "validate" ? "检查中…" : "下一步"}</button></footer></div>;
}
function ConfirmStep({ plan, busy, uncertain, back, execute, titleRef }: { plan: MigrationPlan; busy: string; uncertain: boolean; back(): void; execute(): void; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const counts = plan.history.counts;
  return <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>确认升级</h1><p>Venus 会先备份并检查旧版设置和记录，再创建项目、导入可导入的历史记录，并核验关联成片。</p><dl className="migration-review"><Fact label="项目名称" value={plan.project.name} /><Fact label="录像文件夹" value={plan.project.source_directory} /><Fact label="成片保存位置" value={plan.project.output_directory} /><Fact label="扫描方式" value={discoveryLabel(plan.choices) + (plan.choices.trigger_mode === 'scheduled' ? `（${plan.project.timezone}）` : '')} /><Fact label="历史记录" value={`共 ${counts.importable + counts.compatibility + counts.quarantined} 条${counts.quarantined ? `，其中 ${counts.quarantined} 条不导入项目，将保留在升级备份中。` : ''}`} /><Fact label="升级后项目状态" value="未启用，需检查模型和项目设置。" /><Fact label="升级备份" value={`文件夹：${plan.backup.target_display} · 所需可用空间：${bytes(plan.backup.required_bytes)}`} /></dl><div className="migration-confirm-note"><strong>原始录像不会被移动或删除</strong></div></div><footer className="migration-footer"><button className="button" disabled={Boolean(busy) || uncertain} onClick={back}>返回修改</button><span /><small>开始后请保持 Venus 运行，等待升级结果。</small><button className="button primary" disabled={Boolean(busy)} onClick={execute}>{busy === 'execute' ? '正在提交…' : uncertain ? '继续本次操作' : '开始升级'}</button></footer></div>;
}
function ExecutingStep({ session, titleRef }: { session: MigrationSession; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const backedUp = session.backup_status === 'completed'; const importing = backedUp && ['project', 'history', 'database', 'resources'].includes(session.stage || '');
  return <div className="migration-step"><div className="migration-scroll migration-executing"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>{session.state.startsWith("completed_") ? "正在读取升级结果" : "正在升级"}</h1><p>请保持 Venus 运行。完成后会显示升级结果。</p><div className="migration-stage-list" aria-live="polite"><div className={backedUp ? 'done' : session.stage === 'copy' ? 'active' : ''}><span>{backedUp ? <RemixIcon name="check" /> : '1'}</span><div><strong>备份旧版数据</strong>{!backedUp && session.stage === 'copy' && <small>进行中</small>}</div></div><div className={importing ? 'active' : ''}><span>2</span><div><strong>{session.stage === "resources" ? "转换模型配置" : "导入旧版数据与模型配置"}</strong>{importing && <small>进行中</small>}</div></div></div></div></div>;
}
function IncompleteStep({ session, busy, retry, backup, canShowBackup, titleRef }: { session: MigrationSession; busy: string; retry(): void; backup(): void; canShowBackup: boolean; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const [history, setHistory] = useState<MigrationSavedHistory['history'] | null>(null);
  const [historyError, setHistoryError] = useState(''); const [reading, setReading] = useState(false);
  async function readHistory() {
    setReading(true); setHistoryError('');
    try { setHistory((await projectApi.migrationHistory(session.migration_id)).history); }
    catch (caught) { setHistoryError(message(caught, '无法读取已保存的历史记录，请重试。')); }
    finally { setReading(false); }
  }
  return <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>升级尚未完成</h1><p>项目和历史记录已保存，模型配置还需要完成转换。继续升级会保留这些数据。</p>{session.failure && <p role="alert">{session.failure.summary}</p>}{session.failure?.code && <small className="migration-diagnostic-id">问题编号：{session.failure.code.replaceAll('_', '-').toUpperCase()}</small>}<p>完成前暂时不能修改项目或开始处理录像。</p><button className="button" disabled={reading} onClick={() => void readHistory()}>{reading ? '正在读取…' : '查看已保存的历史记录'}</button>{historyError && <p role="alert">{historyError}</p>}{history && <section aria-label="已保存的历史记录"><p>已保存 {history.length} 条记录。</p>{history.map((run, index) => <p key={run.run_id}>历史记录 {index + 1} · {run.status === 'completed' ? '处理完成' : run.status === 'failed' ? '处理失败' : '已保存'} · {run.created_at}</p>)}</section>}</div><footer className="migration-footer"><button className="button" disabled={!canShowBackup || Boolean(busy) || session.backup_status !== 'completed'} onClick={backup}>在 Finder 中显示备份</button><span /><button className="button primary" disabled={Boolean(busy)} onClick={retry}>{busy === 'retry' ? '正在继续…' : '继续升级'}</button></footer></div>;
}
function CompleteStep({ session, report, busy, retryEnter, enter, backup, canShowBackup, titleRef }: { session: MigrationSession; report: MigrationReport; busy: string; retryEnter: boolean; enter(): void; backup(): void; canShowBackup: boolean; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const attention = session.state === 'completed_attention';
  return <div className="migration-step"><div className="migration-scroll migration-complete"><span className="migration-complete-mark"><RemixIcon name="check" /></span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>数据升级完成</h1><p>{attention ? '请进入项目，查看待处理问题，检查模型和项目设置。' : '项目已创建，导入结果如下。'}</p><dl className="migration-review"><Fact label="项目名称" value={report.project.name} /><Fact label="扫描方式" value={discoveryLabel(report.discovery) + (report.discovery.trigger_mode === "scheduled" && report.discovery.timezone ? `（${report.discovery.timezone}）` : "")} /><Fact label="历史记录" value={`共 ${report.history_total} 条，已导入 ${report.imported + report.compatibility} 条。${report.compatibility ? `其中 ${report.compatibility} 条为旧版待处理记录。` : ''}${report.quarantined ? `另有 ${report.quarantined} 条未导入项目，已保留在升级备份中。` : ''}`} /><Fact label="已导入成片" value={`${report.safe_results} 个`} /><Fact label="升级备份" value="已备份并检查" /><Fact label="项目状态" value={attention ? `${report.blocker_count} 个问题待处理。` : '升级时检查通过。'} /></dl></div><footer className="migration-footer"><button className="button" disabled={!canShowBackup || Boolean(busy)} onClick={backup}>{busy === 'backup' ? '正在显示…' : '在 Finder 中显示备份'}</button><span /><button className="button primary" disabled={Boolean(busy)} onClick={enter}>{busy === 'acknowledge' ? '正在进入…' : retryEnter ? '重试进入项目' : '进入项目'}</button></footer></div>;
}
function FailedStep({ session, busy, uncertain, retry, readState, quit, quitAvailable, titleRef }: { session: MigrationSession; busy: string; uncertain: boolean; retry(): void; readState(): void; quit(): void; quitAvailable: boolean; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const incompatible = ['migration_source_changed', 'migration_plan_changed'].includes(session.failure?.code || '');
  return <div className="migration-step"><div className="migration-scroll"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>{uncertain ? '正在核对升级结果' : '升级未完成'}</h1><p>{uncertain ? '暂时无法确认本次操作结果，请保持 Venus 运行。' : session.failure?.summary || '本次升级未修改旧版数据。请查看问题提示后重试。'}</p>{!uncertain && <div className="migration-failure-facts"><Fact label="旧版数据" value="本次升级未修改" /><Fact label="升级后的项目" value="未创建" /><Fact label="升级备份" value={session.backup_status === 'completed' ? '已完成，重试时会重新检查。' : '尚未完成'} /></div>}{session.failure?.code && <small className="migration-diagnostic-id">问题编号：{session.failure.code.replaceAll('_', '-').toUpperCase()}</small>}</div><footer className="migration-footer"><button className="button" disabled={!quitAvailable || Boolean(busy) || uncertain} onClick={quit}>退出 Venus</button><span />{!uncertain && <small>已有备份会先重新检查，通过后继续使用。</small>}<button className="button primary" disabled={Boolean(busy)} onClick={incompatible && !uncertain ? readState : retry}>{busy === 'retry' ? '正在重试…' : uncertain ? '继续本次操作' : incompatible ? '重新读取状态' : '重试升级'}</button></footer></div>;
}
function Diagnostic({ titleRef, code }: { titleRef: RefObject<HTMLHeadingElement | null>; code: string | null | undefined }) { return <div className="migration-step"><div className="migration-scroll migration-diagnostic"><h1 id="migration-title" ref={titleRef} tabIndex={-1}>暂时无法确认数据状态</h1><p>{code ? '请记录问题编号并联系开发者排查。' : '暂未取得问题编号，请记录当前页面并联系开发者排查。'}</p>{code && <small>问题编号：{code.replaceAll('_', '-').toUpperCase()}</small>}</div></div>; }
function PlanCard({ title, state, children }: { title: string; state: string; children: ReactNode }) { return <section className="migration-plan-card"><header><h2>{title}</h2>{state && <span>{state}</span>}</header><div>{children}</div></section>; }
function Fact({ label, value }: { label: string; value: string }) { return <div className="migration-fact"><dt>{label}</dt><dd title={value}>{value}</dd></div>; }
