import { RemixIcon } from "./ui/RemixIcon";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ChooseRecordingsDialog, LatestScanDialog, PauseProjectDialog, ProjectSettingsDialog } from "./ProjectDialogs";
import { ApiError } from "./api";
import { projectApi, requestId } from "./project-api";
import type { IssueGroupSummary, Run, RunFilter } from "./project-dto";
import { IssueDrawer, LegacyRunView, RunResultPage } from "./RunResultPage";
import { ErrorState, LoadingState, Metric, PageHeading, RUN_LABELS, RunCard, STATUS_LABELS, SectionHeading, StatusPill, basename, scanMessage, time, usePolling } from "./workbench-shared";

type ProjectOperation = { id: string; action: 'scan' | 'enable' | 'pause' | 'resume' } | { id: string; action: 'recheck'; group: string; revisions: Record<string, number> };
function readProjectOperation(key: string): ProjectOperation | null { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; } }

export function ProjectPage({ notify }: { notify(message: string): void }) {
  const { projectId = "" } = useParams(); const navigate = useNavigate(); const location = useLocation(); const [params] = useSearchParams();
  const operationKey = `venus.project-operation.${projectId}`;
  const pending = useRef(readProjectOperation(operationKey)); const [operationBusy, setOperationBusy] = useState(false); const [operationUnknown, setOperationUnknown] = useState(Boolean(pending.current));
  useEffect(() => { pending.current = readProjectOperation(operationKey); setOperationUnknown(Boolean(pending.current)); }, [operationKey]);
  const projectState = usePolling((signal) => projectApi.project(projectId, signal), 15000, projectId);
  const [filter, setFilter] = useState<RunFilter>("all"); const [runs, setRuns] = useState<Run[]>([]); const [cursor, setCursor] = useState<string | null>(null); const [hasMore, setHasMore] = useState(false); const [runsError, setRunsError] = useState(""); const [loadingRuns, setLoadingRuns] = useState(true); const [pauseOpen, setPauseOpen] = useState(false);
  const loadRevision = useRef(0);
  const loadRuns = useCallback(async (append = false, nextCursor = cursor) => { const revision = ++loadRevision.current; setLoadingRuns(true); try { const payload = await projectApi.runs(projectId, filter, append ? nextCursor : null); if (revision !== loadRevision.current) return; setRuns((current) => append ? [...current, ...payload.runs] : payload.runs); setCursor(payload.cursor); setHasMore(payload.has_more); setRunsError(""); } catch (error) { if (revision === loadRevision.current) setRunsError((error as Error).message); } finally { if (revision === loadRevision.current) setLoadingRuns(false); } }, [cursor, filter, projectId]);
  useEffect(() => { setRuns([]); setRunsError(""); setHasMore(false); setCursor(null); void loadRuns(false, null); return () => { loadRevision.current += 1; }; }, [filter, projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const project = projectState.data?.project; const active = Boolean(project && (project.workload.processing || project.workload.queued || project.latest_scan?.status === "running"));
  useEffect(() => { if (!active) return; const id = window.setInterval(() => { if (!document.hidden) { void projectState.refresh(); void loadRuns(false, null); } }, 5000); return () => window.clearInterval(id); }, [active, loadRuns, projectState.refresh]);
  if (projectState.loading && !project) return <LoadingState />;
  if (!project) return projectState.errorCode === "project_not_found" ? <ObjectNotFound type="项目" /> : <ErrorState message={projectState.error} retry={() => void projectState.refresh()} />;
  const openDialog = (dialog: string) => navigate({ pathname: location.pathname, search: new URLSearchParams({ dialog, projectId }).toString() });
  const perform = async (action: ProjectOperation['action'], group?: IssueGroupSummary) => {
    if (operationBusy) return false;
    setOperationBusy(true);
    try {
      if (!pending.current) {
        if (action === 'recheck') {
          if (!group) return false;
          const details = await Promise.all(group.issue_ids.map(id => projectApi.issue(id)));
          pending.current = { id: requestId('issue-group'), action, group: group.group_key, revisions: Object.fromEntries(details.map(({ issue }) => [issue.issue_id, issue.issue_revision])) };
        } else pending.current = { id: requestId(`project-${action}`), action };
        sessionStorage.setItem(operationKey, JSON.stringify(pending.current));
      }
      const original = pending.current;
      if (original.action === 'scan') { const result = await projectApi.scan(projectId, original.id, 'new'); notify(scanMessage(result.scan)); }
      else if (original.action === 'recheck') { await projectApi.groupRecheck(original.group, original.id, original.revisions); notify('已重新检查这组问题'); }
      else { await projectApi.activate(projectId, original.action, original.id); notify(original.action === 'pause' ? '自动扫描已暂停。正在处理和排队的录像会继续处理，仍可手动扫描。' : original.action === 'resume' ? '项目已恢复' : '项目已启用'); }
      pending.current = null; sessionStorage.removeItem(operationKey); setOperationUnknown(false);
      await Promise.all([projectState.refresh(), loadRuns(false, null)]);
      if (original.action === 'scan') openDialog('latest-scan');
      return true;
    } catch (error) {
      if (pending.current && (!(error instanceof ApiError) || error.outcomeUnknown || error.code === 'request_id_conflict')) { setOperationUnknown(true); notify('暂时无法确认操作结果，请核对原操作。'); }
      else { pending.current = null; sessionStorage.removeItem(operationKey); setOperationUnknown(false); notify((error as Error).message); }
      return false;
    } finally { setOperationBusy(false); }
  };
  const activationAction = project.activation_state === "paused" ? "resume" : "enable";
  return <section className="page project-detail"><div className="breadcrumbs"><Link to="/projects">项目</Link><span><RemixIcon name="chevronRight" /></span><span>{project.name}</span></div><PageHeading title={project.name} description={project.description || undefined} actions={<><button className="button" onClick={() => openDialog("project-settings")}>项目设置</button><button className="button" disabled={operationBusy || operationUnknown} onClick={() => project.activation_state === "active" ? setPauseOpen(true) : void perform(activationAction)}>{project.activation_state === "active" ? "暂停项目" : project.activation_state === "paused" ? "恢复项目" : "启用项目"}</button><button className="button primary" disabled={operationBusy || operationUnknown || project.latest_scan?.status === "running" || project.readiness_state === "blocked"} onClick={() => void perform("scan")}>{operationBusy || project.latest_scan?.status === "running" ? "扫描中…" : "手动扫描"}</button></>} />
    {operationUnknown && <p role="alert" className="stale-warning">上次操作结果尚未确认。<button className="button" disabled={operationBusy} onClick={() => pending.current && void perform(pending.current.action)}>核对原操作</button></p>}
    {projectState.error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{projectState.error}</p>}{project.blocking_issues.map((issue, index) => <div className="project-alert error" role="alert" key={("issue_id" in issue ? issue.issue_id : issue.code) ?? index}><strong>项目暂时无法运行</strong><p>{"summary" in issue ? issue.summary : issue.message}</p></div>)}
    <div className="project-overview"><div className="hero-status"><StatusPill status={project.main_status} /><h2>{STATUS_LABELS[project.main_status] ?? "暂时无法确认项目状态"}</h2><p>{project.current_run ? `当前录像：${project.current_run.source_name}` : project.activation_state === "paused" ? "自动扫描已暂停，正在处理和排队的录像会继续处理。" : "暂无正在处理的录像"}</p></div><div className="metric-grid"><Metric label="处理中" value={project.workload.processing} /><Metric label="排队中" value={project.workload.queued} /><Metric label="处理失败" value={project.workload.failed} tone="error" /><Metric label="处理完成" value={project.workload.completed} /><Metric label="未查看结果" value={project.workload.new_results} /></div></div>
    <div className="project-meta-grid"><article><span>扫描方式</span><strong>{project.schedule?.enabled ? "定时扫描（也可手动）" : "手动扫描"}</strong><small>{project.schedule?.enabled ? project.schedule.next_scan_at ? `下次扫描：${time(project.schedule.next_scan_at)}` : "下次扫描时间待确定" : "点击“手动扫描”查找新录像"}</small></article><article><span>最近扫描</span><strong>{project.latest_scan ? scanMessage(project.latest_scan) : "尚未扫描"}</strong>{project.latest_scan && <button className="text-button" onClick={() => openDialog("latest-scan")}>查看扫描结果</button>}</article><article><span>录像文件夹</span><strong title={project.config?.config.source.directory}>{project.config ? basename(project.config.config.source.directory) : "—"}</strong>{project.config?.config.source.first_scan_mode === "choose_existing" && <button className="text-button" onClick={() => openDialog("choose-recordings")}>选择已有录像</button>}</article></div>
    {project.issue_groups.length > 0 && <section className="issue-strip"><SectionHeading title="待处理问题" subtitle="同一原因的问题已合并显示" /><div>{project.issue_groups.map((group) => <article className="issue-group-row" key={group.group_key}><div><strong>{group.title}</strong><span>{group.count} 项受影响</span></div>{group.available_actions.includes("recheck") && <button className="button" disabled={operationBusy || operationUnknown} onClick={() => void perform("recheck", group)}>重新检查这组问题</button>}</article>)}</div></section>}
    <section className="runs-section"><SectionHeading title="剪辑记录" /><div className="filters" role="tablist" aria-label="剪辑记录筛选">{([['all','全部'],['active','进行中'],['attention','有问题'],['completed','处理完成']] as Array<[RunFilter,string]>).map(([value,label]) => <button role="tab" aria-selected={filter === value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)} key={value}>{label}</button>)}</div>{runsError && <p className="stale-warning" role="alert">{runs.length ? "刷新失败，当前显示的是上次加载的内容。原因：" : "剪辑记录加载失败："}{runsError}</p>}<div className="runs-list">{runs.map((run) => <RunCard key={run.run_id} run={run} project={project} />)}{!runs.length && !loadingRuns && !runsError && <p className="quiet-state">没有符合筛选条件的剪辑记录。</p>}</div>{hasMore && <button className="button load-more" disabled={loadingRuns} onClick={() => void loadRuns(true, cursor)}>{loadingRuns ? "加载中…" : "加载更多"}</button>}</section>
    {params.get("dialog") === "project-settings" && <ProjectSettingsDialog project={project} onSaved={async () => { await projectState.refresh(); notify("项目设置已保存"); }} />}{params.get("dialog") === "latest-scan" && <LatestScanDialog project={project} />}{params.get("dialog") === "choose-recordings" && <ChooseRecordingsDialog project={project} onScanned={async (scan) => { notify(scanMessage(scan)); await Promise.all([projectState.refresh(), loadRuns(false, null)]); }} />}{pauseOpen && <PauseProjectDialog projectName={project.name} onClose={() => setPauseOpen(false)} onConfirm={() => perform("pause")} />}
  </section>;
}

export function RunPage() {
  const { projectId = "", runId = "" } = useParams(); const [params, setParams] = useSearchParams(); const state = usePolling(async (signal) => { const [runPayload, projectPayload] = await Promise.all([projectApi.run(runId, signal), projectApi.project(projectId, signal)]); const result = runPayload.run.has_result ? await projectApi.runResult(runId, signal) : null; return { ...runPayload, project: projectPayload.project, result }; }, 15000, `${projectId}:${runId}`); const active = state.data?.run.status === "processing" || state.data?.run.status === "queued" || state.data?.run.active_issue_summary?.status === "recovering";
  useEffect(() => { if (!active) return; const id = window.setInterval(() => { if (!document.hidden) void state.refresh(); }, 5000); return () => window.clearInterval(id); }, [active, state.refresh]);
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return state.errorCode === "project_not_found" ? <ObjectNotFound type="项目" /> : state.errorCode === "run_not_found" ? <ObjectNotFound type="剪辑记录" /> : <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const { run, stage_events: events, project, result } = state.data;
  const issue = run.active_issue_summary;
  return <section className="page run-detail"><div className="breadcrumbs"><Link to="/projects">项目</Link><span><RemixIcon name="chevronRight" /></span><Link to={`/projects/${projectId}`}>{project.name}</Link><span><RemixIcon name="chevronRight" /></span><span>{run.source_name}</span></div><PageHeading eyebrow="剪辑记录" title={run.source_name} description={`${project.name} · 创建于 ${time(run.queued_at)}`} actions={<StatusPill status={run.status} label={run.status === "queued" && run.queue_position ? `排队第 ${run.queue_position} 位` : RUN_LABELS[run.status]} />} />{state.error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}{result ? <RunResultPage run={run} project={project} payload={result} refresh={state.refresh} /> : <><LegacyRunView run={run} project={project} events={events} />{issue && <button className="issue-preview" onClick={() => { const next = new URLSearchParams(params); next.set("issue", issue.issue_id); setParams(next); }}><strong>{issue.title}</strong><span>{issue.summary}</span><small>{issue.next_step}</small></button>}</>}{params.get("issue") && !result && <IssueDrawer key={params.get("issue")} issueId={params.get("issue")!} project={project} onClose={() => { const next = new URLSearchParams(params); next.delete("issue"); setParams(next, { replace: true }); }} onChanged={state.refresh} />}</section>;
}

export function ObjectNotFound({ type }: { type?: "项目" | "剪辑记录" }) { return <section className="page"><div className="empty-state"><strong>{type === "项目" ? "找不到这个项目" : type === "剪辑记录" ? "找不到这条剪辑记录" : "找不到这项内容"}</strong><p>内容可能已被移除，或链接有误。</p><Link className="button primary" to="/projects">返回项目列表</Link></div></section>; }
