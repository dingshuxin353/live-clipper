import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";

import { ApiError } from "../../api";
import { projectApi, requestId } from "../../project-api";
import type { MigrationChoices, MigrationPlan, MigrationReport, MigrationSession, MigrationSnapshot, MigrationStartupSummary } from "../../project-dto";

const STEPS = [
  ["检查升级", "读取旧版设置"],
  ["处理差异", "确认升级内容"],
  ["确认执行", "备份并迁移"],
  ["完成", "进入项目"],
] as const;
const STAGES = [
  ["copy", "创建备份"], ["project", "创建项目"], ["history", "导入历史记录"],
  ["database", "核对结果与数据库"], ["complete", "切换到新工作台"],
] as const;
const ACTIVE_STATES = new Set(["backing_up", "migrating", "validating"]);

type Props = { startup: MigrationStartupSummary; onEnter(projectId: string): void };
type Screen = "check" | "differences" | "confirm" | "executing" | "complete" | "failed" | "diagnostic";

function entryFor(session: MigrationSession): MigrationStartupSummary["entry"] {
  if (session.state.startsWith("completed_")) return "completed";
  if (session.state === "failed_rolled_back") return "failed";
  if (session.state === "diagnostic_required") return "diagnostic";
  return "executing";
}
function bytes(value: number) { if (!Number.isFinite(value) || value <= 0) return "0 B"; const units = ["B", "KB", "MB", "GB", "TB"]; const rank = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** rank).toFixed(rank ? 1 : 0)} ${units[rank]}`; }
function diagnosticId(error: unknown) { return error instanceof ApiError && error.code !== "unknown_error" ? error.code.replaceAll("_", "-").toUpperCase() : null; }
function message(error: unknown, fallback = "暂时无法完成此操作") { return error instanceof ApiError && error.code !== "unknown_error" ? error.message : fallback; }
function discoveryLabel(source: Pick<MigrationChoices, "trigger_mode" | "schedule_mode" | "daily_time" | "interval_minutes">) {
  if (source.trigger_mode === "manual") return "仅手动检查新录像";
  if (source.schedule_mode === "interval") return `每 ${source.interval_minutes ?? 60} 分钟自动检查`;
  return `每天 ${source.daily_time ?? "22:00"} 自动检查`;
}
function normalizedChoices(value: MigrationChoices): MigrationChoices {
  if (value.trigger_mode === "manual") return { ...value, schedule_mode: null, daily_time: null, interval_minutes: null };
  if (value.schedule_mode === "interval") return { ...value, schedule_mode: "interval", daily_time: null, interval_minutes: value.interval_minutes ?? 60 };
  return { ...value, schedule_mode: "daily", daily_time: value.daily_time ?? "22:00", interval_minutes: null };
}

export function MigrationFlow({ startup, onEnter }: Props) {
  const [summary, setSummary] = useState(startup); const [source, setSource] = useState<MigrationSnapshot["source"] | null>(null);
  const [inspectionPlan, setInspectionPlan] = useState<MigrationPlan | null>(null); const [validatedPlan, setValidatedPlan] = useState<MigrationPlan | null>(null);
  const [choices, setChoices] = useState<MigrationChoices | null>(null); const [localScreen, setLocalScreen] = useState<Screen>("check");
  const [loading, setLoading] = useState(startup.entry !== "completed"); const [busy, setBusy] = useState(""); const [error, setError] = useState("");
  const [errorId, setErrorId] = useState<string | null>(null); const [fields, setFields] = useState<Record<string, string>>({}); const [connection, setConnection] = useState("");
  const [historyLimit, setHistoryLimit] = useState(20); const dialogRef = useRef<HTMLElement>(null); const titleRef = useRef<HTMLHeadingElement>(null);
  const executeId = useRef(""); const retryId = useRef(""); const acknowledgeId = useRef(""); const loadRevision = useRef(0); const entered = useRef(false);

  const adopt = useCallback((next: MigrationSnapshot | MigrationStartupSummary) => {
    setSummary({ entry: next.entry, session: next.session, report: next.report });
    if ("source" in next) setSource(next.source);
  }, []);
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
      if (!controller.signal.aborted) { setError(message(caught, "暂时无法读取升级状态")); setErrorId(diagnosticId(caught)); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { loadRevision.current += 1; controller.abort(); };
  }, [refresh]);

  const active = Boolean(summary.session && ACTIVE_STATES.has(summary.session.state)); const confirming = busy === "acknowledge";
  useEffect(() => {
    if (!active && !confirming) return;
    let stopped = false; let timer = 0; let inFlight = false; let controller: AbortController | null = null;
    const poll = async () => {
      if (stopped || inFlight) return; inFlight = true; controller = new AbortController();
      try {
        const next = await refresh(controller.signal); const projectId = confirming && next.report?.acknowledged_at ? next.report.project.project_id : null;
        if (projectId) { stopped = true; enterProject(projectId); return; }
        if (!stopped) setConnection("");
      }
      catch (caught) { if (!stopped && !(caught instanceof DOMException && caught.name === "AbortError")) setConnection("暂时无法刷新，正在保留上一次进度"); }
      finally { inFlight = false; controller = null; if (!stopped) timer = window.setTimeout(() => void poll(), document.hidden ? 4000 : 1000); }
    };
    const visible = () => { if (!document.hidden) { if (timer) window.clearTimeout(timer); void poll(); } };
    void poll(); document.addEventListener("visibilitychange", visible);
    return () => { stopped = true; if (timer) window.clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visible); };
  }, [active, confirming, enterProject, refresh]);

  const screen: Screen = summary.entry === "completed" ? "complete" : summary.entry === "failed" ? "failed" : summary.entry === "diagnostic" ? "diagnostic" : summary.entry === "executing" ? "executing" : localScreen;
  const step = screen === "check" ? 0 : screen === "differences" ? 1 : ["confirm", "executing", "failed", "diagnostic"].includes(screen) ? 2 : 3;
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
  const backToCheck = useCallback(() => { setInspectionPlan(null); setValidatedPlan(null); setChoices(null); setSummary((current) => ({ ...current, entry: "review" })); setLocalScreen("check"); executeId.current = ""; retryId.current = ""; clearError(); }, []);
  async function inspect() {
    if (busy) return; setBusy("inspect"); clearError();
    try { const result = await projectApi.migrationInspect(); setSource(result.source); setInspectionPlan(result.plan); setChoices(result.plan.choices); setValidatedPlan(null); setLocalScreen("differences"); }
    catch (caught) { setError(message(caught, "暂时无法检查升级内容")); setErrorId(diagnosticId(caught)); }
    finally { setBusy(""); setLoading(false); }
  }
  function updateChoice(patch: Partial<MigrationChoices>) {
    setChoices((current) => {
      if (!current) return current; let next = { ...current, ...patch };
      if (patch.trigger_mode === "scheduled" && next.schedule_mode === null) next = { ...next, schedule_mode: "daily", daily_time: "22:00" };
      return normalizedChoices(next);
    });
    setValidatedPlan(null); executeId.current = ""; clearError();
  }
  async function selectDirectory(field: "source_directory" | "output_directory") {
    const selected = await window.liveClipperShell?.selectFolder?.(field === "source_directory" ? "选择录像目录" : "选择成片保存位置");
    if (selected) updateChoice({ [field]: selected });
  }
  async function validate() {
    if (!inspectionPlan || !choices || busy) return; setBusy("validate"); clearError();
    try { const result = await projectApi.migrationValidate(inspectionPlan.source_fingerprint, inspectionPlan.plan_hash, normalizedChoices(choices)); setValidatedPlan(result.plan); setChoices(result.plan.choices); setLocalScreen("confirm"); }
    catch (caught) {
      if (caught instanceof ApiError && ["migration_source_changed", "migration_plan_changed"].includes(caught.code)) { backToCheck(); setError("旧版数据已变化，请重新检查升级内容"); }
      else { setError(message(caught, "升级内容未通过检查")); setErrorId(diagnosticId(caught)); if (caught instanceof ApiError) setFields(caught.fields); }
    } finally { setBusy(""); }
  }
  async function recoverUncertain() { try { return await refresh(); } catch { return null; } }
  async function execute() {
    if (!validatedPlan || busy) return; setBusy("execute"); clearError(); if (!executeId.current) executeId.current = requestId("migration-execute");
    try {
      const failedSession = summary.session?.state === "failed_rolled_back" ? summary.session : null;
      const result = failedSession
        ? await projectApi.migrationRetry(executeId.current, failedSession.migration_id, failedSession.revision)
        : await projectApi.migrationExecute(executeId.current, validatedPlan);
      setSummary({ entry: entryFor(result.session), session: result.session, report: null });
    }
    catch (caught) {
      if (caught instanceof ApiError && caught.code === "network_error") { const current = await recoverUncertain(); if (current?.session) return; setError("升级请求暂时无法确认，请保持当前窗口并使用同一次请求重试"); }
      else if (caught instanceof ApiError && ["migration_source_changed", "migration_plan_changed"].includes(caught.code)) { backToCheck(); setError("升级条件已变化，请重新检查"); }
      else { setError(message(caught, "暂时无法开始升级")); setErrorId(diagnosticId(caught)); }
    } finally { setBusy(""); }
  }
  async function retry() {
    const session = summary.session; if (!session || busy) return; setBusy("retry"); clearError(); if (!retryId.current) retryId.current = requestId("migration-retry");
    try { const result = await projectApi.migrationRetry(retryId.current, session.migration_id, session.revision); retryId.current = ""; setSummary({ entry: entryFor(result.session), session: result.session, report: summary.report }); }
    catch (caught) {
      if (caught instanceof ApiError && ["migration_source_changed", "migration_plan_changed"].includes(caught.code)) { backToCheck(); setError("升级条件已变化。请修复后重新检查，再继续同一次升级。"); }
      else if (caught instanceof ApiError && caught.code === "network_error") { const current = await recoverUncertain(); if (current?.session && current.session.state !== "failed_rolled_back") return; setError("重试结果暂时无法确认，请使用同一次请求继续"); }
      else { setError(message(caught, "暂时无法重新尝试升级")); setErrorId(diagnosticId(caught)); }
    } finally { setBusy(""); }
  }
  async function acknowledge() {
    const session = summary.session; const projectId = summary.report?.project.project_id ?? session?.project_id; if (!session || !projectId || busy) return;
    setBusy("acknowledge"); clearError(); if (!acknowledgeId.current) acknowledgeId.current = requestId("migration-acknowledge");
    try { const result = await projectApi.migrationAcknowledge(acknowledgeId.current, session.migration_id, session.revision); enterProject(result.project_id); }
    catch (caught) {
      if (caught instanceof ApiError && caught.code === "network_error") { const current = await recoverUncertain(); if (current?.report?.acknowledged_at) { enterProject(current.report.project.project_id); return; } setError("确认结果暂时无法读取，请保持当前窗口后重试"); }
      else { setError(message(caught, "暂时无法进入项目")); setErrorId(diagnosticId(caught)); }
    } finally { setBusy(""); }
  }
  async function showBackup() { const id = summary.session?.migration_id; if (!id || !window.liveClipperShell?.showBackup) return; setBusy("backup"); clearError(); try { await window.liveClipperShell.showBackup(id); } catch { setError("暂时无法在 Finder 中显示备份"); } finally { setBusy(""); } }

  return <div className="migration-layer"><section className="migration-shell" role="dialog" aria-modal="true" aria-labelledby="migration-title" ref={dialogRef}>
    <header className="migration-header"><div className="migration-brand"><img src="/static/venus-mark.png" alt="" /><strong>Venus</strong></div><div><span>升级数据</span><small>{active ? "升级正在本机执行，请保持 Venus 运行" : "先备份，再切换；原始录像不会被修改"}</small></div><span className={`migration-safety ${active ? "active" : ""}`}>{active ? "执行中" : "尚未开始"}</span></header>
    <div className="migration-layout"><aside className="migration-steps" aria-label="升级步骤">{STEPS.map(([label, note], index) => <div className={index === step ? "active" : index < step ? "done" : ""} key={label}><span>{index < step ? "✓" : index + 1}</span><div><strong>{label}</strong><small>{note}</small></div></div>)}<p><strong>数据保护</strong>只读取旧版设置和记录，不会扫描、移动或删除原始录像。</p></aside>
      <main className="migration-content">{error && <div className="migration-error" role="alert"><span>{error}</span>{errorId && <small>问题编号：{errorId}</small>}</div>}{connection && <div className="migration-connection" role="status">{connection}</div>}
        {loading && !source && screen === "check" ? <Loading /> : screen === "check" ? <CheckStep source={source} busy={busy} inspect={inspect} quit={() => void window.liveClipperShell?.quitApp?.()} quitAvailable={Boolean(window.liveClipperShell?.quitApp)} error={error} titleRef={titleRef} />
          : screen === "differences" && inspectionPlan && choices ? <DifferenceStep plan={inspectionPlan} choices={choices} fields={fields} busy={busy} historyLimit={historyLimit} update={updateChoice} select={selectDirectory} more={() => setHistoryLimit((value) => value + 20)} back={backToCheck} next={validate} titleRef={titleRef} />
          : screen === "confirm" && validatedPlan ? <ConfirmStep plan={validatedPlan} busy={busy} back={() => setLocalScreen("differences")} execute={execute} titleRef={titleRef} />
          : screen === "executing" && summary.session ? <ExecutingStep session={summary.session} titleRef={titleRef} />
          : screen === "complete" && summary.session && summary.report ? <CompleteStep session={summary.session} report={summary.report} busy={busy} enter={acknowledge} backup={showBackup} canShowBackup={Boolean(window.liveClipperShell?.showBackup)} titleRef={titleRef} />
          : screen === "failed" && summary.session ? <FailedStep session={summary.session} busy={busy} retry={retry} quit={() => void window.liveClipperShell?.quitApp?.()} quitAvailable={Boolean(window.liveClipperShell?.quitApp)} titleRef={titleRef} />
          : <Diagnostic titleRef={titleRef} code={summary.session?.failure?.code ?? errorId} />}
      </main></div>
  </section></div>;
}

function Loading() { return <div className="migration-loading" role="status"><span className="migration-spinner" /><strong>正在读取升级状态</strong><p>正在确认旧版数据和已完成的检查…</p></div>; }
function CheckStep({ source, busy, inspect, quit, quitAvailable, error, titleRef }: { source: MigrationSnapshot["source"] | null; busy: string; inspect(): void; quit(): void; quitAvailable: boolean; error: string; titleRef: RefObject<HTMLHeadingElement | null> }) {
  return <div className="migration-step"><div className="migration-scroll"><span className="migration-eyebrow">开始之前</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>检查现有内容，准备升级</h1><p>Venus 检测到旧版设置和工作记录。检查过程只读，不会创建备份、项目或修改现有数据。</p><section className="migration-source-card"><div><span>已检测到</span><strong>{source ? `${source.display_summary.metadata_file_count} 组设置与记录文件` : "旧版数据"}</strong><small>{source?.display_summary.history_count !== undefined ? `${source.display_summary.history_count} 条历史记录等待检查` : "检查后会显示历史处置结论"}</small></div><span className="status-pill ready">只读检查</span></section><details className="migration-details"><summary>哪些内容会被读取</summary><p>只读取 Venus 旧版配置、运行记录和调度状态，用于准备升级内容。不会读取媒体正文，不会把本机信息发送到外部服务。</p></details>{error && <p className="migration-safe-note">检查未完成；尚未创建备份或修改现有数据。</p>}</div><footer className="migration-footer"><button className="button" disabled={!quitAvailable || Boolean(busy)} onClick={quit}>退出 Venus</button><span /><small>检查完成后，你可以先核对全部内容再决定是否升级。</small><button className="button primary" disabled={Boolean(busy)} onClick={inspect}>{busy === "inspect" ? "检查中…" : error ? "重新检查" : "检查升级内容"}</button></footer></div>;
}
function DifferenceStep({ plan, choices, fields, busy, historyLimit, update, select, more, back, next, titleRef }: { plan: MigrationPlan; choices: MigrationChoices; fields: Record<string, string>; busy: string; historyLimit: number; update(value: Partial<MigrationChoices>): void; select(field: "source_directory" | "output_directory"): void; more(): void; back(): void; next(): void; titleRef: RefObject<HTMLHeadingElement | null> }) {
  const [historyOpen, setHistoryOpen] = useState(false); const required = new Set(plan.required_choices); const counts = plan.history.counts; const total = counts.importable + counts.compatibility + counts.quarantined;
  const incomplete = [...required].some((field) => field === "project_name" ? !choices.project_name.trim() : field === "source_directory" ? !choices.source_directory : field === "output_directory" ? !choices.output_directory : false);
  return <div className="migration-step"><div className="migration-scroll"><span className="migration-eyebrow">升级内容</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>{required.size ? "处理必要差异" : "升级内容已准备好"}</h1><p>{required.size ? "只需确认下面标出的差异，其余内容将使用当前默认设置。" : "请核对默认项目、处理能力、历史记录和备份空间。"}</p>
    <div className="migration-grid"><PlanCard title="默认项目" state={plan.readiness.source_status === "ready" && plan.readiness.output_status === "ready" ? "已准备" : "需确认"}><Fact label="项目名称" value={choices.project_name} /><Fact label="录像目录" value={choices.source_directory} /><Fact label="成片位置" value={choices.output_directory} />{required.has("project_name") && <Field label="项目名称" error={fields.project_name} errorId="migration-project-name-error"><input value={choices.project_name} aria-invalid={Boolean(fields.project_name)} aria-describedby={fields.project_name ? "migration-project-name-error" : undefined} onChange={(event) => update({ project_name: event.target.value })} /></Field>}{required.has("source_directory") && <DirectoryField field="source-directory" label="录像目录" value={choices.source_directory} error={fields.source_directory} choose={() => select("source_directory")} />}{required.has("output_directory") && <DirectoryField field="output-directory" label="成片位置" value={choices.output_directory} error={fields.output_directory} choose={() => select("output_directory")} />}</PlanCard>
      <PlanCard title="处理能力" state={plan.readiness.resource_problems.length ? "迁移后处理" : "已准备"}>{Object.entries(plan.resources).map(([id, resource]) => <div className="migration-resource" key={id}><div><strong>{resource.label}</strong><small>{resource.model || "尚未配置模型"} · {resource.credential_present ? "凭据已保存" : "缺少凭据"}</small></div><span className={`status-pill ${resource.status === "ready" ? "ready" : "attention"}`}>{resource.status === "ready" ? "可用" : "待修复"}</span></div>)}{plan.readiness.resource_problems.length > 0 && <p className="migration-card-note">数据可以迁移；相关项目会保持未启用，迁移后在项目中修复。</p>}</PlanCard>
      <PlanCard title="历史记录" state={`${total} 条`}><div className="migration-counts"><Fact label="正常导入" value={String(counts.importable)} /><Fact label="继续处理" value={String(counts.compatibility)} /><Fact label="隔离" value={String(counts.quarantined)} /><Fact label="已有成片" value={String(counts.safe_result)} /></div><details className="migration-details" open={historyOpen}><summary aria-expanded={historyOpen} onClick={(event) => { event.preventDefault(); setHistoryOpen((value) => !value); }}>查看历史明细</summary><div className="migration-history">{plan.history.entries.slice(0, historyLimit).map((item) => <div key={item.display_identity}><span>{item.display_identity}</span><small>{item.reason_label}</small></div>)}{historyLimit < plan.history.entries.length && <button className="text-button" onClick={more}>显示更多</button>}</div></details></PlanCard>
      <PlanCard title="发现新录像" state={required.has("trigger_mode") ? "需确认" : "已继承"}><p className="migration-card-value">{discoveryLabel(choices)}</p>{required.has("trigger_mode") && <fieldset className="migration-choice"><legend>迁移后如何发现新录像</legend><label><input type="radio" checked={choices.trigger_mode === "manual"} onChange={() => update({ trigger_mode: "manual" })} />仅手动检查</label><label><input type="radio" checked={choices.trigger_mode === "scheduled"} onChange={() => update({ trigger_mode: "scheduled" })} />定时自动检查</label>{choices.trigger_mode === "scheduled" && <div className="migration-schedule"><label>定时方式<select value={choices.schedule_mode || "daily"} onChange={(event) => update({ schedule_mode: event.target.value as "daily" | "interval" })}><option value="daily">每天固定时间</option><option value="interval">固定间隔</option></select></label>{choices.schedule_mode === "interval" ? <label>检查间隔<select value={choices.interval_minutes ?? 60} onChange={(event) => update({ interval_minutes: Number(event.target.value) })}><option value={30}>30 分钟</option><option value={60}>1 小时</option><option value={180}>3 小时</option><option value={360}>6 小时</option><option value={720}>12 小时</option></select></label> : <label>每天时间<input type="time" value={choices.daily_time ?? "22:00"} onChange={(event) => update({ daily_time: event.target.value })} /></label>}</div>}</fieldset>}</PlanCard>
    </div><section className={`migration-backup ${plan.backup.space_status}`}><div><strong>升级前将创建本机备份</strong><p>位置：{plan.backup.target_display} · 预计需要 {bytes(plan.backup.required_bytes)} · 当前可用 {bytes(plan.backup.available_bytes)}</p></div><span>{plan.backup.space_status === "ready" ? "空间充足" : "空间不足"}</span></section></div><footer className="migration-footer"><button className="button" disabled={Boolean(busy)} onClick={back}>返回</button><span /><small>下一步会重新校验当前选择，不会立即执行。</small><button className="button primary" disabled={Boolean(busy) || incomplete || plan.backup.space_status !== "ready"} onClick={next}>{busy === "validate" ? "校验中…" : "继续确认"}</button></footer></div>;
}
function ConfirmStep({ plan, busy, back, execute, titleRef }: { plan: MigrationPlan; busy: string; back(): void; execute(): void; titleRef: RefObject<HTMLHeadingElement | null> }) { const counts = plan.history.counts; return <div className="migration-step"><div className="migration-scroll"><span className="migration-eyebrow">最后确认</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>确认升级内容</h1><p>开始后 Venus 会先完成并核验备份，再迁移项目、历史记录和结果。</p><dl className="migration-review"><Fact label="项目" value={plan.project.name} /><Fact label="录像目录" value={plan.project.source_directory} /><Fact label="成片位置" value={plan.project.output_directory} /><Fact label="发现新录像" value={discoveryLabel(plan.choices)} /><Fact label="历史记录" value={`${counts.importable + counts.compatibility + counts.quarantined} 条，${counts.quarantined} 条隔离`} /><Fact label="迁移后状态" value={plan.readiness.resource_problems.length ? `数据可用，${plan.readiness.resource_problems.length} 项需修复` : "项目可直接使用"} /><Fact label="备份" value={`${plan.backup.target_display}（${bytes(plan.backup.required_bytes)}）`} /></dl><div className="migration-confirm-note"><strong>原始录像不会被移动或删除</strong><p>执行失败时不会创建不完整的项目；修复问题后可以继续升级。</p></div></div><footer className="migration-footer"><button className="button" disabled={Boolean(busy)} onClick={back}>返回修改</button><span /><small>开始后，在得到明确结果前不可退出或返回。</small><button className="button primary" disabled={Boolean(busy)} onClick={execute}>{busy === "execute" ? "正在提交…" : "开始升级"}</button></footer></div>; }
function ExecutingStep({ session, titleRef }: { session: MigrationSession; titleRef: RefObject<HTMLHeadingElement | null> }) { const index = Math.max(0, STAGES.findIndex(([id]) => id === session.stage)); return <div className="migration-step"><div className="migration-scroll migration-executing"><span className="migration-eyebrow">正在升级</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>请保持 Venus 运行</h1><p>完成后会自动显示升级结果。</p><div className="migration-stage-list" aria-live="polite">{STAGES.map(([, label], position) => <div className={position < index ? "done" : position === index ? "active" : ""} key={label}><span>{position < index ? "✓" : position === index ? "•" : position + 1}</span><div><strong>{label}</strong>{position === index && <small>正在处理</small>}{session.stage === "history" && position === index && session.total_history_count !== null && <small>{session.processed_history_count ?? 0} / {session.total_history_count} 条</small>}</div></div>)}</div><p className="migration-lock-note">升级完成或恢复原数据前，导航、退出和返回操作已锁定。</p></div></div>; }
function CompleteStep({ session, report, busy, enter, backup, canShowBackup, titleRef }: { session: MigrationSession; report: MigrationReport; busy: string; enter(): void; backup(): void; canShowBackup: boolean; titleRef: RefObject<HTMLHeadingElement | null> }) { const attention = session.state === "completed_attention"; return <div className="migration-step"><div className="migration-scroll migration-complete"><span className="migration-complete-mark">✓</span><span className="migration-eyebrow">升级完成</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>{attention ? "数据已迁移，还有问题需要处理" : "项目已经准备好了"}</h1><p>{attention ? "项目当前不会处理新录像。进入同一项目后，根据问题提示完成修复即可。" : "旧版设置、历史记录和已有成片已迁移到项目中。"}</p><dl className="migration-review"><Fact label="项目" value={report.project.name} /><Fact label="发现新录像" value={discoveryLabel(report.discovery)} /><Fact label="历史记录" value={`${report.history_total} 条（导入 ${report.imported}、继续处理 ${report.compatibility}、隔离 ${report.quarantined}）`} /><Fact label="已有成片" value={`${report.safe_results} 个`} /><Fact label="本机备份" value={report.backup_created ? "已创建并核验" : "未创建"} /><Fact label="项目状态" value={attention ? `${report.blocker_count} 项条件需要修复` : "可以开始使用"} /></dl>{attention && <div className="migration-attention"><strong>数据迁移已经完成</strong><p>历史与备份均已保留；在修复完成前不会自动扫描或创建新的处理任务。</p></div>}</div><footer className="migration-footer"><button className="button" disabled={!canShowBackup || Boolean(busy)} onClick={backup}>{busy === "backup" ? "正在打开…" : "在 Finder 中显示备份"}</button><span /><small>确认后将进入项目。</small><button className="button primary" disabled={Boolean(busy)} onClick={enter}>{busy === "acknowledge" ? "正在确认…" : attention ? "查看并修复" : "进入项目"}</button></footer></div>; }
function FailedStep({ session, busy, retry, quit, quitAvailable, titleRef }: { session: MigrationSession; busy: string; retry(): void; quit(): void; quitAvailable: boolean; titleRef: RefObject<HTMLHeadingElement | null> }) { return <div className="migration-step"><div className="migration-scroll"><span className="migration-eyebrow">升级已停止</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>升级没有提交，可以重新尝试</h1><p>{session.failure?.summary || "旧版数据保持不变，请检查条件后重试。"}</p><div className="migration-failure-facts"><Fact label="旧版数据" value="没有修改" /><Fact label="默认项目" value="未创建" /><Fact label="备份" value={session.backup_status === "completed" ? "已完成并可复用" : "未完成"} /><Fact label="下一步" value="修复问题后可以继续升级" /></div>{session.failure?.code && <small className="migration-diagnostic-id">问题编号：{session.failure.code.replaceAll("_", "-").toUpperCase()}</small>}</div><footer className="migration-footer"><button className="button" disabled={!quitAvailable || Boolean(busy)} onClick={quit}>退出 Venus</button><span /><small>重试会复用已核验的备份，不会自动重复写入。</small><button className="button primary" disabled={Boolean(busy)} onClick={retry}>{busy === "retry" ? "正在重试…" : "重新尝试升级"}</button></footer></div>; }
function Diagnostic({ titleRef, code }: { titleRef: RefObject<HTMLHeadingElement | null>; code: string | null | undefined }) { return <div className="migration-step"><div className="migration-scroll migration-diagnostic"><span className="migration-eyebrow">升级已停止</span><h1 id="migration-title" ref={titleRef} tabIndex={-1}>数据状态需要检查</h1><p>现有数据没有修改。请记录下面的问题编号并联系支持。</p>{code && <small>问题编号：{code.replaceAll("_", "-").toUpperCase()}</small>}<details className="migration-details"><summary>查看诊断说明</summary><p>请记录问题编号并联系支持。诊断信息不会包含完整路径、旧记录正文或凭据。</p></details></div></div>; }
function PlanCard({ title, state, children }: { title: string; state: string; children: ReactNode }) { return <section className="migration-plan-card"><header><h2>{title}</h2><span>{state}</span></header><div>{children}</div></section>; }
function Fact({ label, value }: { label: string; value: string }) { return <div className="migration-fact"><dt>{label}</dt><dd title={value}>{value}</dd></div>; }
function Field({ label, error, errorId, children }: { label: string; error?: string; errorId: string; children: ReactNode }) { return <label className="migration-field">{label}{children}{error && <small id={errorId}>{error}</small>}</label>; }
function DirectoryField({ field, label, value, error, choose }: { field: string; label: string; value: string; error?: string; choose(): void }) { const errorId = `migration-${field}-error`; return <Field label={label} error={error} errorId={errorId}><div><input value={value} readOnly aria-invalid={Boolean(error)} aria-describedby={error ? errorId : undefined} /><button className="button" type="button" onClick={choose}>选择…</button></div></Field>; }
