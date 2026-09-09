import { Dialog } from '@astryxdesign/core/Dialog';
import { DialogFrame } from './ProjectDialogs';
import { Button } from '@astryxdesign/core/Button';
import { RemixIcon } from "./ui/RemixIcon";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Field } from "@astryxdesign/core/Field";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";

import { api, ApiError } from "./api";
import { projectApi, requestId } from "./project-api";
import type { OnboardingDraft, OnboardingEnvironment, OnboardingSession, OnboardingSnapshot, OnboardingStep, OnboardingValidationPayload, SourceFile } from "./project-dto";
import { ResourcesPage, type Resource } from "./ResourcesPage";
import { PathField, usePolling } from "./workbench-shared";

const STEPS: Array<{ id: OnboardingStep; label: string; note: string }> = [
  { id: "welcome", label: "开始", note: "检查运行环境" }, { id: "asr", label: "语音识别", note: "准备转写能力" },
  { id: "ai", label: "AI 服务", note: "连接内容分析" }, { id: "project", label: "第一个项目", note: "设置录像与输出" },
  { id: "complete", label: "完成", note: "检查并开始使用" },
];

type Props = {
  snapshot: OnboardingSnapshot; onSession(session: OnboardingSession): void;
  onRefresh(): Promise<OnboardingSnapshot>; onPaused(session: OnboardingSession): void; onClose(): void;
};
type SaveItem = { patch: OnboardingDraft; step: OnboardingStep };

function draftFrom(snapshot: OnboardingSnapshot): OnboardingDraft {
  const draft = snapshot.session?.draft ?? {};
  return {
    asr: { mode: "local", local_model_id: snapshot.initial_local_model, model_source: "modelscope", ...(draft.asr ?? {}) },
    ai: { ...(draft.ai ?? {}) },
    project: { name: snapshot.suggestions.project_name || "直播录像精选", trigger_mode: "manual", schedule_mode: "daily", daily_time: "22:00", interval_minutes: 60, output_directory: snapshot.suggestions.output_directory, ...(draft.project ?? {}) },
  };
}

function mergePatch(left: OnboardingDraft, right: OnboardingDraft): OnboardingDraft {
  return { ...left, ...right, asr: { ...(left.asr ?? {}), ...(right.asr ?? {}) }, ai: { ...(left.ai ?? {}), ...(right.ai ?? {}) }, project: { ...(left.project ?? {}), ...(right.project ?? {}) } };
}
function humanBytes(value: number) { return `${(value / 1024 ** 3).toFixed(1)} GiB`; }
function diagnosticId(error: unknown) { return error instanceof ApiError && error.code !== "unknown_error" ? error.code.replaceAll("_", "-").toUpperCase() : null; }
function friendlyError(error: unknown) { const id = diagnosticId(error); return { message: error instanceof ApiError && error.code !== "unknown_error" ? error.message : "暂时无法完成此操作", id }; }

export function Onboarding({ snapshot, onSession, onRefresh, onPaused, onClose }: Props) {
  const navigate = useNavigate(); const location = useLocation(); const initialSession = snapshot.session!;
  const [session, setSession] = useState(initialSession); const sessionRef = useRef(initialSession);
  const [step, setStep] = useState<OnboardingStep>(initialSession.current_step); const [draft, setDraft] = useState(() => draftFrom(snapshot));
  const draftRef = useRef(draft); const [environment, setEnvironment] = useState(snapshot.environment);
  const [validation, setValidation] = useState<OnboardingValidationPayload | null>(null); const [error, setError] = useState(""); const [errorId, setErrorId] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false); const [busy, setBusy] = useState(""); const [saveState, setSaveState] = useState<"saved" | "saving" | "failed">("saved");
  const [trialFiles, setTrialFiles] = useState<SourceFile[] | null>(null); const [trialOpen, setTrialOpen] = useState(false); const [trialFile, setTrialFile] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null); const nameEdited = useRef(Boolean(initialSession.draft.project?.name));
  const saveTimer = useRef<number | null>(null); const pendingSave = useRef<SaveItem | null>(null); const saving = useRef<Promise<void> | null>(null); const finishId = useRef(initialSession.pending_finish_request_id || ""); const environmentProbed = useRef(false);

  const adoptSession = useCallback((next: OnboardingSession) => { sessionRef.current = next; setSession(next); onSession(next); }, [onSession]);
  const handleFailure = useCallback((caught: unknown, fallback?: string) => { const detail = friendlyError(caught); setError(fallback || detail.message); setErrorId(detail.id); }, []);
  const drainSave = useCallback(async (): Promise<void> => {
    if (saving.current) return saving.current;
    const run = async () => {
      while (pendingSave.current) {
        const item = pendingSave.current; pendingSave.current = null; setSaveState("saving");
        try { const result = await projectApi.onboardingPatch(sessionRef.current.revision, item.step, item.patch); adoptSession(result.session); setSaveState("saved"); }
        catch (caught) {
          setSaveState("failed");
          if (caught instanceof ApiError && caught.code === "onboarding_revision_conflict") { setConflict(true); setError("设置已在另一个窗口更新"); }
          else handleFailure(caught, "暂时无法保存设置");
          throw caught;
        }
      }
    };
    saving.current = run().finally(() => { saving.current = null; }); return saving.current;
  }, [adoptSession, handleFailure]);
  const queueSave = useCallback((patch: OnboardingDraft, nextStep = step) => {
    pendingSave.current = pendingSave.current ? { patch: mergePatch(pendingSave.current.patch, patch), step: nextStep } : { patch, step: nextStep };
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { saveTimer.current = null; void drainSave().catch(() => undefined); }, 500);
  }, [drainSave, step]);
  const flush = useCallback(async (nextStep?: OnboardingStep) => {
    if (saveTimer.current) { window.clearTimeout(saveTimer.current); saveTimer.current = null; }
    if (nextStep) pendingSave.current = pendingSave.current ? { ...pendingSave.current, step: nextStep } : { patch: {}, step: nextStep };
    await drainSave();
  }, [drainSave]);

  const updateDraft = useCallback(<S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => {
    const patch = { [section]: { [field]: value } } as OnboardingDraft;
    const next = mergePatch(draftRef.current, patch); draftRef.current = next; setDraft(next); setValidation(null); setError("");
    queueSave(patch);
  }, [queueSave]);

  const go = useCallback(async (next: OnboardingStep) => { try { await flush(next); setStep(next); setError(""); } catch { /* the inline save error keeps the current step */ } }, [flush]);
  const pause = useCallback(async () => {
    if (busy) return; setBusy("pause"); setError("");
    try { await flush(step); const result = await projectApi.onboardingPause(sessionRef.current.revision); adoptSession(result.session); onPaused(result.session); }
    catch (caught) { handleFailure(caught, "暂时无法保存进度"); } finally { setBusy(""); }
  }, [adoptSession, busy, flush, handleFailure, onPaused, step]);

  useEffect(() => { titleRef.current?.focus(); }, [step]);
  useEffect(() => () => { if (saveTimer.current) window.clearTimeout(saveTimer.current); }, []);


  useEffect(() => {
    if (step !== "welcome" || session.state !== "in_progress" || environmentProbed.current) return;
    environmentProbed.current = true;
    void recheckEnvironment();
  }, [session.state, step]);

  useEffect(() => {
    if (session.state !== "completed" || !session.first_project?.project_id) return;
    const controller = new AbortController(); projectApi.sourceFiles(session.first_project.project_id, controller.signal).then((result) => setTrialFiles(result.files.filter((file) => file.selectable))).catch(() => setTrialFiles([])); return () => controller.abort();
  }, [session.first_project?.project_id, session.state]);

  async function recheckEnvironment() { setBusy("environment"); setError(""); try { const result = await projectApi.onboardingEnvironment(sessionRef.current.revision); setEnvironment(result.environment); } catch (caught) { handleFailure(caught); } finally { setBusy(""); } }
  async function openResource(purpose: 'asr' | 'analysis', identifier?: string) {
    try { await flush(step); navigate(`/resources/${identifier ? identifier : 'new'}?origin=onboarding&purpose=${purpose}`); }
    catch (caught) { handleFailure(caught); }
  }
  useEffect(() => {
    const query = new URLSearchParams(location.search); const identifier = query.get('selectedResource');
    const purpose = query.get('resourcePurpose');
    if (!identifier || !['asr', 'analysis'].includes(purpose || '')) return;
    let disposed = false;
    api<{ resource: Resource }>(`/api/resources/${encodeURIComponent(identifier)}`).then(result => {
      if (disposed || result.resource.deleted || !result.resource.config.purposes.includes(purpose as 'asr' | 'analysis')) return;
      updateDraft(purpose === 'asr' ? 'asr' : 'ai', 'resource_id', identifier);
      query.delete('selectedResource'); query.delete('resourcePurpose'); navigate(`${location.pathname}?${query}`, { replace: true });
    }).catch(handleFailure);
    return () => { disposed = true; };
  }, [location.search, location.pathname, navigate, updateDraft, handleFailure]);
  async function selectFolder(kind: "source" | "output") {
    const select = window.liveClipperShell?.selectFolder; if (!select) return;
    const value = await select(kind === "source" ? "选择录像目录" : "选择成片保存位置"); if (!value) return;
    updateDraft("project", kind === "source" ? "source_directory" : "output_directory", value);
    if (kind === "source" && !nameEdited.current) { const name = value.split(/[\\/]/).filter(Boolean).at(-1) || "直播录像精选"; updateDraft("project", "name", name); }
  }
  async function validateProject() { setBusy("validate"); setError(""); try { await flush("project"); const result = await projectApi.onboardingValidate(sessionRef.current.revision); setValidation(result); setStep("complete"); } catch (caught) { handleFailure(caught); } finally { setBusy(""); } }
  async function finish() {
    if (busy) return; setBusy("finish"); setError("");
    try {
      await flush("project"); if (!finishId.current) finishId.current = sessionRef.current.pending_finish_request_id || requestId("onboarding-finish");
      const result = await projectApi.onboardingFinish(sessionRef.current.revision, finishId.current); adoptSession(result.session);
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "network_error") {
        try { const recovered = await onRefresh(); if (recovered.session) { adoptSession(recovered.session); finishId.current = recovered.session.pending_finish_request_id || finishId.current; if (["completed", "activation_pending"].includes(recovered.session.state)) return; } } catch { /* retain original uncertainty message */ }
        setError("创建结果暂时无法确认，请保持当前窗口后重试");
      } else handleFailure(caught);
    } finally { setBusy(""); }
  }
  async function retryService() {
    if (busy || !session.pending_finish_request_id) return; setBusy("retry"); setError("");
    try { const result = await projectApi.onboardingRetry(sessionRef.current.revision, session.pending_finish_request_id); adoptSession(result.session); }
    catch (caught) { handleFailure(caught); } finally { setBusy(""); }
  }
  async function runTrial() {
    const projectId = session.first_project?.project_id; if (!projectId || !trialFile || busy) return; setBusy("trial");
    try { await projectApi.scan(projectId, requestId("onboarding-trial"), "selected", [trialFile]); setTrialOpen(false); onClose(); navigate(`/projects/${projectId}`); }
    catch (caught) { handleFailure(caught); } finally { setBusy(""); }
  }

  const stepIndex = STEPS.findIndex((item) => item.id === step);
  return <Dialog isOpen role="dialog" purpose={busy || trialOpen ? "required" : "form"} onOpenChange={open => { if (!open) void pause(); }} width={1120} maxHeight="94dvh" padding={0} aria-labelledby="onboarding-title"><section className="onboarding-shell">
    <header className="onboarding-header"><div className="onboarding-brand"><img src="/static/venus-mark.png" alt="" /><strong>Venus</strong></div><div><span>首次设置</span><small>设置处理资源和第一个项目</small></div><Button data-autofocus onClick={() => void pause()} isDisabled={Boolean(busy)} label={"稍后继续"} /></header>
    {conflict && <div className="onboarding-conflict" role="alert"><span>设置已在另一个窗口更新</span><Button onClick={() => void onRefresh().then((next) => { if (next.session) { adoptSession(next.session); draftRef.current = draftFrom(next); setDraft(draftRef.current); setStep(next.session.current_step); setConflict(false); setError(""); setSaveState("saved"); } })} label={"重新加载"} /></div>}
    <div className="onboarding-layout"><aside className="onboarding-steps" aria-label="首次设置步骤">{STEPS.map((item, index) => <Button key={item.id} isDisabled={index > stepIndex || session.state !== "in_progress"} onClick={() => index < stepIndex && void go(item.id)} label={item.label} variant={index === stepIndex ? "primary" : "secondary"} icon={<span>{index < stepIndex ? <RemixIcon name="check" /> : index + 1}</span>} endContent={<small>{item.note}</small>} />)}<p><strong>设置自动保留</strong><span aria-live="polite">{saveState === "saving" ? "正在保存非密钥设置…" : saveState === "failed" ? "自动保存失败，请处理后重试。" : "非密钥设置已保存。"}</span><br />关闭窗口或稍后继续，不会删除已下载模型和已提交配置。</p></aside>
      <main className="onboarding-content"><h1 ref={titleRef} tabIndex={-1} id="onboarding-title">{STEPS[stepIndex]?.label ?? "首次设置"}</h1>{error && <div className="onboarding-error" id="onboarding-action-error" role="alert"><span>{error}</span>{errorId && <small>问题编号：{errorId}</small>}</div>}
        {location.pathname.startsWith("/resources") && new URLSearchParams(location.search).get("origin") === "onboarding" ? <ResourcesPage /> : session.state === "activation_pending" ? <ActivationPending session={session} busy={busy} diagnosticsOpen={diagnosticsOpen} setDiagnosticsOpen={setDiagnosticsOpen} retry={retryService} /> : session.state === "completed" ? <Completed snapshot={snapshot} session={session} files={trialFiles} enter={() => { onClose(); navigate(`/projects/${session.first_project?.project_id}`); }} openTrial={() => setTrialOpen(true)} /> : step === "welcome" ? <Welcome environment={environment} busy={busy} recheck={recheckEnvironment} next={() => void go("asr")} pause={pause} /> : step === "asr" ? <ResourcePick purpose="asr" selected={draft.asr?.resource_id || ''} select={id => updateDraft('asr', 'resource_id', id)} open={id => void openResource('asr', id)} back={() => void go('welcome')} next={() => void go('ai')} pause={pause} /> : step === "ai" ? <ResourcePick purpose="analysis" selected={draft.ai?.resource_id || ''} select={id => updateDraft('ai', 'resource_id', id)} open={id => void openResource('analysis', id)} back={() => void go('asr')} next={() => void go('project')} pause={pause} /> : step === "project" ? <ProjectStep draft={draft} snapshot={snapshot} busy={busy} update={updateDraft} markNameEdited={() => { nameEdited.current = true; }} selectFolder={selectFolder} back={() => void go("ai")} validate={validateProject} pause={pause} /> : <ReviewStep draft={draft} snapshot={snapshot} validation={validation} busy={busy} back={() => void go("project")} validate={validateProject} finish={finish} pause={pause} />}
      </main></div>
  </section>{trialOpen && trialFiles && <TrialDialog files={trialFiles} selected={trialFile} setSelected={setTrialFile} close={() => setTrialOpen(false)} confirm={runTrial} busy={busy === "trial"} />}</Dialog>;
}

function StepFooter({ back, pause, action, label, disabled, note }: { back?: () => void; pause(): void | Promise<void>; action(): void; label: string; disabled?: boolean; note: string }) { return <footer className="onboarding-footer">{back ? <Button onClick={back} label={"上一步"} /> : <span />}<Button onClick={() => void pause()} label={"稍后继续"} /><small>{note}</small><Button isDisabled={disabled} onClick={action} label={(label)} variant="primary" /></footer>; }
function Welcome({ environment, busy, recheck, next, pause }: { environment: OnboardingEnvironment; busy: string; recheck(): void; next(): void; pause(): void | Promise<void> }) {
  const groups = [{ label: "保存设置", names: ["app_home", "service_dir", "workspace_root", "sqlite"] }, { label: "媒体处理", names: ["ffmpeg", "ffprobe", "asr_runtime"] }, { label: "本地服务", names: ["embedded_service"] }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">欢迎使用 Venus</span><h2>完成首次设置</h2><p>设置语音识别、AI 服务和第一个项目后，即可开始处理录像。</p><div className="onboarding-flow"><div><b>1</b><strong>设置处理资源</strong></div><i><RemixIcon name="arrowRight" /></i><div><b>2</b><strong>创建项目</strong></div><i><RemixIcon name="arrowRight" /></i><div><b>3</b><strong>处理录像</strong></div></div><section className="onboarding-checks"><header><div><strong>{environment.status === "ready" ? "运行环境已准备好" : "运行环境需要处理"}</strong><p>检查 Venus 运行所需的本机环境。</p></div><Button data-busy={busy === "environment" ? "true" : undefined} isDisabled={busy === "environment"} onClick={recheck} label={(busy === "environment" ? "检查中…" : "重新检查")} /></header>{groups.map((group) => { const checks = environment.checks.filter((item) => group.names.includes(item.name)); const ready = checks.every((item) => item.status === "ready"); return <div key={group.label} className={ready ? "ready" : "blocked"}><span>{ready ? <RemixIcon name="check" /> : "!"}</span><strong>{group.label}</strong><small>{ready ? "已就绪" : checks.find((item) => item.problem)?.problem || "需要检查"}</small></div>; })}</section></div><StepFooter pause={pause} action={next} label="开始设置" disabled={environment.status !== "ready"} note="模型下载中断后可以继续" /></div>;
}
function ResourcePick({ purpose, selected, select, open, back, next, pause }: { purpose: 'asr' | 'analysis'; selected: string; select(id: string): void; open(id?: string): void; back(): void; next(): void; pause(): void | Promise<void> }) {
  const state = usePolling(signal => api<{ resources: Resource[] }>('/api/resources', {}, signal), 10000, `onboarding-${purpose}`);
  const options = (state.data?.resources || []).filter(r => r.config.purposes.includes(purpose));
  const resource = options.find(r => r.resource_id === selected);
  const ready = resource?.validation[purpose]?.state === 'ready' && (purpose !== 'analysis' || resource.validation.review?.state === 'ready');
  return <div className="onboarding-step"><div className="onboarding-scroll"><h2>{purpose === 'asr' ? '选择语音识别资源' : '选择内容分析与审阅资源'}</h2><p>这里使用资源页中的同一份配置。新增、验证或下载后可返回继续。</p>{state.error && <p role="alert">{state.error}</p>}<Selector label={"资源"} value={selected} onChange={value => select(value)} options={[{value:"",label:"请选择",disabled:true},...options.map(r => ({value:r.resource_id,label:(r.name) + " " + "·" + " " + (r.ready ? '可用' : '待准备')}))]} width="100%"  /><div className="resource-actions"><Button onClick={() => open()} label={"添加资源"} />{resource && <Button onClick={() => open(resource.resource_id)} label={"查看和准备资源"} />}</div><p role="status">{ready ? '所需用途已通过验证' : '所选资源尚未就绪'}</p></div><StepFooter back={back} pause={pause} action={next} label="继续" disabled={!ready || Boolean(state.error)} note="仅选择资源，不会发起付费调用" /></div>;
}
function ProjectStep({ draft, snapshot, busy, update, markNameEdited, selectFolder, back, validate, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; busy: string; update: <S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => void; markNameEdited(): void; selectFolder(kind: "source" | "output"): void; back(): void; validate(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const scheduled = project.trigger_mode === "scheduled";
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">第一个项目</span><h2>设置第一个项目</h2><p>选择录像目录、成片位置和扫描方式。</p><FormLayout className="onboarding-project-form form-surface"><FormLayout className="form-pair"><TextInput label="项目名称" onChange={(value) => { markNameEdited(); update("project", "name", value); }} value={project.name || ""} width="100%" /><Selector label="发现新录像" onChange={(value) => update("project", "trigger_mode", value)} options={[{ value: "manual", label: "仅手动扫描" }, { value: "scheduled", label: "定时扫描 + 手动扫描" }]} value={project.trigger_mode || "manual"} width="100%" /></FormLayout><PathField choose={() => selectFolder("source")} description={project.source_directory ? "检查时会读取目录并统计已有录像" : "选择存放直播录像的文件夹"} label="录像目录" onChange={(value) => update("project", "source_directory", value)} value={project.source_directory || ""} />{scheduled && <FormLayout className="form-subgroup"><FormLayout className="form-pair"><Selector label="定时方式" onChange={(value) => update("project", "schedule_mode", value)} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "固定间隔" }]} value={project.schedule_mode || "daily"} width="100%" />{project.schedule_mode === "interval" ? <Selector label="扫描间隔" onChange={(value) => update("project", "interval_minutes", Number(value))} options={[30, 60, 180, 360, 720].map((minutes) => ({ value: String(minutes), label: minutes === 60 ? "每 1 小时" : `每 ${minutes} 分钟` }))} value={String(project.interval_minutes || 60)} width="100%" /> : <Field inputID="onboarding-project-daily-time" label="扫描时间" width="100%"><input className="form-control" id="onboarding-project-daily-time" type="time" value={project.daily_time || "22:00"} onChange={(event) => update("project", "daily_time", event.target.value)} /></Field>}</FormLayout></FormLayout>}<PathField choose={() => selectFolder("output")} label="成片保存位置" onChange={(value) => update("project", "output_directory", value)} value={project.output_directory || ""} /></FormLayout><div className="onboarding-defaults"><div><span>已准备</span><strong>{snapshot.resources.asr.model_label || "语音识别"}与 {snapshot.resources.ai.model || "AI 服务"}</strong></div><div><span>处理方式</span><strong>AI 自动判断并生成成片，默认只处理创建后新增的录像</strong></div><div><span>文件处理</span><strong>原始录像不会自动删除，成片与临时文件分开保存</strong></div></div></div><StepFooter back={back} pause={pause} action={validate} label={busy === "validate" ? "检查中…" : "检查配置"} disabled={busy === "validate" || !project.name || !project.source_directory || !project.output_directory} note="继续前会检查目录和处理资源" /></div>;
}
function ReviewStep({ draft, snapshot, validation, busy, back, validate, finish, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; validation: OnboardingValidationPayload | null; busy: string; back(): void; validate(): void; finish(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const canFinish = Boolean(validation && validation.fatal.length === 0 && validation.blockers.length === 0);
  if (!validation) return <div className="onboarding-step"><div className="onboarding-scroll"><h2>需要重新检查</h2><p>请重新检查语音识别、AI 服务、录像目录和成片位置。</p></div><StepFooter back={back} pause={pause} action={validate} label="重新检查" disabled={busy === "validate"} note="检查目录和处理资源后才能继续" /></div>;
  const checks = [{ label: "语音识别", ready: validation.checks.asr.ready }, { label: "AI 服务", ready: validation.checks.ai.ready }, { label: "录像目录", ready: validation.checks.source_directory.status === "ready" }, { label: "成片位置", ready: ["ready", "creatable"].includes(validation.checks.output_directory.status) }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">完成</span><h2>检查配置并创建第一个项目</h2><p>{canFinish ? "项目可以创建并启用。" : "仍有配置需要处理。"}</p><section className="onboarding-final-checks">{checks.map((item) => <div key={item.label} className={item.ready ? "ready" : "blocked"}><span>{item.ready ? <RemixIcon name="check" /> : "!"}</span><strong>{item.label}</strong><small>{item.ready ? "已就绪" : "需要处理"}</small></div>)}</section>{[...validation.fatal, ...validation.blockers, ...validation.warnings].length > 0 && <div className="onboarding-validation" role="alert">{validation.fatal.map((item) => <p key={`fatal-${item.field}`}>必须修正：{item.message}</p>)}{validation.blockers.map((item) => <p key={`block-${item.field}`}>启用前需处理：{item.message}</p>)}{validation.warnings.map((item) => <p key={`warn-${item.field}`}>提醒：{item.message}</p>)}</div>}<dl className="onboarding-review"><div><dt>录像来源</dt><dd>{validation.summary.recording_source}</dd><small>目录可读 · 发现 {validation.existing_video_count} 个已有录像，默认不会自动处理</small></div><div><dt>发现方式</dt><dd>{project.trigger_mode === "scheduled" ? project.schedule_mode === "interval" ? `每 ${project.interval_minutes} 分钟扫描` : `每天 ${project.daily_time} 扫描` : "仅手动扫描"}</dd></div><div><dt>处理能力</dt><dd>{snapshot.resources.asr.model_label || "语音识别"} · {snapshot.resources.ai.model || "AI 服务"}</dd></div><div><dt>成片位置</dt><dd>{validation.summary.output}</dd></div></dl></div><VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">{busy === "finish" ? "正在创建项目" : ""}</VisuallyHidden><StepFooter back={back} pause={pause} action={finish} label={busy === "finish" ? "正在创建项目…" : "完成设置并创建项目"} disabled={!canFinish || busy === "finish"} note="创建期间请不要重复提交" /></div>;
}
function ActivationPending({ session, busy, diagnosticsOpen, setDiagnosticsOpen, retry }: { session: OnboardingSession; busy: string; diagnosticsOpen: boolean; setDiagnosticsOpen(value: boolean): void; retry(): void }) { return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish pending"><span>还差一步</span><h2>项目已保存，本机服务尚未启动</h2><p>项目和资源均已保留。当前不会扫描或处理录像。</p></section><div className="onboarding-service-issue"><strong>{session.failure?.summary || "本机处理服务尚未就绪"}</strong>{session.failure?.code && <small>问题编号：{session.failure.code}</small>}<div><Button onClick={() => setDiagnosticsOpen(!diagnosticsOpen)} label={(diagnosticsOpen ? "收起诊断" : "查看诊断")} /><Button isDisabled={busy === "retry"} onClick={retry} label={(busy === "retry" ? "正在重新启动…" : "重新启动服务")} variant="primary" /></div></div>{diagnosticsOpen && <div className="onboarding-diagnostic"><strong>诊断摘要</strong><p>{session.failure?.summary || "服务启动未完成。项目数据没有丢失。"}</p></div>}</div></div>; }
function Completed({ snapshot, session, files, enter, openTrial }: { snapshot: OnboardingSnapshot; session: OnboardingSession; files: SourceFile[] | null; enter(): void; openTrial(): void }) { const project = session.first_project!; const draft = session.draft.project ?? {}; return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish complete"><span>✓ 设置完成</span><h2>{project.name} 已创建并启用</h2><p>项目已启用，语音识别和 AI 服务均已就绪。</p></section><dl className="onboarding-review"><div><dt>录像目录</dt><dd>{draft.source_directory || "已保存"}</dd></div><div><dt>发现新录像</dt><dd>{draft.trigger_mode === "scheduled" ? "定时扫描 + 手动扫描" : "仅手动扫描"}</dd></div><div><dt>成片保存位置</dt><dd>{draft.output_directory || snapshot.suggestions.output_directory}</dd></div><div><dt>处理能力</dt><dd>{snapshot.resources.asr.model_label || "语音识别"} · {snapshot.resources.ai.model || "AI 服务"}</dd></div></dl>{files?.length === 0 && <p className="onboarding-quiet">后续把新录像放入目录后，可从项目中手动扫描。</p>}</div><footer className="onboarding-footer complete-actions"><span /><span />{files && files.length > 0 && <Button onClick={openTrial} label={"选择一条录像试运行"} />}<Button onClick={enter} label={"进入项目"} variant="primary" /></footer></div>; }
function TrialDialog({ files, selected, setSelected, close, confirm, busy }: { files: SourceFile[]; selected: string; setSelected(value: string): void; close(): void; confirm(): void; busy: boolean }) {
  return <DialogFrame title="选择一条录像试运行" description="会创建正式剪辑记录，处理可能需要一些时间。" onClose={close} closeDisabled={busy} footer={<><Button isDisabled={busy} onClick={close} label="暂不试运行" /><Button isLoading={busy} isDisabled={!selected || busy} onClick={confirm} label="用这条录像试运行" variant="primary" /></>}><div className="source-files">{files.map(file => <label key={file.relative_path}><input type="radio" name="trial-file" checked={selected === file.relative_path} disabled={busy} onChange={() => setSelected(file.relative_path)} /><span><strong>{file.relative_path}</strong><small>{humanBytes(file.bytes)}</small></span></label>)}</div></DialogFrame>;
}
