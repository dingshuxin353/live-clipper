import { RemixIcon } from "./ui/RemixIcon";
import { useEffect } from "react";
import { Link } from "react-router-dom";

import { projectApi } from "./project-api";
import type { OnboardingSnapshot, ProjectSummary, ResultSummary, Run, StudioPayload } from "./project-dto";
import { ErrorState, LoadingState, Metric, PageHeading, ProjectRow, RunCard, SectionHeading, basename, sortProjects, type PollingState, usePolling } from "./workbench-shared";

export function StudioPage({ notify, state, onboarding = null, resumeOnboarding = async () => undefined, resumeTriggerRef }: { notify(message: string): void; state: PollingState<StudioPayload>; onboarding?: OnboardingSnapshot | null; resumeOnboarding?(): Promise<void>; resumeTriggerRef?: React.RefObject<HTMLButtonElement | null> }) {
  const studio = state.data;
  const active = Boolean(studio && (studio.workload.processing || studio.workload.queued));
  useEffect(() => { if (!active) return; const id = window.setInterval(() => { if (!document.hidden) void state.refresh(); }, 5000); return () => window.clearInterval(id); }, [active, state.refresh]);
  if (onboarding?.session?.state === "paused") {
    const steps = { welcome: "开始", asr: "语音识别", ai: "AI 服务", project: "第一个项目", complete: "确认设置" } as const;
    const current = steps[onboarding.session.current_step] ?? "首次设置";
    const ready = [onboarding.resources.asr.ready ? "语音识别模型可用" : onboarding.session?.draft.asr?.resource_id ? "已选语音识别模型暂不可用" : null, onboarding.resources.ai.ready ? "AI 模型可用" : onboarding.session?.draft.ai?.resource_id ? "已选 AI 模型暂不可用" : null].filter(Boolean);
    return <section className="page onboarding-paused-page"><PageHeading title="工作室" /><article className="onboarding-paused-card"><img src="/static/venus-mark.png" alt="" /><span>首次设置尚未完成</span><h1>继续设置</h1><p>已保存的设置和模型下载进度都已保留。</p><div className="onboarding-paused-progress"><strong>从“{current}”继续</strong><small>{ready.length ? ready.join(" · ") : "还未选择模型"}</small></div><button ref={resumeTriggerRef} className="button primary" onClick={() => void resumeOnboarding()}>继续设置</button></article></section>;
  }
  if (state.loading && !studio) return <LoadingState />;
  if (!studio) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const projectById = new Map(studio.projects.map((item) => [item.project_id, item]));
  const blocked = studio.needs_attention.blocked_project_ids.map((id) => projectById.get(id)).filter(Boolean) as ProjectSummary[];
  const markSeen = async () => { try { await projectApi.markStudioSeen(studio.through_event_id); await state.refresh(); notify("当前动态已标为已读"); } catch (error) { notify((error as Error).message); } };
  return <section className="page studio-page">
    <PageHeading title="工作室" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />
    {state.error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}
    <section className="attention-panel"><SectionHeading title="待处理问题" subtitle={`${studio.needs_attention.issue_groups.length} 组问题 · ${studio.needs_attention.failed_runs.length} 条记录失败 · ${blocked.length} 个项目无法运行`} />
      {!studio.needs_attention.issue_groups.length && !studio.needs_attention.failed_runs.length && !blocked.length ? <p className="quiet-state">暂无待处理问题</p> : <div className="attention-list">
        {studio.needs_attention.issue_groups.map((group) => <Link className="attention-item warning" to={`/projects/${studio.needs_attention.failed_runs.find((run) => run.active_issue_summary?.group_key === group.group_key)?.project_id ?? blocked[0]?.project_id ?? ""}`} key={group.group_key}><span><RemixIcon name="warning" /></span><div><strong>{group.title}（{group.count}）</strong><p>进入项目查看详情</p></div><b><RemixIcon name="chevronRight" /></b></Link>)}
        {studio.needs_attention.failed_runs.map((run) => <RunLink key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}
        {blocked.map((project) => { const issue = project.blocking_issues[0]; return <Link className="attention-item error" key={project.project_id} to={`/projects/${project.project_id}`}><span><RemixIcon name="warning" /></span><div><strong>{project.name} 暂时无法运行</strong><p>{issue ? ("summary" in issue ? issue.summary : issue.message) : "进入项目查看原因"}</p></div><b><RemixIcon name="chevronRight" /></b></Link>; })}
      </div>}
    </section>
    <section className="section-block"><SectionHeading title="正在处理" subtitle={`${studio.workload.processing} 条处理中 · ${studio.workload.queued} 条排队`} /><div className="run-grid">{studio.in_progress.processing.map((run) => <RunCard key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}{studio.in_progress.queued.map((run) => <RunCard key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}{!studio.in_progress.processing.length && !studio.in_progress.queued.length && <p className="quiet-state">暂无正在处理或排队的录像</p>}</div></section>
    <section className="section-block"><SectionHeading title="最近动态" subtitle={`${studio.changes.length} 条动态`} action={studio.changes.length ? <button className="text-button" onClick={() => void markSeen()}>标记动态为已读</button> : undefined} /><div className="metric-grid"><Metric label="新增记录" value={studio.unattended_changes.created.length} /><Metric label="处理完成" value={studio.unattended_changes.completed.length} /><Metric label="未查看结果" value={studio.unseen_result_count} /><Metric label="处理失败" value={studio.unattended_changes.failed.length} tone="error" /></div></section>
    <section className="section-block"><SectionHeading title="项目状态" subtitle={`${studio.projects.length} 个项目`} action={<Link className="text-button" to="/projects">查看全部项目</Link>} /><div className="project-health-list">{studio.project_health.map((project) => <ProjectRow key={project.project_id} project={project} />)}</div></section>
    <section className="section-block"><SectionHeading title="最近处理结果" action={<Link className="text-button" to="/clips">查看全部成片</Link>} /><div className="result-list">{studio.recent_results.map((result) => <ResultLink key={result.run_id} result={result} />)}{!studio.recent_results.length && <p className="quiet-state">暂无处理结果</p>}</div></section>
  </section>;
}

function RunLink({ run, project }: { run: Run; project?: ProjectSummary }) { return <Link className="attention-item error" to={`/projects/${run.project_id}/runs/${run.run_id}`}><span><RemixIcon name="warning" /></span><div><strong>{run.source_name} 处理失败</strong><p>{project?.name ?? run.project_id} · {run.error_summary ?? "查看失败详情"}</p></div><b><RemixIcon name="chevronRight" /></b></Link>; }
function ResultLink({ result }: { result: ResultSummary }) { return <Link className="run-card" to={`/projects/${result.project.project_id}/runs/${result.run_id}?view=result`}><div><span className="overline">{result.project.name}</span><strong>{result.source_name}</strong><small>{result.overall_summary || "查看处理结果"}</small></div>{!result.seen && <span className="nav-badge">未查看</span>}</Link>; }

export function ProjectsPage() {
  const state = usePolling((signal) => projectApi.projects(signal), 15000);
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const projects = sortProjects(state.data.projects);
  return <section className="page"><PageHeading title="项目" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />{state.error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}<div className="project-list">{projects.map((project) => <ProjectRow key={project.project_id} project={project} detailed />)}{!projects.length && <div className="empty-state"><strong>还没有项目</strong><p>点击“新建项目”，创建第一个项目。</p></div>}</div></section>;
}
