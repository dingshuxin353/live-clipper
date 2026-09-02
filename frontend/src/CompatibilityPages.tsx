import { Link } from "react-router-dom";

import { api } from "./api";
import { projectApi } from "./project-api";
import { Settings } from "./Settings";
import type { GenericRecord, Model } from "./types";
import { ErrorState, LoadingState, PageHeading, StatusPill, usePolling } from "./workbench-shared";

interface SettingsSnapshot {
  configPayload: GenericRecord;
  service: GenericRecord;
  scheduler: GenericRecord;
  reviewAutomation: GenericRecord;
  models: Model[];
}

async function loadSettings(signal: AbortSignal): Promise<SettingsSnapshot> {
  const [configPayload, service, scheduler, reviewAutomation, modelPayload] = await Promise.all([
    api<GenericRecord>("/api/config", {}, signal),
    api<GenericRecord>("/api/service", {}, signal),
    api<GenericRecord>("/api/scheduler", {}, signal),
    api<GenericRecord>("/api/review-automation", {}, signal),
    api<{ models?: Model[] }>("/api/asr/models", {}, signal),
  ]);
  return { configPayload, service, scheduler, reviewAutomation, models: modelPayload.models ?? [] };
}

export function SettingsPage({ notify }: { notify(message: string): void }) {
  const state = usePolling(loadSettings, 15000, "global-settings");
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const reloadConfig = async () => {
    const configPayload = await api<GenericRecord>("/api/config");
    state.setData((current) => current ? { ...current, configPayload } : current);
  };
  const refreshModels = async () => {
    const payload = await api<{ models?: Model[] }>("/api/asr/models");
    state.setData((current) => current ? { ...current, models: payload.models ?? [] } : current);
  };
  return <section className="page legacy-settings-page">
    {state.error && <p className="stale-warning" role="alert">刷新失败：{state.error}。正在保留上次成功数据。</p>}
    <Settings {...state.data} reloadConfig={reloadConfig} refreshModels={refreshModels} refreshAll={state.refresh} notify={notify} schedulerDraft={null} />
  </section>;
}

export function ResourcesPage() {
  const state = usePolling((signal) => projectApi.formOptions(signal), 15000, "project-resources");
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  return <section className="page resources-page">
    <PageHeading eyebrow="处理资源" title="资源" description="查看项目使用的语音识别和 AI 服务。修改配置请前往设置。" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />
    {state.error && <p className="stale-warning" role="alert">刷新失败：{state.error}。正在保留上次成功数据。</p>}
    <div className="resource-list">{state.data.resources.map((resource) => <article className="resource-row" key={resource.resource_id}><div><span className="overline">{resource.resource_type === "asr" ? "语音识别" : resource.resource_type === "analysis" ? "内容分析与 AI 审阅" : "项目资源"}</span><strong>{resource.display_name}</strong><small>{resource.problem || resource.version || resource.resource_id}</small></div><StatusPill status={resource.ready ? "idle" : "blocked"} label={resource.ready ? "已就绪" : "不可用"} /></article>)}</div>
    {!state.data.resources.length && <div className="empty-state"><strong>还没有处理资源</strong><p>请先在设置中完成资源配置。</p></div>}
    <div className="compatibility-action"><div><strong>修改处理资源</strong><p>请前往设置修改服务或管理模型。</p></div><Link className="button" to="/settings">前往设置</Link></div>
  </section>;
}

export function ReviewCompatibilityPage() {
  const state = usePolling((signal) => projectApi.legacyAwaitingReview(signal), 15000, "legacy-review");
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  return <section className="page"><PageHeading eyebrow="旧版待审记录" title="待审" description="查看升级前已进入人工待审的记录。其他剪辑结果请前往成片页。" /><div className="attention-list">{state.data.runs.map((item) => <Link className="attention-item warning" to={item.detail_url} key={item.run.run_id}><span>旧</span><div><strong>{item.run.source_name}</strong><p>{item.project.name} · 旧版待审记录</p></div><b>›</b></Link>)}{!state.data.count && <div className="empty-state"><strong>没有旧版待审记录</strong><p>其他剪辑结果请从“成片”查看。</p><Link className="button" to="/clips">前往成片</Link></div>}</div></section>;
}
