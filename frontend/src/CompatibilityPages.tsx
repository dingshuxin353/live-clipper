import { RemixIcon } from "./ui/RemixIcon";
import { Link } from "react-router-dom";

import { api } from "./api";
import { projectApi } from "./project-api";
import { Settings, type ApplicationSettings } from "./Settings";
import { ErrorState, LoadingState, PageHeading, usePolling } from "./workbench-shared";

export function SettingsPage({ notify }: { notify(message: string): void }) {
  const state = usePolling(signal => api<ApplicationSettings>('/api/config', {}, signal), 15000, 'application-settings');
  return <Settings initial={state.data ?? undefined} loading={state.loading} error={state.error} retry={() => void state.refresh()} notify={notify} />;
}

export function ReviewCompatibilityPage() {
  const state = usePolling((signal) => projectApi.legacyAwaitingReview(signal), 15000, "legacy-review");
  if (state.loading && !state.data) return <LoadingState />;
  if (!state.data) return <ErrorState message={state.error} retry={() => void state.refresh()} />;
  return <section className="page"><PageHeading eyebrow="旧版待审记录" title="待审" description="查看升级前已进入人工待审的记录。其他剪辑结果请前往成片页。" /><div className="attention-list">{state.data.runs.map((item) => <Link className="attention-item warning" to={item.detail_url} key={item.run.run_id}><span>旧</span><div><strong>{item.run.source_name}</strong><p>{item.project.name} · 旧版待审记录</p></div><b><RemixIcon name="chevronRight" /></b></Link>)}{!state.data.count && <div className="empty-state"><strong>没有旧版待审记录</strong><p>其他剪辑结果请从“成片”查看。</p><Link className="button" to="/clips">前往成片</Link></div>}</div></section>;
}
