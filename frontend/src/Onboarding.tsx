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
  { id: "welcome", label: "开始", note: "检查能否正常运行" }, { id: "asr", label: "语音识别", note: "把语音转成文字" },
  { id: "ai", label: "AI 服务", note: "分析内容，挑选片段" }, { id: "project", label: "第一个项目", note: "选择录像和成片目录" },
  { id: "complete", label: "确认设置", note: "" },
];

type Props = {
  snapshot: OnboardingSnapshot; onSession(session: OnboardingSession): void;
  onRefresh(): Promise<OnboardingSnapshot>; onPaused(session: OnboardingSession): void; onClose(): void;
};
type SaveItem = { patch: OnboardingDraft; step: OnboardingStep };
type SaveAttempt = SaveItem & { id: string; revision: number };

function draftFrom(snapshot: OnboardingSnapshot): OnboardingDraft {
  const draft = snapshot.session?.draft ?? {};
  return {
    asr: { mode: "local", local_model_id: snapshot.initial_local_model, model_source: "modelscope", ...(draft.asr ?? {}) },
    ai: { ...(draft.ai ?? {}) },
    project: { name: snapshot.suggestions.project_name || "我的第一个项目", trigger_mode: "manual", schedule_mode: "daily", daily_time: "22:00", interval_minutes: 60, output_directory: snapshot.suggestions.output_directory, ...(draft.project ?? {}) },
  };
}

function mergePatch(left: OnboardingDraft, right: OnboardingDraft): OnboardingDraft {
  return { ...left, ...right, asr: { ...(left.asr ?? {}), ...(right.asr ?? {}) }, ai: { ...(left.ai ?? {}), ...(right.ai ?? {}) }, project: { ...(left.project ?? {}), ...(right.project ?? {}) } };
}
function humanBytes(value: number) { return `${(value / 1024 ** 3).toFixed(1)} GiB`; }
function diagnosticId(error: unknown) { return error instanceof ApiError && error.code !== "unknown_error" ? error.code.replaceAll("_", "-").toUpperCase() : null; }
function friendlyError(error: unknown) { const id = diagnosticId(error); return { message: error instanceof ApiError && error.code !== "unknown_error" ? error.message : "操作未完成，请重试。", id }; }

export function Onboarding({ snapshot, onSession, onRefresh, onPaused, onClose }: Props) {
  const navigate = useNavigate(); const location = useLocation(); const initialSession = snapshot.session!;
  const [session, setSession] = useState(initialSession); const sessionRef = useRef(initialSession);
  const [step, setStep] = useState<OnboardingStep>(initialSession.current_step); const [draft, setDraft] = useState(() => draftFrom(snapshot));
  const draftRef = useRef(draft); const [environment, setEnvironment] = useState(snapshot.environment);
  const [validation, setValidation] = useState<OnboardingValidationPayload | null>(null); const [error, setError] = useState(""); const [errorId, setErrorId] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false); const [busy, setBusy] = useState(""); const [saveState, setSaveState] = useState<"saved" | "saving" | "failed" | "dirty">("saved");
  const [finishUnknown, setFinishUnknown] = useState(false);
  const [trialError, setTrialError] = useState("");
  const [trialFiles, setTrialFiles] = useState<SourceFile[] | null>(null); const [trialOpen, setTrialOpen] = useState(false); const [trialFile, setTrialFile] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null); const nameEdited = useRef(Boolean(initialSession.draft.project?.name));
  const saveTimer = useRef<number | null>(null); const pendingSave = useRef<SaveItem | null>(null); const saving = useRef<Promise<void> | null>(null); const finishId = useRef(initialSession.pending_finish_request_id || ""); const environmentProbed = useRef(false);
  const saveAttempt = useRef<SaveAttempt | null>(null);
  const saveFailure = useRef<unknown>(null);
  const pauseAttempt = useRef<{ id: string; revision: number } | null>(null);
  const finishRevision = useRef<number | null>(null);
  const trialAttempt = useRef<{ id: string; file: string } | null>(null);

  const adoptSession = useCallback((next: OnboardingSession) => { sessionRef.current = next; setSession(next); onSession(next); }, [onSession]);
  const handleFailure = useCallback((caught: unknown, fallback?: string) => { const detail = friendlyError(caught); setError(fallback || detail.message); setErrorId(detail.id); }, []);
  const drainSave = useCallback(async (retry = false): Promise<void> => {
    if (saving.current) return saving.current;
    if (saveFailure.current && !retry) throw saveFailure.current;
    const recovering = Boolean(saveFailure.current);
    saveFailure.current = null;
    const run = async () => {
      while (saveAttempt.current || pendingSave.current) {
        if (!saveAttempt.current) {
          saveAttempt.current = { ...pendingSave.current!, id: requestId("onboarding-draft"), revision: sessionRef.current.revision };
          pendingSave.current = null;
        }
        const item = saveAttempt.current; setSaveState("saving");
        try {
          const result = await projectApi.onboardingPatch(item.id, item.revision, item.step, item.patch);
          adoptSession(result.session); saveAttempt.current = null; setError("");
          setSaveState(pendingSave.current ? "dirty" : "saved");
          // Resolving an earlier submission does not authorize sending later edits.
          if (recovering) return;
        } catch (caught) {
          saveFailure.current = caught; setSaveState("failed");
          if (caught instanceof ApiError && caught.code === "onboarding_revision_conflict") { setConflict(true); setError("设置已在其他窗口更改，请重新加载。"); }
          else handleFailure(caught, caught instanceof ApiError && caught.outcomeUnknown ? "暂时无法确认设置是否保存，请核对原操作。" : "设置未保存，请重试。");
          throw caught;
        }
      }
    };
    saving.current = run().finally(() => { saving.current = null; }); return saving.current;
  }, [adoptSession, handleFailure]);
  const queueSave = useCallback((patch: OnboardingDraft, nextStep = step) => {
    if (saveFailure.current instanceof ApiError && !saveFailure.current.outcomeUnknown && saveFailure.current.code !== "onboarding_revision_conflict") {
      if (saveAttempt.current) pendingSave.current = { patch: mergePatch(saveAttempt.current.patch, pendingSave.current?.patch || {}), step: nextStep };
      saveAttempt.current = null; saveFailure.current = null;
    }
    pendingSave.current = pendingSave.current ? { patch: mergePatch(pendingSave.current.patch, patch), step: nextStep } : { patch, step: nextStep };
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    if (saveFailure.current) return;
    setSaveState("dirty");
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
    if (busy || finishUnknown) return; setBusy("pause"); setError("");
    try {
      if (!pauseAttempt.current) { await flush(step); pauseAttempt.current = { id: requestId("onboarding-pause"), revision: sessionRef.current.revision }; }
      const attempt = pauseAttempt.current;
      const result = await projectApi.onboardingPause(attempt.id, attempt.revision);
      pauseAttempt.current = null; adoptSession(result.session); onPaused(result.session);
    }
    catch (caught) { if (caught instanceof ApiError && !caught.outcomeUnknown) pauseAttempt.current = null; handleFailure(caught, "进度未保存，暂时无法退出设置。"); } finally { setBusy(""); }
  }, [adoptSession, busy, finishUnknown, flush, handleFailure, onPaused, step]);

  useEffect(() => { titleRef.current?.focus(); }, [step]);
  useEffect(() => () => { if (saveTimer.current) window.clearTimeout(saveTimer.current); }, []);


  useEffect(() => {
    if (step !== "welcome" || session.state !== "in_progress" || environmentProbed.current) return;
    environmentProbed.current = true;
    void recheckEnvironment();
  }, [session.state, step]);

  useEffect(() => {
    if (session.state !== "completed" || !session.first_project?.project_id) return;
    const controller = new AbortController(); setTrialError("");
    projectApi.sourceFiles(session.first_project.project_id, controller.signal).then((result) => setTrialFiles(result.files.filter((file) => file.selectable))).catch(caught => { if (!controller.signal.aborted) setTrialError(friendlyError(caught).message); }); return () => controller.abort();
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
    const select = window.liveClipperShell?.selectFolder;
    if (!select) { setError("请在 Venus 桌面应用中选择文件夹。"); setErrorId(null); return; }
    try {
      const value = await select(kind === "source" ? "选择录像文件夹" : "选择成片保存位置"); if (!value) return;
      updateDraft("project", kind === "source" ? "source_directory" : "output_directory", value);
      if (kind === "source" && !nameEdited.current) { const name = value.split(/[\\/]/).filter(Boolean).at(-1) || "我的第一个项目"; updateDraft("project", "name", name); }
    } catch (caught) { handleFailure(caught, "暂时无法选择文件夹，请稍后重试。"); }
  }
  async function validateProject() { setBusy("validate"); setError(""); try { await flush("project"); const result = await projectApi.onboardingValidate(sessionRef.current.revision); setValidation(result); setStep("complete"); } catch (caught) { handleFailure(caught); } finally { setBusy(""); } }
  async function finish() {
    if (busy) return; setBusy("finish"); setError("");
    try {
      if (finishRevision.current === null) { await flush("project"); finishRevision.current = sessionRef.current.revision; }
      if (!finishId.current) finishId.current = sessionRef.current.pending_finish_request_id || requestId("onboarding-finish");
      const result = await projectApi.onboardingFinish(finishRevision.current, finishId.current); setFinishUnknown(false); adoptSession(result.session);
    } catch (caught) {
      if (caught instanceof ApiError && caught.outcomeUnknown) {
        setFinishUnknown(true);
        try { const recovered = await onRefresh(); if (recovered.session) { adoptSession(recovered.session); finishId.current = recovered.session.pending_finish_request_id || finishId.current; if (["completed", "activation_pending"].includes(recovered.session.state)) { setFinishUnknown(false); return; } } } catch { /* retain original uncertainty message */ }
        setError("暂时无法确认项目是否创建成功，请留在此页重试。");
      } else { finishRevision.current = null; finishId.current = ""; setFinishUnknown(false); handleFailure(caught); }
    } finally { setBusy(""); }
  }
  async function retryService() {
    if (busy || !session.pending_finish_request_id) return; setBusy("retry"); setError("");
    try { const result = await projectApi.onboardingRetry(sessionRef.current.revision, session.pending_finish_request_id); adoptSession(result.session); }
    catch (caught) { handleFailure(caught); } finally { setBusy(""); }
  }
  async function runTrial() {
    const projectId = session.first_project?.project_id; if (!projectId || !trialFile || busy) return; setBusy("trial");
    try { trialAttempt.current ??= { id: requestId("onboarding-trial"), file: trialFile }; await projectApi.scan(projectId, trialAttempt.current.id, "selected", [trialAttempt.current.file]); trialAttempt.current = null; setTrialOpen(false); onClose(); navigate(`/projects/${projectId}`); }
    catch (caught) { if (caught instanceof ApiError && !caught.outcomeUnknown) trialAttempt.current = null; handleFailure(caught, trialAttempt.current ? "暂时无法确认是否开始处理，请重试原操作。" : undefined); } finally { setBusy(""); }
  }

  const stepIndex = STEPS.findIndex((item) => item.id === step);
  return <Dialog isOpen role="dialog" purpose={busy || trialOpen || finishUnknown || pauseAttempt.current ? "required" : "form"} onOpenChange={open => { if (!open) void pause(); }} width={1120} maxHeight="94dvh" padding={0} aria-labelledby="onboarding-title"><section className="onboarding-shell">
    <header className="onboarding-header"><div className="onboarding-brand"><img src="/static/venus-mark.png" alt="" /><strong>Venus</strong></div><div><span>首次设置</span></div><Button data-autofocus onClick={() => void pause()} isDisabled={Boolean(busy) || finishUnknown} label={"稍后继续"} /></header>
    {conflict && <div className="onboarding-conflict" role="alert"><span>设置已在其他窗口更改，请重新加载。</span><Button onClick={() => void onRefresh().then((next) => { if (next.session) { adoptSession(next.session); draftRef.current = draftFrom(next); setDraft(draftRef.current); setStep(next.session.current_step); setConflict(false); saveAttempt.current = null; pendingSave.current = null; saveFailure.current = null; if (saveTimer.current) window.clearTimeout(saveTimer.current); setError(""); setSaveState("saved"); } }).catch(handleFailure)} label={"重新加载"} /></div>}
    <div className="onboarding-layout"><aside className="onboarding-steps" aria-label="首次设置步骤">{STEPS.map((item, index) => <Button key={item.id} isDisabled={finishUnknown || Boolean(pauseAttempt.current) || Boolean(busy) || index > stepIndex || session.state !== "in_progress"} onClick={() => index < stepIndex && void go(item.id)} className="onboarding-step-button" label={item.label} variant={index === stepIndex ? "primary" : "secondary"} icon={<span>{index < stepIndex ? <RemixIcon name="check" /> : index + 1}</span>}><span className="onboarding-step-text"><span>{item.label}</span><small>{item.note}</small></span></Button>)}<p><strong>进度自动保存</strong><span aria-live="polite">{saveState === "saving" ? "正在保存设置进度…" : saveState === "failed" ? "设置未保存，暂时不要关闭窗口。" : saveState === "dirty" ? "有尚未保存的修改。" : "设置进度已保存。"}</span>{!conflict && ["failed", "dirty"].includes(saveState) && <Button onClick={() => void drainSave(true).catch(() => undefined)} label={saveState === "failed" ? "核对并重试保存" : "保存修改"} />}<br />已保存的设置和已下载的模型会保留，下次可继续。API Key 不会自动保存，请在模型配置页保存。</p></aside>
      <main className="onboarding-content"><h1 ref={titleRef} tabIndex={-1} id="onboarding-title">{STEPS[stepIndex]?.label ?? "首次设置"}</h1>{error && <div className="onboarding-error" id="onboarding-action-error" role="alert"><span>{error}</span>{errorId && <small>问题编号：{errorId}</small>}</div>}
        {pauseAttempt.current ? <p role="status">正在核对暂停结果，请点击上方“稍后继续”继续原操作。</p> : location.pathname.startsWith("/resources") && new URLSearchParams(location.search).get("origin") === "onboarding" ? <ResourcesPage /> : session.state === "activation_pending" ? <ActivationPending session={session} busy={busy} diagnosticsOpen={diagnosticsOpen} setDiagnosticsOpen={setDiagnosticsOpen} retry={retryService} /> : session.state === "completed" ? <Completed snapshot={snapshot} session={session} files={trialFiles} filesError={trialError} enter={() => { onClose(); navigate(`/projects/${session.first_project?.project_id}`); }} openTrial={() => setTrialOpen(true)} /> : step === "welcome" ? <Welcome environment={environment} busy={busy} recheck={recheckEnvironment} next={() => void go("asr")} pause={pause} /> : step === "asr" ? <ResourcePick purpose="asr" selected={draft.asr?.resource_id || ''} select={id => updateDraft('asr', 'resource_id', id)} open={id => void openResource('asr', id)} back={() => void go('welcome')} next={() => void go('ai')} pause={pause} /> : step === "ai" ? <ResourcePick purpose="analysis" selected={draft.ai?.resource_id || ''} select={id => updateDraft('ai', 'resource_id', id)} open={id => void openResource('analysis', id)} back={() => void go('asr')} next={() => void go('project')} pause={pause} /> : step === "project" ? <ProjectStep draft={draft} snapshot={snapshot} busy={busy} update={updateDraft} markNameEdited={() => { nameEdited.current = true; }} selectFolder={selectFolder} back={() => void go("ai")} validate={validateProject} pause={pause} /> : <ReviewStep draft={draft} snapshot={snapshot} validation={validation} busy={busy} back={finishUnknown ? undefined : () => void go("project")} validate={validateProject} finish={finish} pause={pause} />}
      </main></div>
  </section>{trialOpen && trialFiles && <TrialDialog files={trialFiles} selected={trialFile} setSelected={setTrialFile} close={() => setTrialOpen(false)} confirm={runTrial} busy={busy === "trial"} uncertain={Boolean(trialAttempt.current)} />}</Dialog>;
}

function StepFooter({ back, pause, action, label, disabled, note }: { back?: () => void; pause(): void | Promise<void>; action(): void; label: string; disabled?: boolean; note?: string }) { return <footer className="onboarding-footer">{back ? <Button onClick={back} label={"上一步"} /> : <span />}<Button onClick={() => void pause()} label={"稍后继续"} />{note && <small>{note}</small>}<Button isDisabled={disabled} onClick={action} label={(label)} variant="primary" /></footer>; }
function Welcome({ environment, busy, recheck, next, pause }: { environment: OnboardingEnvironment; busy: string; recheck(): void; next(): void; pause(): void | Promise<void> }) {
  const groups = [{ label: "数据保存", names: ["app_home", "service_dir", "workspace_root", "sqlite"] }, { label: "音视频处理", names: ["ffmpeg", "ffprobe", "asr_runtime"] }, { label: "后台服务", names: ["embedded_service"] }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><h2>欢迎使用 Venus</h2><p>先选择语音识别和 AI 模型，再创建一个项目。</p><section className="onboarding-checks"><header><div><strong>{environment.status === "ready" ? "检查通过，可以开始设置" : "检查未通过"}</strong></div><Button data-busy={busy === "environment" ? "true" : undefined} isDisabled={busy === "environment"} onClick={recheck} label={(busy === "environment" ? "检查中…" : "重新检查")} /></header>{groups.map((group) => { const checks = environment.checks.filter((item) => group.names.includes(item.name)); const ready = checks.length === group.names.length && checks.every((item) => item.status === "ready"); return <div key={group.label} className={ready ? "ready" : "blocked"}><span>{ready ? <RemixIcon name="check" /> : "!"}</span><strong>{group.label}</strong><small>{ready ? "正常" : checks.find((item) => item.problem)?.problem || "未通过"}</small></div>; })}</section></div><StepFooter pause={pause} action={next} label="开始设置" disabled={environment.status !== "ready"}  /></div>;
}
function ResourcePick({ purpose, selected, select, open, back, next, pause }: { purpose: 'asr' | 'analysis'; selected: string; select(id: string): void; open(id?: string): void; back(): void; next(): void; pause(): void | Promise<void> }) {
  const state = usePolling(signal => api<{ resources: Resource[] }>('/api/resources', {}, signal), 10000, `onboarding-${purpose}`);
  const options = (state.data?.resources || []).filter(r => !r.deleted && r.config.purposes.includes(purpose));
  const usable = (r: Resource) => r.validation[purpose]?.state === 'ready' && (purpose !== 'analysis' || r.validation.review?.state === 'ready');
  const resource = options.find(r => r.resource_id === selected);
  const ready = resource && usable(resource);
  return <div className="onboarding-step"><div className="onboarding-scroll"><h2>{purpose === 'asr' ? '选择语音识别模型' : '选择 AI 模型'}</h2><p>{purpose === 'asr' ? '用于把录像中的语音转成文字。首次使用请先添加模型。' : 'AI 会分析录像内容，挑选适合剪辑的片段。首次使用请先添加模型。'}</p>
    {state.error && <p role="alert">{state.data ? '显示的是上次读取结果。' : ''}{state.error}</p>}
    {state.loading && !state.data && <p role="status">正在读取模型…</p>}
    <Selector label="模型" value={selected} onChange={select} options={[{value:"",label:"请选择模型",disabled:true},...options.map(r => ({value:r.resource_id,label:`${r.name} · ${usable(r) ? '可用' : '不可用'}`}))]} width="100%" />
    <div className="resource-actions"><Button onClick={() => open()} label="添加模型" />{resource && <Button onClick={() => open(resource.resource_id)} label="查看模型" />}</div>
    {selected && state.data && <p role="status">{ready ? purpose === 'asr' ? '可以开始识别语音' : '可以分析内容并挑选片段' : '这个模型暂时无法使用，请查看模型详情。'}</p>}
    </div><StepFooter back={back} pause={pause} action={next} label="下一步" disabled={!ready || Boolean(state.error)} note="选择模型不会产生调用费用。" /></div>;
}
function ProjectStep({ draft, snapshot, busy, update, markNameEdited, selectFolder, back, validate, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; busy: string; update: <S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => void; markNameEdited(): void; selectFolder(kind: "source" | "output"): void; back(): void; validate(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const scheduled = project.trigger_mode === "scheduled";
  return <div className="onboarding-step"><div className="onboarding-scroll"><h2>创建第一个项目</h2><p>选择录像文件夹、成片保存位置，以及扫描新录像的方式。</p><FormLayout className="onboarding-project-form form-surface"><FormLayout className="form-pair"><TextInput label="项目名称" onChange={(value) => { markNameEdited(); update("project", "name", value); }} value={project.name || ""} width="100%" /><Selector label="扫描方式" onChange={(value) => update("project", "trigger_mode", value)} options={[{ value: "manual", label: "手动扫描" }, { value: "scheduled", label: "定时扫描（也可手动）" }]} value={project.trigger_mode || "manual"} width="100%" /></FormLayout><PathField choose={() => selectFolder("source")} description={project.source_directory ? "检查时会统计已有录像，不会开始处理。" : "选择存放录像的文件夹"} label="录像文件夹" onChange={(value) => update("project", "source_directory", value)} value={project.source_directory || ""} />{scheduled && <FormLayout className="form-subgroup"><FormLayout className="form-pair"><Selector label="定时方式" onChange={(value) => update("project", "schedule_mode", value)} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "固定间隔" }]} value={project.schedule_mode || "daily"} width="100%" />{project.schedule_mode === "interval" ? <Selector label="扫描间隔" onChange={(value) => update("project", "interval_minutes", Number(value))} options={[30, 60, 180, 360, 720].map((minutes) => ({ value: String(minutes), label: minutes < 60 ? `每 ${minutes} 分钟` : `每 ${minutes / 60} 小时` }))} value={String(project.interval_minutes || 60)} width="100%" /> : <Field inputID="onboarding-project-daily-time" label="扫描时间" width="100%"><input className="form-control" id="onboarding-project-daily-time" type="time" value={project.daily_time || "22:00"} onChange={(event) => update("project", "daily_time", event.target.value)} /></Field>}</FormLayout></FormLayout>}<PathField choose={() => selectFolder("output")} label="成片保存位置" onChange={(value) => update("project", "output_directory", value)} value={project.output_directory || ""} /></FormLayout><div className="onboarding-defaults"><div><span>已选模型</span><strong>语音识别：{snapshot.resources.asr.model_label || "未选择"}；AI：{snapshot.resources.ai.model || "未选择"}</strong></div><div><span>处理方式</span><strong>AI 会挑选片段并生成成片。默认只处理项目创建后新增的录像。</strong></div><div><span>文件处理</span><strong>原始录像不会自动删除。成片和临时文件分开保存。</strong></div></div></div><StepFooter back={back} pause={pause} action={validate} label={busy === "validate" ? "检查中…" : "检查并继续"} disabled={busy === "validate" || !project.name || !project.source_directory || !project.output_directory} note="检查文件夹和模型设置，不会开始处理录像。" /></div>;
}
function ReviewStep({ draft, snapshot, validation, busy, back, validate, finish, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; validation: OnboardingValidationPayload | null; busy: string; back?: () => void; validate(): void; finish(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const canFinish = Boolean(validation && validation.fatal.length === 0 && validation.blockers.length === 0);
  if (!validation) return <div className="onboarding-step"><div className="onboarding-scroll"><h2>请重新检查设置</h2><p>请重新检查模型设置、录像文件夹和成片保存位置。</p></div><StepFooter back={back} pause={pause} action={validate} label="重新检查" disabled={busy === "validate"}  /></div>;
  const checks = [{ label: "语音识别", ready: validation.checks.asr.ready }, { label: "AI 模型", ready: validation.checks.ai.ready }, { label: "录像文件夹", ready: validation.checks.source_directory.status === "ready" }, { label: "成片保存位置", ready: ["ready", "creatable"].includes(validation.checks.output_directory.status) }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><h2>确认项目信息</h2><p>{canFinish ? "检查通过，可以创建项目。" : "检查未通过，请先解决下面的问题。"}</p><section className="onboarding-final-checks">{checks.map((item) => <div key={item.label} className={item.ready ? "ready" : "blocked"}><span>{item.ready ? <RemixIcon name="check" /> : "!"}</span><strong>{item.label}</strong><small>{item.ready ? "正常" : "未通过"}</small></div>)}</section>{[...validation.fatal, ...validation.blockers, ...validation.warnings].length > 0 && <div className="onboarding-validation" role="alert">{validation.fatal.map((item) => <p key={`fatal-${item.field}`}>{item.message}</p>)}{validation.blockers.map((item) => <p key={`block-${item.field}`}>{item.message}</p>)}{validation.warnings.map((item) => <p key={`warn-${item.field}`}>提醒：{item.message}</p>)}</div>}<dl className="onboarding-review"><div><dt>录像文件夹</dt><dd>{validation.summary.recording_source}</dd>{validation.checks.source_directory.status === "ready" && <small>找到 {validation.existing_video_count} 个已有录像，默认不会自动处理</small>}</div><div><dt>扫描方式</dt><dd>{project.trigger_mode === "scheduled" ? project.schedule_mode === "interval" ? (project.interval_minutes! < 60 ? `每 ${project.interval_minutes} 分钟扫描` : `每 ${project.interval_minutes! / 60} 小时扫描`) : `每天 ${project.daily_time} 扫描` : "手动扫描"}</dd></div><div><dt>已选模型</dt><dd>语音识别：{snapshot.resources.asr.model_label || "未选择"}；AI：{snapshot.resources.ai.model || "未选择"}</dd></div><div><dt>成片保存位置</dt><dd>{validation.summary.output}</dd></div></dl></div><VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">{busy === "finish" ? "正在创建项目" : ""}</VisuallyHidden><StepFooter back={back} pause={pause} action={finish} label={busy === "finish" ? "正在创建项目…" : "创建项目"} disabled={!canFinish || busy === "finish"} note="创建后，项目会按所选扫描方式运行。" /></div>;
}
function ActivationPending({ session, busy, diagnosticsOpen, setDiagnosticsOpen, retry }: { session: OnboardingSession; busy: string; diagnosticsOpen: boolean; setDiagnosticsOpen(value: boolean): void; retry(): void }) { return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish pending"><h2>项目已保存，设置尚未完成</h2><p>项目和模型配置已保存，请查看下面的问题并重试。</p></section><div className="onboarding-service-issue"><strong>{session.failure?.summary || "后台服务暂时不可用"}</strong>{session.failure?.code && <small>问题编号：{session.failure.code}</small>}<div><Button onClick={() => setDiagnosticsOpen(!diagnosticsOpen)} label={(diagnosticsOpen ? "收起详情" : "查看详情")} /><Button isDisabled={busy === "retry"} onClick={retry} label={(busy === "retry" ? "正在重试…" : "重试")} variant="primary" /></div></div>{diagnosticsOpen && <div className="onboarding-diagnostic"><strong>问题详情</strong><p>{session.failure?.summary || "设置尚未完成，项目已保存。"}</p></div>}</div></div>; }
function Completed({ snapshot, session, files, filesError, enter, openTrial }: { snapshot: OnboardingSnapshot; session: OnboardingSession; files: SourceFile[] | null; filesError: string; enter(): void; openTrial(): void }) { const project = session.first_project!; const draft = session.draft.project ?? {}; return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish complete"><span><RemixIcon name="check" /> 设置完成</span><h2>{project.name} 已创建</h2><p>{files && files.length > 0 ? '接下来可以进入项目，或选择一段录像开始处理。' : '接下来可以进入项目。'}</p></section><dl className="onboarding-review"><div><dt>录像文件夹</dt><dd>{draft.source_directory || "已保存"}</dd></div><div><dt>扫描方式</dt><dd>{draft.trigger_mode === "scheduled" ? "定时扫描（也可手动）" : "手动扫描"}</dd></div><div><dt>成片保存位置</dt><dd>{draft.output_directory || snapshot.suggestions.output_directory}</dd></div><div><dt>已选模型</dt><dd>语音识别：{snapshot.resources.asr.model_label || "未选择"}；AI：{snapshot.resources.ai.model || "未选择"}</dd></div></dl>{filesError && <p role="alert">录像列表未能读取：{filesError}</p>}{files?.length === 0 && <p className="onboarding-quiet">{draft.trigger_mode === "scheduled" ? "把新录像放入所选文件夹后，Venus 会按设定时间扫描，也可以手动扫描。" : "把新录像放入所选文件夹后，在项目中点击扫描。"}</p>}</div><footer className="onboarding-footer complete-actions"><span /><span />{files && files.length > 0 && <Button onClick={openTrial} label={"选择录像开始处理"} />}<Button onClick={enter} label={"进入项目"} variant="primary" /></footer></div>; }
function TrialDialog({ files, selected, setSelected, close, confirm, busy, uncertain }: { files: SourceFile[]; selected: string; setSelected(value: string): void; close(): void; confirm(): void; busy: boolean; uncertain: boolean }) {
  return <DialogFrame title="选择要处理的录像" description="将创建一条剪辑记录并开始处理。使用云端模型可能产生调用费用。" onClose={close} closeDisabled={busy || uncertain} footer={<><Button isDisabled={busy || uncertain} onClick={close} label="取消" /><Button isLoading={busy} isDisabled={!selected || busy} onClick={confirm} label={uncertain ? "核对并重试" : "开始处理"} variant="primary" /></>}><div className="source-files">{files.map(file => <label key={file.relative_path}><input type="radio" name="trial-file" checked={selected === file.relative_path} disabled={busy || uncertain} onChange={() => setSelected(file.relative_path)} /><span><strong>{file.relative_path}</strong><small>{humanBytes(file.bytes)}</small></span></label>)}</div></DialogFrame>;
}
