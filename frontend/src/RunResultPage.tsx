import { RemixIcon } from "./ui/RemixIcon";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Field } from "@astryxdesign/core/Field";
import { TextInput } from "@astryxdesign/core/TextInput";

import { api, post, ApiError } from "./api";
import type { Resource } from "./ResourcesPage";
import { copyText } from "./copy";
import { projectApi, requestId } from "./project-api";
import type { IssueAction, IssueDetail, IssueSummary, OutputMaterial, ProjectSummary, Run, RunOutput, RunResultPayload } from "./project-dto";
import { ReprocessControls } from "./ReprocessDialogs";
import { ErrorState, LoadingState, Metric, RUN_LABELS, STAGES, SectionHeading, StatusPill, formatBytes, time } from "./workbench-shared";

const seenRequests = new Map<string, string>();
const seenAttempts = new Set<string>();
type MaterialDraft = { draft: OutputMaterial; request?: { id: string; snapshot: OutputMaterial }; blocked?: 'conflict' | 'unknown' | 'review' };
const materialDrafts = new Map<string, MaterialDraft>();
const RESULT_LABEL = { clips_ready: "成片已生成", no_clip: "本次未选出适合的片段", partial: "部分成片可用", unavailable: "结果暂时不可用" } as const;
function resultLabel(value: string) { const label = RESULT_LABEL[value as keyof typeof RESULT_LABEL]; if (!label) console.warn("Venus received an unknown result type"); return label ?? "暂时无法识别结果状态"; }

export function RunResultPage({ run, project, payload, refresh }: { run: Run; project: ProjectSummary; payload: RunResultPayload; refresh(): Promise<void> }) {
  const [params, setParams] = useSearchParams();
  const rawView = params.get("view");
  const view = rawView === "materials" ? "materials" : "result";
  const defaultOutput = payload.outputs.find((item) => item.available) ?? payload.outputs[0];
  const [outputId, setOutputId] = useState(params.get("output") ?? ((payload.result.result_type === "clips_ready" || payload.result.result_type === "partial") ? (defaultOutput?.output_id ?? "") : ""));
  const [playback, setPlayback] = useState<Record<string, number>>({});
  const [invalidOutput, setInvalidOutput] = useState(() => Boolean(params.get("output") && !payload.outputs.some((item) => item.output_id === params.get("output"))));
  useEffect(() => { if (rawView !== "result" && rawView !== "materials") { const next = new URLSearchParams(params); next.set("view", "result"); setParams(next, { replace: true }); } }, [params, rawView, setParams]);
  useEffect(() => { if ((rawView === "result" || rawView === "materials") && defaultOutput && !params.get("output")) { const next = new URLSearchParams(params); next.set("output", defaultOutput.output_id); setParams(next, { replace: true }); } }, [defaultOutput, params, rawView, setParams]);
  useEffect(() => { if (defaultOutput && !payload.outputs.some((item) => item.output_id === outputId)) { const first = defaultOutput.output_id; setInvalidOutput(Boolean(outputId)); setOutputId(first); const next = new URLSearchParams(params); next.set("output", first); setParams(next, { replace: true }); } }, [defaultOutput, outputId, params, payload.outputs, setParams]);
  useEffect(() => { if (!payload.outputs.length && params.get("output")) { setInvalidOutput(true); const next = new URLSearchParams(params); next.delete("output"); setParams(next, { replace: true }); } }, [params, payload.outputs.length, setParams]);
  useEffect(() => {
    if (payload.result.seen) return;
    const key = `${run.run_id}:${payload.result.result_revision}`;
    if (seenAttempts.has(key)) return;
    const id = seenRequests.get(key) ?? requestId("result-seen"); seenRequests.set(key, id);
    seenAttempts.add(key);
    void projectApi.markResultSeen(run.run_id, id, payload.result.result_revision).then(() => { window.dispatchEvent(new Event("venus-results-changed")); return refresh(); }).catch((reason) => { seenAttempts.delete(key); if ((reason as ApiError).code === "revision_conflict") void refresh(); });
  }, [payload.result.result_revision, payload.result.seen, refresh, run.run_id]);
  const selectOutput = (nextId: string) => { setInvalidOutput(false); setOutputId(nextId); const next = new URLSearchParams(params); next.set("view", view); next.set("output", nextId); setParams(next); };
  const selectView = (nextView: "result" | "materials") => { const next = new URLSearchParams(params); next.set("view", nextView); if (outputId) next.set("output", outputId); setParams(next); };
  const selected = payload.outputs.find((item) => item.output_id === outputId) ?? payload.outputs[0];
  return <>
    <ReprocessControls run={run} project={project} />
    {invalidOutput && <p className="stale-warning" role="alert">链接指定的成片不存在或不属于这条记录。{selected?.available && "已显示其他可用成片。"}</p>}
    <section className="result-hero"><div><span className="eyebrow">{resultLabel(payload.result.result_type)}</span><h2>{payload.result.overall_summary || resultLabel(payload.result.result_type)}</h2>{payload.result.warnings.length > 0 && <p>{payload.result.warnings.join(" · ")}</p>}</div><div className="metric-grid"><Metric label="候选片段" value={payload.result.candidate_count} /><Metric label="已选片段" value={payload.result.selected_count} /><Metric label="可用成片" value={payload.result.available_output_count} /><Metric label="不可用成片" value={payload.result.failed_output_count} tone="error" /></div></section>
    <div className="result-tabs" role="tablist" aria-label="处理结果视图"><button role="tab" aria-selected={view === "result"} className={view === "result" ? "active" : ""} onClick={() => selectView("result")}>处理结果</button><button role="tab" aria-selected={view === "materials"} className={view === "materials" ? "active" : ""} onClick={() => selectView("materials")} disabled={!payload.outputs.length}>素材与发布文案</button></div>
    {payload.issues.length > 0 && <IssueStrip issues={payload.issues} params={params} setParams={setParams} />}
    {view === "result" ? <ResultView payload={payload} outputs={payload.outputs} selected={selected} playback={playback} setPlayback={setPlayback} selectOutput={selectOutput} /> : selected ? <MaterialEditor key={selected.output_id} output={selected} selectOutput={selectOutput} outputs={payload.outputs} /> : <div className="empty-state"><strong>暂无可编辑的发布文案</strong><p>{payload.result.result_type === "no_clip" ? "本次未选出片段，因此没有生成成片和发布文案。" : "暂时没有可编辑的发布文案。"}</p></div>}
    {params.get("issue") && <IssueDrawer key={params.get("issue")} issueId={params.get("issue")!} project={project} onClose={() => { const next = new URLSearchParams(params); next.delete("issue"); setParams(next, { replace: true }); }} onChanged={refresh} />}
  </>;
}

function ResultView({ payload, outputs, selected, playback, setPlayback, selectOutput }: { payload: RunResultPayload; outputs: RunOutput[]; selected?: RunOutput; playback: Record<string, number>; setPlayback(value: React.SetStateAction<Record<string, number>>): void; selectOutput(id: string): void }) {
  if (payload.result.result_type === "no_clip") return <section className="no-clip-state"><strong>本次未选出适合的片段</strong><p>{payload.result.overall_summary || "本次没有选出片段，可查看下方记录中的说明。"}</p><div className="decision-list">{payload.decisions.map((item) => <article key={item.decision_id}><span>{candidateLabel(item.candidate_type)}</span><strong>未入选</strong><p>{item.reason || "未提供未入选理由"}</p>{item.transcript_excerpt && <p>片段原文：{item.transcript_excerpt}</p>}</article>)}</div></section>;
  if (!outputs.length) return <div className="empty-state"><strong>暂无可用成片</strong>{payload.issues.length > 0 && <p>点击上方问题查看原因和处理方法。</p>}</div>;
  const index = Math.max(0, outputs.findIndex((item) => item.output_id === selected?.output_id)); const decision = payload.decisions.find((item) => item.output_id === selected?.output_id); const rejected = payload.decisions.filter((item) => item.decision === "rejected");
  return <section className="result-workspace"><div className="output-navigation"><button className="button" disabled={index <= 0} onClick={() => selectOutput(outputs[index - 1].output_id)}>上一个成片</button><OutputPicker outputs={outputs} selectedId={selected?.output_id ?? ""} onSelect={selectOutput} /><button className="button" disabled={index >= outputs.length - 1} onClick={() => selectOutput(outputs[index + 1].output_id)}>下一个成片</button></div><VideoPlayer output={selected} playback={playback} setPlayback={setPlayback} />{selected && <OutputFacts output={selected} />}{decision && <section className="selection-reason"><span className="eyebrow">入选理由</span><h3>{decision.hook || decision.core_value || candidateLabel(decision.candidate_type)}</h3><p>{decision.reason || "未提供入选理由"}</p><small>{formatRange(decision.selected_start_ms, decision.selected_end_ms)}{decision.transcript_excerpt ? ` · “${decision.transcript_excerpt}”` : ""}</small>{decision.risks.length > 0 && <p>注意：{decision.risks.join("、")}</p>}</section>}<section className="review-record"><SectionHeading title="片段筛选记录" subtitle={[payload.review_session?.model_name, payload.review_session?.completed_at ? `筛选完成于 ${time(payload.review_session.completed_at)}` : payload.result.completed_at ? `结果完成于 ${time(payload.result.completed_at)}` : null].filter(Boolean).join(" · ")} /><p>{payload.review_session?.overall_summary || payload.result.overall_summary}</p>{rejected.length > 0 && <details><summary>{rejected.length} 个片段未入选</summary><div className="decision-list">{rejected.map((item) => <article key={item.decision_id}><span>{candidateLabel(item.candidate_type)}</span><strong>未入选</strong><p>{item.reason || "未提供未入选理由"}</p>{item.transcript_excerpt && <p>片段原文：{item.transcript_excerpt}</p>}</article>)}</div></details>}</section></section>;
}

function VideoPlayer({ output, playback, setPlayback }: { output?: RunOutput; playback: Record<string, number>; setPlayback(value: React.SetStateAction<Record<string, number>>): void }) { const [mediaState, setMediaState] = useState<"loading" | "ready" | "error">("loading"); useEffect(() => setMediaState("loading"), [output?.output_id]); if (!output?.available || !output.media_url) return <div className="video-stage"><div className="video-unavailable">{output?.active_issue_summary?.summary || "成片文件暂时不可用。"}</div></div>; return <div className="video-stage">{mediaState === "loading" && <div className="video-loading" role="status">正在加载成片…</div>}<video aria-label={`播放 ${output.file_name}`} key={output.output_id} controls preload="metadata" src={output.media_url} onLoadedMetadata={(event) => { event.currentTarget.currentTime = playback[output.output_id] ?? 0; setMediaState("ready"); }} onError={() => setMediaState("error")} onTimeUpdate={(event) => { const currentTime = event.currentTarget.currentTime; setPlayback((current) => ({ ...current, [output.output_id]: currentTime })); }}>当前环境不支持视频播放。</video>{mediaState === "error" && <div className="video-error" role="alert">成片加载失败，仍可查看片段筛选记录。</div>}</div>; }

function candidateLabel(value: string) { return ({ highlight: "高光片段", summary: "总结片段", opening: "开场片段" } as Record<string, string>)[value] ?? "候选片段"; }
function formatRange(start: number | null, end: number | null) { if (start === null || end === null) return "未记录片段时间"; return `${formatDuration(start)} – ${formatDuration(end)}`; }

function OutputPicker({ outputs, selectedId, onSelect }: { outputs: RunOutput[]; selectedId: string; onSelect(id: string): void }) { return <div className="output-picker" role="tablist" aria-label="成片切换">{outputs.map((output, index) => <button role="tab" aria-selected={output.output_id === selectedId} className={output.output_id === selectedId ? "active" : ""} onClick={() => onSelect(output.output_id)} key={output.output_id}>成片 {index + 1}<small>{output.available ? formatDuration(output.duration_ms) : "当前不可用"}</small></button>)}</div>; }
function formatDuration(value: number | null) { if (value === null) return "—"; const seconds = Math.round(value / 1000); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }
function OutputFacts({ output }: { output: RunOutput }) { return <div className="output-facts"><span>{output.width && output.height ? `${output.width} × ${output.height}` : "分辨率未知"}</span><span>{output.duration_ms === null ? "时长未知" : formatDuration(output.duration_ms)}</span><span>{output.byte_size === null ? "大小未知" : formatBytes(output.byte_size)}</span><span>{output.container?.toUpperCase() ?? "格式未知"} · {output.video_codec ?? "编码未知"}</span></div>; }

function MaterialEditor({ output, outputs, selectOutput }: { output: RunOutput; outputs: RunOutput[]; selectOutput(id: string): void }) {
  const [detail, setDetail] = useState<RunOutput | null>(null); const [saved, setSaved] = useState<OutputMaterial | null>(null); const [draft, setDraft] = useState<OutputMaterial | null>(null);
  const [error, setError] = useState(""); const [copyNotice, setCopyNotice] = useState(""); const [serverVersion, setServerVersion] = useState<OutputMaterial | null>(null); const [showServer, setShowServer] = useState(false);
  const [state, setState] = useState<"loading" | "saved" | "dirty" | "saving" | "failed" | "conflict" | "unknown">("loading");
  const saving = useRef(false); const queued = useRef(false); const draftRef = useRef<OutputMaterial | null>(null); const savedRef = useRef<OutputMaterial | null>(null);
  const [tagInput, setTagInput] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setState("loading");
    Promise.all([projectApi.output(output.output_id, controller.signal), projectApi.material(output.output_id, controller.signal)]).then(([outputPayload, materialPayload]) => {
      const cached = materialDrafts.get(output.output_id); const baseline = materialPayload.material;
      setDetail(outputPayload.output); setSaved(baseline); savedRef.current = baseline;
      const initial = cached?.draft || baseline; setDraft(initial); draftRef.current = initial; setTagInput(initial.tags.join('，'));
      if (cached?.request) { cached.blocked = 'unknown'; setState('unknown'); }
      else if (cached && (cached.blocked === 'conflict' || initial.material_revision !== baseline.material_revision) && !sameEditable(initial, baseline)) {
        cached.blocked = 'conflict'; setServerVersion(baseline); setState('conflict'); setError('文案已被其他操作修改，你当前的修改尚未保存。请先查看最新内容，再决定是否覆盖。');
      } else setState(cached && !sameEditable(initial, baseline) ? 'dirty' : 'saved');
    }).catch(reason => { if (!controller.signal.aborted) setError((reason as Error).message); });
    return () => controller.abort();
  }, [output.output_id]);
  const persist = useCallback(async (explicit = false) => {
    const entry = materialDrafts.get(output.output_id);
    if (entry?.blocked && !explicit || entry?.blocked === 'conflict') return;
    if (saving.current) { queued.current = true; return; }
    const current = draftRef.current; const baseline = savedRef.current;
    if (!current || !baseline || !entry || !entry.request && sameMaterial(current, baseline)) return;
    if (!entry.request) {
      const problems = [current.titles.some(title => !title.text.trim()) ? '标题不能为空，填写后会自动保存。' : '', !current.preferred_title_id ? '请选择一个首选标题，选择后会自动保存。' : '', current.tags.length > 20 ? '标签最多 20 个，请减少后保存。' : ''].filter(Boolean);
      if (problems.length) { setError(problems.join('')); setState('failed'); return; }
      entry.request = { id: requestId('material-save'), snapshot: { ...current, material_revision: baseline.material_revision, titles: current.titles.map(item => ({ ...item })), tags: [...current.tags] } };
    }
    const attempt = entry.request; const recovering = entry.blocked === 'unknown';
    saving.current = true; setState('saving'); setError('');
    try {
      const result = await projectApi.saveMaterial(output.output_id, attempt.id, attempt.snapshot);
      entry.request = undefined; entry.blocked = undefined; savedRef.current = result.material; setSaved(result.material); setServerVersion(null);
      const latest = entry.draft;
      if (sameEditable(latest, attempt.snapshot)) {
        draftRef.current = result.material; setDraft(result.material); setTagInput(result.material.tags.join('，')); materialDrafts.delete(output.output_id); setState('saved');
      } else {
        const retained = { ...latest, material_revision: result.material.material_revision }; entry.draft = retained;
        draftRef.current = retained; setDraft(retained); setState('dirty');
        if (recovering) { entry.blocked = 'review'; queued.current = false; setError('上次提交已保存。新的修改仍未提交，请核对后点击立即保存。'); }
      }
    } catch (reason) {
      queued.current = false;
      const apiError = reason as ApiError;
      if (apiError.code === 'revision_conflict') {
        entry.request = undefined; entry.blocked = 'conflict'; setServerVersion(null); setState('conflict');
        setError('文案已被其他操作修改，你当前的修改尚未保存。请先查看最新内容，再决定是否覆盖。');
        try { const latest = (await projectApi.material(output.output_id)).material; savedRef.current = latest; setSaved(latest); setServerVersion(latest); }
        catch { setError('无法读取最新文案，你的修改仍保留。请重新读取后再比较。'); }
      } else if (apiError instanceof ApiError && !apiError.outcomeUnknown && apiError.code !== "request_id_conflict") {
        entry.request = undefined; entry.blocked = 'review'; setError(apiError.message); setState('failed');
      } else { entry.blocked = 'unknown'; setError('暂时无法确认保存结果，请核对原操作。'); setState('unknown'); }
    } finally {
      saving.current = false;
      if (queued.current && !entry.blocked) { queued.current = false; void persist(); }
    }
  }, [output.output_id]);
  useEffect(() => { if (state !== 'dirty') return; const timer = window.setTimeout(() => void persist(), 800); return () => window.clearTimeout(timer); }, [persist, state, draft]);
  useEffect(() => () => { void persist(); }, [persist]);
  const update = (next: OutputMaterial) => {
    const entry = materialDrafts.get(output.output_id) || { draft: next }; entry.draft = next; materialDrafts.set(output.output_id, entry);
    draftRef.current = next; setDraft(next);
    if (entry.blocked === 'conflict' || entry.blocked === 'unknown') return;
    if (!entry.request && savedRef.current && sameEditable(next, savedRef.current)) { materialDrafts.delete(output.output_id); setState('saved'); setError(''); return; }
    if (entry.blocked === 'review') entry.blocked = undefined;
    setState('dirty'); setError('');
  };
  const overwrite = () => {
    const entry = materialDrafts.get(output.output_id); if (!entry || !serverVersion) return;
    entry.blocked = undefined; savedRef.current = serverVersion; void persist(true);
  };
  if (error && !draft) return <ErrorState message={error} retry={() => window.location.reload()} />;
  if (!draft || !detail || !saved) return <LoadingState />;
  const preferred = draft.titles.find((item) => item.title_id === draft.preferred_title_id)?.text ?? "";
  const copy = async (value: string, label: string) => { try { await copyText(value); setError(""); setCopyNotice(`已复制${label}`); } catch (reason) { setCopyNotice(""); setError((reason as Error).message); } };
  const shellAction = async (action: (() => Promise<{ ok: true }>) | undefined, success: string) => { if (!action) return; try { await action(); setError(""); setCopyNotice(success); } catch (reason) { setError((reason as Error).message); setCopyNotice(""); } };
  return <section className="materials-workspace"><OutputPicker outputs={outputs} selectedId={output.output_id} onSelect={(id) => { void persist().then(() => selectOutput(id)); }} /><div className="material-layout"><aside className="file-panel"><span className="eyebrow">成片文件</span><strong>{detail.file_name}</strong><small title={detail.display_path}>{detail.display_path ?? "暂时无法读取文件位置"}</small><OutputFacts output={detail} /><small>生成于 {time(detail.generated_at)}</small><div className="actions"><button className="button" disabled={!window.liveClipperShell?.openOutput} onClick={() => void shellAction(window.liveClipperShell?.openOutput ? () => window.liveClipperShell!.openOutput!(detail.output_id) : undefined, "已请求打开成片")}>打开成片</button><button className="button" disabled={!window.liveClipperShell?.revealOutput} onClick={() => void shellAction(window.liveClipperShell?.revealOutput ? () => window.liveClipperShell!.revealOutput!(detail.output_id) : undefined, "已请求在 Finder 中显示成片")}>在 Finder 中显示</button><button className="button" disabled={!detail.display_path} onClick={() => detail.display_path && void copy(detail.display_path, "成片路径")}>复制路径</button></div>{(!window.liveClipperShell?.openOutput || !window.liveClipperShell?.revealOutput) && <small>请在 Venus 桌面应用中打开或定位成片。</small>}</aside><div className="material-editor form-surface"><div className="save-state" role="status">{{ loading: "正在加载…", saved: "已保存", dirty: "待保存", saving: "正在保存…", failed: "保存失败", conflict: "保存冲突", unknown: "保存结果未确认" }[state]}</div>{copyNotice && <div className="copy-notice" role="status">{copyNotice}</div>}{error && <p className="form-error" role="alert">{error}{serverVersion && <button className="text-button" onClick={() => setShowServer((value) => !value)}>{showServer ? "收起最新内容" : "查看最新内容"}</button>}</p>}{showServer && serverVersion && <div className="server-version"><strong>最新保存的文案</strong>{serverVersion.titles.map((title, index) => <p key={title.title_id}>标题 {index + 1}{title.title_id === serverVersion.preferred_title_id ? "（首选）" : ""}：{title.text}</p>)}<p>{serverVersion.description}</p><small>{serverVersion.tags.map((tag) => `#${tag}`).join(" ")}</small></div>}<fieldset><legend>备选标题</legend>{draft.titles.map((title, index) => <label className="title-candidate" key={title.title_id}><input type="radio" aria-label={`将标题 ${index + 1} 设为首选`} name="preferred-title" checked={draft.preferred_title_id === title.title_id} onChange={() => update({ ...draft, preferred_title_id: title.title_id })} /><input aria-label={`标题 ${index + 1}`} aria-invalid={!title.text.trim()} className="form-control" value={title.text} maxLength={100} onChange={(event) => update({ ...draft, titles: draft.titles.map((item) => item.title_id === title.title_id ? { ...item, text: event.target.value } : item) })} /><button className="text-button" type="button" onClick={() => void copy(title.text, "标题")}>复制标题</button></label>)}</fieldset><Field inputID="material-description" label="视频描述" width="100%"><textarea className="form-control" id="material-description" rows={5} value={draft.description} maxLength={2000} onChange={(event) => update({ ...draft, description: event.target.value })} /></Field><TextInput label="标签（用逗号分隔，最多 20 个）" onChange={value => { setTagInput(value); update({ ...draft, tags: normalizeTags(value) }); }} value={tagInput} status={draft.tags.length > 20 ? { type: "error", message: "标签最多 20 个，请减少后保存。" } : undefined} width="100%" /><div className="tag-copy-list">{draft.tags.map((tag) => <button className="text-button" key={tag} onClick={() => void copy(`#${tag}`, `标签 #${tag}`)}>#{tag}</button>)}</div><div className="publish-preview"><span className="eyebrow">文案预览</span><strong>{preferred}</strong><p>{draft.description}</p><small>{draft.tags.map((tag) => `#${tag}`).join(" ")}</small></div><div className="actions"><button className="button" onClick={() => void copy(preferred, "首选标题")}>复制首选标题</button><button className="button" onClick={() => void copy(draft.description, "视频描述")}>复制描述</button><button className="button" onClick={() => void copy(draft.tags.map((tag) => `#${tag}`).join(" "), "全部标签")}>复制全部标签</button><button className="button" onClick={() => void copy(materialText(draft), "全部文案")}>复制全部文案</button><button className="button primary" disabled={state === "saving" || state === "saved" || state === "conflict" && !serverVersion} onClick={() => state === "conflict" ? overwrite() : void persist(true)}>{state === "unknown" ? "核对保存结果" : state === "conflict" ? "保留我的修改并保存" : state === "failed" ? "重试保存" : "立即保存"}</button>{state === "conflict" && !serverVersion && <button className="button" onClick={() => void projectApi.material(output.output_id).then(result => { setServerVersion(result.material); savedRef.current = result.material; setSaved(result.material); }).catch(reason => setError((reason as Error).message))}>重新读取最新文案</button>}</div></div></div></section>;
}

function sameEditable(a: OutputMaterial, b: OutputMaterial) { return JSON.stringify([a.titles, a.preferred_title_id, a.description, a.tags]) === JSON.stringify([b.titles, b.preferred_title_id, b.description, b.tags]); }
function sameMaterial(a: OutputMaterial, b: OutputMaterial) { return a.material_revision === b.material_revision && sameEditable(a, b); }
function normalizeTags(value: string) { return value.split(/[，,]/).map((item) => item.trim().replace(/^#+/, "")).filter(Boolean).filter((item, index, all) => all.indexOf(item) === index); }
function materialText(material: OutputMaterial) { const title = material.titles.find((item) => item.title_id === material.preferred_title_id)?.text.trim() ?? ""; return [title, material.description.trim(), material.tags.map((tag) => `#${tag}`).join(" ")].filter(Boolean).join("\n\n"); }

function IssueStrip({ issues, params, setParams }: { issues: IssueSummary[]; params: URLSearchParams; setParams(value: URLSearchParams, options?: { replace?: boolean }): void }) { return <section className="issue-strip"><SectionHeading title="待处理问题" subtitle={`${issues.length} 个问题`} /><div>{issues.map((issue) => <button key={issue.issue_id} onClick={() => { const next = new URLSearchParams(params); next.set("issue", issue.issue_id); setParams(next); }}><strong>{issue.title}</strong><span>{issue.summary}</span><small>{issue.next_step}</small></button>)}</div></section>; }

type IssueRequest = { id: string; revision: number; action: IssueAction; selection?: { kind: "source" | "recovery-output"; token: string } };
function readIssueRequest(key: string): IssueRequest | null { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; } }
export function IssueDrawer({ issueId, project, onClose, onChanged }: { issueId: string; project: ProjectSummary; onClose(): void; onChanged(): Promise<void> }) {
  const drawerRef = useRef<HTMLElement>(null); const closeRef = useRef(onClose);
  const [issue, setIssue] = useState<IssueDetail | null>(null); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false); const [repair, setRepair] = useState(false);
  const requestKey = `venus.issue-request.${issueId}`;
  const pending = useRef(readIssueRequest(requestKey)); const [unknown, setUnknown] = useState(Boolean(pending.current));
  const close = () => { if (!busy && !repair && !pending.current) onClose(); }; closeRef.current = close;
  const load = useCallback(async () => { try { setIssue((await projectApi.issue(issueId)).issue); setError(""); } catch (reason) { setError((reason as Error).message); } }, [issueId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; drawerRef.current?.querySelector<HTMLElement>("button")?.focus(); const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") { closeRef.current(); return; } if (event.key !== "Tab") return; const items = [...(drawerRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])]; if (!items.length) return; const first = items[0]; const last = items.at(-1)!; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }; window.addEventListener("keydown", keydown); return () => { window.removeEventListener("keydown", keydown); previous?.focus(); }; }, [issueId]);
  const act = async (action: IssueAction) => {
    if (!issue || busy || repair) return;
    if (!pending.current) {
      if (action === "open_resource_repair") { setRepair(true); return; }
      if (action === "copy_diagnostic") { try { await copyText(`${issue.diagnostic.diagnostic_id ?? ""}\n${issue.diagnostic.summary ?? issue.summary}`); setNotice("已复制诊断信息"); } catch (reason) { setError((reason as Error).message); } return; }
      if (!(action in ACTION_LABELS)) return;
    }
    setBusy(true); setError(''); setNotice('');
    try {
      if (!pending.current) {
        let selection: IssueRequest['selection'];
        if (action === 'select_source' || action === 'select_recovery_output') {
          const shell = window.liveClipperShell; const select = action === 'select_source' ? shell?.selectIssueSource : shell?.selectRecoveryOutput;
          if (!select) { setError('请在 Venus 桌面应用中选择文件或文件夹。'); return; }
          const chosen = await select(issue.issue_id); if (!chosen) { setNotice('已取消选择。'); return; }
          selection = { kind: action === 'select_source' ? 'source' : 'recovery-output', token: chosen.selectionToken };
        }
        pending.current = { id: requestId('issue-action'), revision: issue.issue_revision, action, selection };
        sessionStorage.setItem(requestKey, JSON.stringify(pending.current));
      }
      const original = pending.current;
      if (original.action === 'recheck' || original.selection) {
        const result = await projectApi.issueCheck(issueId, original.id, original.revision, original.selection);
        setIssue(result.issue); setNotice(result.issue.status === 'ready_to_recover' ? '检查通过，可以继续处理。' : result.issue.status === 'resolved' ? '问题已解决。' : '检查已完成，仍有问题需要处理。');
      } else {
        const route = ({ continue_run: 'continue', retry_output: 'retry-output', retry_material: 'retry-material' } as const)[original.action as 'continue_run' | 'retry_output' | 'retry_material'];
        await projectApi.issueRecover(issueId, original.id, route, original.revision);
        setNotice('已提交恢复处理，请查看记录的最新状态。');
      }
      pending.current = null; sessionStorage.removeItem(requestKey); setUnknown(false);
      await load(); await onChanged();
    } catch (reason) {
      if (pending.current && (!(reason instanceof ApiError) || reason.outcomeUnknown || reason.code === 'request_id_conflict')) {
        setUnknown(true); setError('暂时无法确认本次操作结果，请继续本次操作核对。');
      } else { pending.current = null; sessionStorage.removeItem(requestKey); setUnknown(false); await load(); setError((reason as Error).message); }
    } finally { setBusy(false); }
  };
  const resourceId = issue?.repair_resource_id;
  return <div className="issue-drawer-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}><aside ref={drawerRef} className="issue-drawer" role="dialog" aria-modal="true" aria-label="问题详情"><header><span className="eyebrow">问题详情</span><button aria-label="关闭" disabled={busy || unknown || repair} onClick={close}><RemixIcon name="close" /></button></header>{notice && <p className="copy-notice" role="status">{notice}</p>}{!issue ? error ? <ErrorState message={error} retry={() => void load()} /> : <LoadingState /> : <><h2>{issue.title}</h2><p>{issue.summary}</p><dl><div><dt>影响</dt><dd>{issue.impact}</dd></div><div><dt>保留的内容</dt><dd>{issue.preserved_content}</dd></div><div><dt>下一步</dt><dd>{issue.next_step}</dd></div></dl><p className="issue-stages">可沿用的步骤：{issue.reuse_stages.length ? stageNames(issue.reuse_stages) : '没有可沿用的步骤'}<br />需要重做的步骤：{issue.redo_stages.length ? stageNames(issue.redo_stages) : '尚未确定需要重做的步骤'}</p>{error && <p className="form-error" role="alert">{error}</p>}<div className="issue-actions">{unknown ? <button className="button primary" disabled={busy} onClick={() => void act(pending.current!.action)}>继续本次操作</button> : issue.available_actions.filter(action => action in ACTION_LABELS).map(action => <button className={action.startsWith("retry") || action === "continue_run" ? "button primary" : "button"} disabled={busy || repair} key={action} onClick={() => void act(action)}>{actionLabel(action)}</button>)}</div><section className="issue-events"><h3>问题处理记录</h3>{issue.events.map((event, index) => <div key={event.issue_event_id ?? event.event_id ?? index}><span>{time(event.occurred_at)}</span><strong>{eventLabel(event.event_type)}</strong></div>)}</section></>}{repair && issue && resourceId && <ResourceRepair issue={issue} resourceId={resourceId} onClose={() => setRepair(false)} onDone={async (message, keepOpen) => { setNotice(message); if (!keepOpen) setRepair(false); await load(); await onChanged(); }} />}</aside></div>;
}
const ACTION_LABELS: Record<IssueAction, string> = { recheck: "重新检查", open_resource_repair: "修复模型连接", select_source: "重新选择原始录像", select_recovery_output: "重新选择成片保存位置", continue_run: "继续处理", retry_output: "重新生成此成片", retry_material: "重新生成发布文案", copy_diagnostic: "复制诊断信息" };
function actionLabel(value: string) { const label = ACTION_LABELS[value as IssueAction]; if (!label) console.warn("Venus received an unknown issue action"); return label ?? "暂不支持此操作"; }
function stageNames(stages: Array<string | null>) { return stages.filter((stage): stage is string => Boolean(stage)).map(stage => STAGES.find(([value]) => value === stage)?.[1] ?? "暂时无法识别处理步骤").join("、"); }
function eventLabel(value: string) { return ({ opened: "问题已记录", recheck_started: "开始重新检查", recheck_failed: "重新检查未通过", recheck_succeeded: "检查通过，可以继续处理", ready_to_recover: "检查通过，可以继续处理", recovery_started: "已开始恢复处理", recovery_accepted: "已提交恢复处理", recovery_queued: "已加入处理队列", resolved: "问题已解决", material_saved: "发布文案已保存" } as Record<string, string>)[value] ?? "状态已更新"; }

function ResourceRepair({ issue, resourceId, onClose, onDone }: { issue: IssueDetail; resourceId: string; onClose(): void; onDone(message: string, keepOpen?: boolean): Promise<void> }) {
  const [resource, setResource] = useState<Resource | null>(null); const [credential, setCredential] = useState(''); const credentialRef = useRef(credential); credentialRef.current = credential;
  const [confirmed, setConfirmed] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [busy, setBusy] = useState(false);
  const operationKey = `venus.resource-repair.${issue.issue_id}.${resourceId}`;
  const operation = useRef(localStorage.getItem(operationKey)); const [unknown, setUnknown] = useState(Boolean(operation.current));
  const original = useRef<{ resource: Resource; credential: string; confirmed: boolean } | null>(null);
  useEffect(() => { let disposed = false; projectApi.repairContext(resourceId, issue.issue_id).then(value => api<{ resource: Resource }>(`/api/resources/${resourceId}/revisions/${value.repair_context.revision}`)).then(value => { if (!disposed) setResource(value.resource); }).catch(reason => setError((reason as Error).message)); return () => { disposed = true; }; }, [issue.issue_id, resourceId]);
  const repair = async () => {
    if (!resource || busy) return; setBusy(true); setError(''); setNotice('');
    let submitted = false;
    try {
      if (!operation.current) { operation.current = requestId('resource-repair'); original.current = { resource, credential, confirmed }; localStorage.setItem(operationKey, operation.current); }
      const previous = await api<{ result: unknown }>(`/api/resources/operations/${operation.current}`);
      if (!previous.result) {
        const validationId = operation.current + '-validate';
        const pending = await api<{ result: { validation_id?: string; results?: Resource['validation'] } | null }>(`/api/resources/operations/${validationId}`);
        if (pending.result && !pending.result.validation_id) { setUnknown(true); setNotice('暂时无法确认上次检查结果，请稍后继续本次操作。'); return; }
        const attempt = original.current;
        if (!attempt) { setUnknown(true); setNotice('正在核对上次操作。原 API Key 未保存在页面中，当前输入不会用于重发原操作。'); return; }
        const { resource: target, credential: key } = attempt;
        const proposal = { name: target.name, kind: target.kind, config: target.config };
        if (!pending.result) submitted = true;
        const evidence = pending.result || await post<{ validation_id: string; results: Resource['validation'] }>('/api/resources/validate', { request_id: validationId, proposal, resource_id: resourceId, revision: target.revision, credential: key || undefined, purposes: target.config.purposes });
        if (!target.config.purposes.every(purpose => evidence.results?.[purpose]?.state === 'ready')) {
          setError(target.kind === 'local_agent' ? '原配置检查未通过，连接未恢复。请核对后重试。' : '原配置检查未通过，API Key 未更换。请核对后重试。');
          operation.current = null; original.current = null; localStorage.removeItem(operationKey); setUnknown(false); return;
        }
        submitted = true;
        await post(`/api/resources/${resourceId}/repair`, { revision: target.revision, credential: key || undefined, validation_id: evidence.validation_id, request_id: operation.current, confirm_same_account: attempt.confirmed });
      }
      const changed = Boolean(credentialRef.current && credentialRef.current !== original.current?.credential);
      localStorage.removeItem(operationKey); operation.current = null; original.current = null; setUnknown(false);
      if (!changed) setCredential('');
      const message = resource.kind === 'local_agent' ? '连接检查已通过，请继续检查其他问题。' : 'API Key 已更新，请继续检查其他问题。';
      setNotice(changed ? `${message} 你新输入的 API Key 尚未提交。` : message);
      await onDone(message, changed);
    } catch (reason) {
      if (submitted && reason instanceof ApiError && !reason.outcomeUnknown && reason.code !== 'request_conflict') {
        operation.current = null; original.current = null; localStorage.removeItem(operationKey); setUnknown(false);
      } else setUnknown(Boolean(operation.current));
      setError((reason as Error).message);
    } finally { setBusy(false); }
  };
  return <div className="nested-dialog form-surface" role="dialog" aria-modal="true" aria-labelledby="repair-title"><h3 id="repair-title">修复原模型连接</h3>{resource && <><p>{resource.name} · 配置版本 {resource.revision}</p><p>{resource.kind === 'local_asr' ? '本机' : resource.kind === 'local_agent' ? '本机启动 · Claude Code' : resource.config.endpoint || '未设置服务地址'}{resource.kind !== 'local_agent' && ` · ${resource.config.model || '未记录模型'}`}</p>{Boolean(resource.projects?.length || resource.active_runs?.length || resource.failed_runs?.length) && <p>此次修复会影响：使用此配置版本的 {resource.projects?.length || 0} 个项目后续新建的记录，以及 {(resource.active_runs?.length || 0) + (resource.failed_runs?.length || 0)} 条尚未完成的记录。{resource.projects?.map(p => p.name).join('、')}</p>}<p>这里只更新原账号的连接信息，供应商、地域、业务空间、模型和处理参数不变。如需更换账号或模型，请先修改项目设置，再重新处理。</p>{resource.kind !== 'local_asr' && <p>检查会使用内置测试内容，不会发送你的录像。调用云端模型或 Claude Code 可能产生费用。</p>}{resource.kind === 'ai' || resource.kind === 'cloud_asr' ? <><label>原账号的新 API Key<input className="form-control" type="password" autoComplete="off" value={credential} onChange={event => setCredential(event.target.value)} /></label><label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认此 API Key 属于原供应商的同一账号和业务空间</label><button className="button primary" disabled={busy || !unknown && (!confirmed || !credential)} onClick={() => void repair()}>{busy ? '正在检查并修复…' : unknown ? '继续本次操作' : '检查并更新 API Key'}</button></> : <><p>{resource.kind === 'local_agent' ? '请恢复 Claude Code 的安装和登录状态，再重新检查。' : '请恢复原语音识别模型文件，再重新检查。'}</p>{resource.kind === 'local_agent' && <><label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认仍使用原 Claude Code 账号</label><button className="button" disabled={busy || !unknown && !confirmed} onClick={() => void repair()}>{unknown ? '继续本次操作' : '检查 Claude Code 并恢复连接'}</button></>}{resource.kind === 'local_asr' && <button className="button" disabled={busy} onClick={() => { setBusy(true); setError(''); post(`/api/resources/${resourceId}/prepare`, { expected_revision: resource.revision }).then(() => { const message = '已开始准备原模型，完成后请重新检查问题。'; setNotice(message); return onDone(message, true); }).catch(reason => setError(reason.message)).finally(() => setBusy(false)); }}>准备原模型</button>}</>}</>}{notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}<button className="button" disabled={busy || unknown} onClick={onClose}>关闭</button></div>;
}

export function LegacyRunView({ run, project, events }: { run: Run; project: ProjectSummary; events: Array<{ event_id: number; stage: string; occurred_at: string; event_type: string }> }) {
  const completedStages = new Set(events.filter(event => ["succeeded", "completed", "no_clip_completed"].includes(event.event_type)).map(event => event.stage));
  const eventNames: Record<string, string> = { started: "开始处理", process_started: "处理任务已启动", succeeded: "阶段完成", completed: "处理完成", failed: "处理失败", recovery_succeeded: "已恢复处理", recovery_failed: "恢复失败", review_ready: "等待片段筛选", awaiting_review: "旧版待审", review_started: "开始筛选片段", render_started: "开始生成成片", no_clip_completed: "处理完成，未选出片段", resource_binding_repaired: "模型连接已修复", review_registration_pending: "正在保存筛选结果", evidence_invalid: "处理结果校验未通过", automatic_retry_started: "正在重试处理" };
  const source = ({ scheduled: "定时扫描", manual: "手动扫描", legacy_import: "旧版导入" } as Record<string, string>)[run.trigger_source] ?? "来源未知";
  return <><ReprocessControls run={run} project={project} />{run.legacy_awaiting_review && <div className="project-alert" role="status"><strong>旧版待审记录</strong><p>这条记录在旧版中尚未完成处理。</p></div>}{run.error_summary && <div className={`project-alert${run.status === "failed" ? " error" : ""}`} role={run.status === "failed" ? "alert" : "status"}><strong>处理提示</strong><p>{run.error_summary}</p></div>}<section className="stage-rail" aria-label="处理阶段">{STAGES.map(([stage, label], index) => { const done = completedStages.has(stage); return <div className={done ? "done" : run.current_stage === stage && run.status === "processing" ? "current" : "pending"} key={stage}><span>{done ? <RemixIcon name="check" /> : index + 1}</span><strong>{label}</strong></div>; })}</section><div className="run-detail-grid"><article><span>原始录像</span><strong>{run.source_name}</strong></article><article><span>记录来源</span><strong>{source}</strong></article><article><span>配置版本</span><strong>{run.trigger_source === "legacy_import" ? "原处理配置未记录" : `第 ${run.config_revision} 版`}</strong></article><article><span>最后更新</span><strong>{time(run.updated_at)}</strong></article></div><section className="section-block"><SectionHeading title="处理记录" /><div className="timeline">{events.map((event) => <div key={event.event_id}><span>{time(event.occurred_at)}</span><strong>{STAGES.find(([stage]) => stage === event.stage)?.[1] ?? "未识别的处理阶段"}</strong><small>{eventNames[event.event_type] ?? "未识别的操作"}</small></div>)}{!events.length && <p className="quiet-state">暂无处理记录。</p>}</div></section><StatusPill status={run.status} label={RUN_LABELS[run.status]} /></>;
}
