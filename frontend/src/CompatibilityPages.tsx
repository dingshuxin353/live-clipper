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
  return <section className="page"><PageHeading title="旧版待审记录" description="这里保留了旧版待审记录，可打开查看详情。其他剪辑结果请前往成片页。" />{state.error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}<div className="attention-list">{state.data.runs.map((item) => <Link className="attention-item warning" to={item.detail_url} key={item.run.run_id}><span><RemixIcon name="clock" /></span><div><strong>{item.run.source_name}</strong><p>{item.project.name}</p></div><b><RemixIcon name="chevronRight" /></b></Link>)}{!state.data.count && <div className="empty-state"><strong>暂无旧版待审记录</strong><p>可前往「成片」查看其他剪辑结果。</p><Link className="button" to="/clips">查看成片</Link></div>}</div></section>;
}
