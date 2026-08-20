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
    <PageHeading eyebrow="项目处理依赖" title="资源" description="只读查看项目可引用的 ASR 与分析资源；资源配置仍在全局设置中维护。" actions={<button className="button" onClick={() => void state.refresh()}>刷新</button>} />
    {state.error && <p className="stale-warning" role="alert">刷新失败：{state.error}。正在保留上次成功数据。</p>}
    <div className="resource-list">{state.data.resources.map((resource) => <article className="resource-row" key={resource.resource_id}><div><span className="overline">{resource.resource_type === "asr" ? "语音识别" : resource.resource_type === "analysis" ? "内容分析" : resource.resource_type}</span><strong>{resource.display_name}</strong><small>{resource.detail || resource.resource_id}</small></div><StatusPill status={resource.ready ? "idle" : "blocked"} label={resource.ready ? "已就绪" : resource.status || "不可用"} /></article>)}</div>
    {!state.data.resources.length && <div className="empty-state"><strong>没有可用资源档案</strong><p>请先在全局设置中完成资源配置。</p></div>}
    <div className="compatibility-action"><div><strong>需要修复资源？</strong><p>资源页不提供新增、编辑或删除；请沿用全局设置中的配置与模型管理能力。</p></div><Link className="button" to="/settings">前往设置</Link></div>
  </section>;
}

export function ReviewCompatibilityPage({ pendingCount }: { pendingCount: number }) {
  return <section className="page"><PageHeading eyebrow="兼容入口" title="待审" description={`当前有 ${pendingCount} 条剪辑记录等待审阅。候选审阅器将在后续 Spec 接入。`} /><div className="empty-state"><strong>{pendingCount ? `${pendingCount} 条记录等待审阅` : "当前没有待审记录"}</strong><p>本轮保留真实数量和入口，不伪造尚未接入的审阅操作。</p><Link className="button" to="/studio">返回工作室</Link></div></section>;
}
