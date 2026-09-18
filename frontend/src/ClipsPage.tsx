import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { projectApi } from "./project-api";
import type { ResultSummary } from "./project-dto";
import { ErrorState, LoadingState, Metric, PageHeading, time } from "./workbench-shared";

const RESULT_LABEL = { clips_ready: "成片已生成", no_clip: "本次未选出适合的片段", partial: "部分成片可用" } as const;
function resultLabel(value: string) { const label = RESULT_LABEL[value as keyof typeof RESULT_LABEL]; if (!label) console.warn("Venus received an unknown clip result type"); return label ?? "暂时无法识别结果状态"; }

export function ClipsPage() {
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "all" ? "all" : "new";
  const [results, setResults] = useState<ResultSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [unseen, setUnseen] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const loadRevision = useRef(0);
  const load = useCallback(async (append = false, nextCursor?: string | null) => {
    const revision = ++loadRevision.current; setLoading(true);
    try {
      const payload = await projectApi.clips(view, append ? nextCursor : null);
      if (revision !== loadRevision.current) return;
      setResults((current) => append ? [...current, ...payload.results] : payload.results);
      setCursor(payload.cursor); setHasMore(payload.has_more); setUnseen(payload.unseen_result_count); setError("");
    } catch (reason) { if (revision === loadRevision.current) setError((reason as Error).message); } finally { if (revision === loadRevision.current) setLoading(false); }
  }, [view]);
  useEffect(() => { setResults([]); setCursor(null); setHasMore(false); setError(""); void load(); return () => { loadRevision.current += 1; }; }, [load]);
  useEffect(() => { let timer = 0; const schedule = () => { window.clearInterval(timer); if (!document.hidden) timer = window.setInterval(() => void load(), 15000); }; const visible = () => { if (!document.hidden) void load(); schedule(); }; schedule(); document.addEventListener("visibilitychange", visible); return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); }; }, [load]);
  return <section className="page clips-page">
    <PageHeading title="成片" description="处理结果会显示在这里，包括未选出片段的录像。打开结果后会标记为已查看。" actions={<button className="button" onClick={() => void load()}>刷新</button>} />
    <div className="clips-summary">{unseen !== null && <Metric label="未查看结果" value={unseen} />}<div className="filters" role="tablist" aria-label="结果筛选"><button role="tab" aria-selected={view === "new"} className={view === "new" ? "active" : ""} onClick={() => setParams({ view: "new" })}>未查看</button><button role="tab" aria-selected={view === "all"} className={view === "all" ? "active" : ""} onClick={() => setParams({ view: "all" })}>全部</button></div></div>
    {error && !results.length ? <ErrorState message={error} retry={() => void load()} /> : <>{error && <p className="stale-warning" role="alert">刷新失败，当前显示的是上次加载的内容。原因：{error}</p>}<div className="clip-result-grid">{results.map((result) => <ResultCard key={`${result.run_id}:${result.result_revision}`} result={result} />)}</div>{!results.length && !loading && <div className="empty-state"><strong>{view === "new" ? "没有未查看的结果" : "还没有处理结果"}</strong><p>{view === "new" ? "可切换到“全部”查看已有结果。" : "录像处理完成后，可以在这里查看成片或未选出片段的原因。"}</p></div>}{loading && !results.length && <LoadingState />}{hasMore && <button className="button load-more" disabled={loading} onClick={() => void load(true, cursor)}>{loading ? "加载中…" : "加载更多"}</button>}</>}
  </section>;
}

function ResultCard({ result }: { result: ResultSummary }) {
  return <Link className={`clip-result-card ${result.seen ? "" : "unseen"}`} to={`/projects/${result.project.project_id}/runs/${result.run_id}?view=result${result.primary_output_id ? `&output=${encodeURIComponent(result.primary_output_id)}` : ""}`}>
    <div className="clip-card-kicker"><span>{result.seen ? "已查看" : "未查看"}</span><small>{time(result.completed_at)}</small></div>
    <strong>{result.source_name}</strong><p>{result.overall_summary || resultLabel(result.result_type)}</p>
    <div className="clip-card-meta"><span>{result.project.name}</span><span>{resultLabel(result.result_type)}</span>{result.available_output_count > 0 && <><span>{result.available_output_count} 个成片</span><span>{formatTotalDuration(result.total_duration_ms)}</span></>}</div>
    {result.issue_summary && <div className="clip-card-issue" role="status">{result.issue_summary.title} · {result.issue_summary.next_step}</div>}
  </Link>;
}
function formatTotalDuration(value: number) { const seconds = Math.round(value / 1000); return seconds < 60 ? `总时长 ${seconds} 秒` : `总时长 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`; }
