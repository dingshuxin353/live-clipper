import { useEffect } from "react";
import { Link } from "react-router-dom";

import { projectApi } from "./project-api";
import type { ProjectSummary, Run, StudioPayload } from "./project-dto";
import { ErrorState, LoadingState, Metric, PageHeading, ProjectRow, RunCard, SectionHeading, basename, sortProjects, type PollingState, usePolling } from "./workbench-shared";

export function StudioPage({ notify, state }: { notify(message: string): void; state: PollingState<StudioPayload> }) {
  const studio = state.data;
  const active = Boolean(studio && (studio.workload.processing || studio.workload.queued));
  useEffect(() => { if (!active) return; const id = window.setInterval(() => { if (!document.hidden) void state.refresh(); }, 5000); return () => window.clearInterval(id); }, [active, state.refresh]);
  if (state.loading && !studio) return <LoadingState />;
  if (!studio) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const projectById = new Map(studio.projects.map((item) => [item.project_id, item]));
  const blocked = studio.needs_attention.blocked_project_ids.map((id) => projectById.get(id)).filter(Boolean) as ProjectSummary[];
  const markSeen = async () => { try { await projectApi.markStudioSeen(studio.through_event_id); await state.refresh(); notify("已将当前变化标记为已查看"); } catch (error) { notify((error as Error).message); } };
  return <section className="page studio-page">
    <PageHeading eyebrow="本机持续生产概览" title="工作室" description="先处理需要介入的事项，再查看正在制作、后台变化与最近结果。" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />
    {state.error && <p className="stale-warning" role="alert">刷新失败：{state.error}。正在保留上次成功数据。</p>}
    <section className="attention-panel"><SectionHeading title="需要你处理" subtitle={`${studio.pending_review_count} 条待审 · ${studio.needs_attention.failed_runs.length} 条失败 · ${blocked.length} 个项目受阻`} />
      {!studio.pending_review_count && !studio.needs_attention.failed_runs.length && !blocked.length ? <p className="quiet-state">当前没有需要介入的事项。</p> : <div className="attention-list">
        {studio.pending_review_count > 0 && <Link className="attention-item warning" to="/review"><span>审</span><div><strong>{studio.pending_review_count} 条剪辑记录等待审阅</strong><p>进入待审入口继续处理。</p></div><b>›</b></Link>}
        {studio.needs_attention.failed_runs.map((run) => <RunLink key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}
        {blocked.map((project) => <Link className="attention-item error" key={project.project_id} to={`/projects/${project.project_id}`}><span>!</span><div><strong>{project.name} 需要完成配置</strong><p>{project.blocking_issues[0]?.message ?? "项目当前不可运行"}</p></div><b>›</b></Link>)}
      </div>}
    </section>
    <section className="section-block"><SectionHeading title="正在发生" subtitle={`${studio.workload.processing} 条处理中 · ${studio.workload.queued} 条排队`} /><div className="run-grid">{studio.in_progress.processing.map((run) => <RunCard key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}{studio.in_progress.queued.map((run) => <RunCard key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}{!studio.in_progress.processing.length && !studio.in_progress.queued.length && <p className="quiet-state">当前没有正在处理或排队的剪辑记录。</p>}</div></section>
    <section className="section-block"><SectionHeading title="自上次查看" subtitle={`${studio.changes.length} 项后台变化`} action={studio.changes.length ? <button className="text-button" onClick={() => void markSeen()}>标记为已查看</button> : undefined} /><div className="metric-grid"><Metric label="新建记录" value={studio.unattended_changes.created.length} /><Metric label="已完成" value={studio.unattended_changes.completed.length} /><Metric label="转为待审" value={studio.unattended_changes.awaiting_review.length} /><Metric label="失败" value={studio.unattended_changes.failed.length} tone="error" /></div></section>
    <section className="section-block"><SectionHeading title="项目运行状态" subtitle={`${studio.projects.length} 条生产线`} action={<Link className="text-button" to="/projects">查看全部项目</Link>} /><div className="project-health-list">{studio.project_health.map((project) => <ProjectRow key={project.project_id} project={project} />)}</div></section>
    <section className="section-block"><SectionHeading title="最近结果" subtitle="最近完成的剪辑记录" /><div className="result-list">{studio.recent_results.map((run) => <RunCard key={run.run_id} run={run} project={projectById.get(run.project_id)} />)}{!studio.recent_results.length && <p className="quiet-state">还没有完成的剪辑记录。</p>}</div></section>
  </section>;
}

function RunLink({ run, project }: { run: Run; project?: ProjectSummary }) { return <Link className="attention-item error" to={`/projects/${run.project_id}/runs/${run.run_id}`}><span>!</span><div><strong>{basename(run.latest_seen_path)} 处理失败</strong><p>{project?.name ?? run.project_id} · {run.error_summary ?? "查看失败详情"}</p></div><b>›</b></Link>; }

export function ProjectsPage() {
  const state = usePolling((signal) => projectApi.projects(signal), 15000);
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const projects = sortProjects(state.data.projects);
  return <section className="page"><PageHeading eyebrow="全部生产线" title="项目" description="按行动优先级查看每个项目的运行状态、工作量与下一次扫描。" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />{state.error && <p className="stale-warning" role="alert">刷新失败：{state.error}。正在保留上次成功数据。</p>}<div className="project-list">{projects.map((project) => <ProjectRow key={project.project_id} project={project} detailed />)}{!projects.length && <div className="empty-state"><strong>还没有项目</strong><p>点击右上角“新建项目”建立第一条内容生产线。</p></div>}</div></section>;
}
