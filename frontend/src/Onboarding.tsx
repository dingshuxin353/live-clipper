import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";

import { ApiError } from "./api";
import { projectApi, requestId } from "./project-api";
import type { ModelJob, OnboardingDraft, OnboardingEnvironment, OnboardingModel, OnboardingSession, OnboardingSnapshot, OnboardingStep, OnboardingValidationPayload, SourceFile } from "./project-dto";

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
function humanBytes(value: number) { if (!Number.isFinite(value) || value <= 0) return "0 B"; const units = ["B", "KiB", "MiB", "GiB"]; const rank = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** rank).toFixed(rank ? 1 : 0)} ${units[rank]}`; }
function diagnosticId(error: unknown) { return error instanceof ApiError && error.code !== "unknown_error" ? error.code.replaceAll("_", "-").toUpperCase() : null; }
function friendlyError(error: unknown) { const id = diagnosticId(error); return { message: error instanceof ApiError && error.code !== "unknown_error" ? error.message : "暂时无法完成此操作", id }; }

export function Onboarding({ snapshot, onSession, onRefresh, onPaused, onClose }: Props) {
  const navigate = useNavigate(); const initialSession = snapshot.session!;
  const [session, setSession] = useState(initialSession); const sessionRef = useRef(initialSession);
  const [step, setStep] = useState<OnboardingStep>(initialSession.current_step); const [draft, setDraft] = useState(() => draftFrom(snapshot));
  const draftRef = useRef(draft); const [environment, setEnvironment] = useState(snapshot.environment); const [models, setModels] = useState(snapshot.model_catalog);
  const [validation, setValidation] = useState<OnboardingValidationPayload | null>(null); const [error, setError] = useState(""); const [errorId, setErrorId] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false); const [busy, setBusy] = useState(""); const [saveState, setSaveState] = useState<"saved" | "saving" | "failed">("saved");
  const [asrStatus, setAsrStatus] = useState(snapshot.resources.asr.ready ? "ready" : "untested"); const [aiStatus, setAiStatus] = useState(snapshot.resources.ai.ready ? "ready" : "untested");
  const [downloadJob, setDownloadJob] = useState<ModelJob | null>(() => { const item = snapshot.model_catalog.find((model) => model.job_id); return item?.job_id ? { id: item.job_id, status: "running", bytes_downloaded: item.bytes_downloaded, bytes_total: item.bytes_total } : null; });
  const [trialFiles, setTrialFiles] = useState<SourceFile[] | null>(null); const [trialOpen, setTrialOpen] = useState(false); const [trialFile, setTrialFile] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false); const asrKeyInput = useRef<HTMLInputElement>(null); const aiKeyInput = useRef<HTMLInputElement>(null);
  const asrKeyRef = useRef(""); const aiKeyRef = useRef(""); const dialogRef = useRef<HTMLElement>(null); const titleRef = useRef<HTMLHeadingElement>(null); const nameEdited = useRef(Boolean(initialSession.draft.project?.name));
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
    if (section === "asr") setAsrStatus("untested");
    if (section === "ai") setAiStatus("untested");
    queueSave(patch);
  }, [queueSave]);

  const go = useCallback(async (next: OnboardingStep) => { try { await flush(next); setStep(next); setError(""); window.setTimeout(() => titleRef.current?.focus(), 0); } catch { /* the inline save error keeps the current step */ } }, [flush]);
  const pause = useCallback(async () => {
    if (busy) return; setBusy("pause"); setError("");
    try { await flush(step); const result = await projectApi.onboardingPause(sessionRef.current.revision); adoptSession(result.session); onPaused(result.session); }
    catch (caught) { handleFailure(caught, "暂时无法保存进度"); } finally { setBusy(""); }
  }, [adoptSession, busy, flush, handleFailure, onPaused, step]);

  useEffect(() => { titleRef.current?.focus(); }, []);
  useEffect(() => () => { if (saveTimer.current) window.clearTimeout(saveTimer.current); }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !trialOpen) { event.preventDefault(); void pause(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const controls = [...dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]')];
      if (!controls.length) return; const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown); return () => document.removeEventListener("keydown", keydown);
  }, [pause, trialOpen]);

  const selectedModelId = draft.asr?.local_model_id || snapshot.initial_local_model;
  const selectedModel = models.find((model) => model.id === selectedModelId) ?? models.find((model) => model.recommended) ?? models[0];
  const asrMode = draft.asr?.mode === "cloud" ? "cloud" : "local";
  const modelBusy = selectedModel?.state === "downloading" || ["queued", "running"].includes(downloadJob?.status ?? "");

  useEffect(() => {
    if (step !== "welcome" || session.state !== "in_progress" || environmentProbed.current) return;
    environmentProbed.current = true;
    void recheckEnvironment();
  }, [session.state, step]);

  const refreshModels = useCallback(async () => { const payload = await projectApi.modelCatalog(); setModels(payload.models); return payload.models; }, []);
  useEffect(() => {
    if (step !== "asr" || !downloadJob?.id || !["queued", "running"].includes(downloadJob.status)) return;
    let timer = 0; let stopped = false;
    const poll = async () => {
      if (document.hidden || stopped) return;
      try { const result = await projectApi.job(downloadJob.id); if (stopped) return; setDownloadJob(result.job); if (["succeeded", "failed", "interrupted"].includes(result.job.status)) await refreshModels(); }
      catch { if (!stopped) setError("暂时无法刷新下载进度，正在保留上次数据"); }
    };
    const schedule = () => { if (timer) window.clearInterval(timer); timer = window.setInterval(() => void poll(), 1000); };
    const visible = () => { if (!document.hidden) { void poll(); schedule(); } else if (timer) window.clearInterval(timer); };
    void poll(); schedule(); document.addEventListener("visibilitychange", visible);
    return () => { stopped = true; if (timer) window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [downloadJob?.id, downloadJob?.status, refreshModels, step]);

  useEffect(() => {
    if (session.state !== "completed" || !session.first_project?.project_id) return;
    const controller = new AbortController(); projectApi.sourceFiles(session.first_project.project_id, controller.signal).then((result) => setTrialFiles(result.files.filter((file) => file.selectable))).catch(() => setTrialFiles([])); return () => controller.abort();
  }, [session.first_project?.project_id, session.state]);

  async function recheckEnvironment() { setBusy("environment"); setError(""); try { const result = await projectApi.onboardingEnvironment(sessionRef.current.revision); setEnvironment(result.environment); } catch (caught) { handleFailure(caught); } finally { setBusy(""); } }
  async function prepareLocalAsr() {
    if (!selectedModel || busy) return; setBusy("asr"); setError("");
    try {
      await flush("asr");
      if (selectedModel.state !== "installed") {
        const result = await projectApi.downloadModel(selectedModel.id, selectedModel.download_source || "modelscope");
        if (!result.job?.id) throw new Error("missing job id"); setDownloadJob(result.job); await refreshModels();
      } else {
        const result = await projectApi.onboardingAsrLocal(sessionRef.current.revision, selectedModel.id, selectedModel.download_source || "modelscope"); adoptSession(result.session); setAsrStatus("ready");
      }
    } catch (caught) { handleFailure(caught); } finally { setBusy(""); }
  }
  async function submitCloudAsr() {
    if (busy) return; const apiKey = asrKeyRef.current.trim(); if (!apiKey) { setError("请填写识别 API key"); asrKeyInput.current?.focus(); return; }
    setBusy("asr"); setError(""); try { await flush("asr"); const result = await projectApi.onboardingAsrCloud(sessionRef.current.revision, draftRef.current.asr?.api_base || "", draftRef.current.asr?.model || "", apiKey); adoptSession(result.session); setAsrStatus("ready"); }
    catch (caught) { setAsrStatus("error"); handleFailure(caught); } finally { if (asrKeyInput.current) asrKeyInput.current.value = ""; asrKeyRef.current = ""; setBusy(""); }
  }
  async function submitAi() {
    if (busy) return; const apiKey = aiKeyRef.current.trim(); if (!apiKey) { setError("请填写 AI API key"); aiKeyInput.current?.focus(); return; }
    const preset = snapshot.provider_presets.find((item) => item.id === (draftRef.current.ai?.provider_id || "custom")); setBusy("ai"); setError("");
    try { await flush("ai"); const result = await projectApi.onboardingAi(sessionRef.current.revision, preset?.id || "custom", preset?.label || "其他兼容服务", draftRef.current.ai?.api_base || "", draftRef.current.ai?.model || "", apiKey); adoptSession(result.session); setAiStatus("ready"); }
    catch (caught) { setAiStatus("error"); handleFailure(caught); } finally { if (aiKeyInput.current) aiKeyInput.current.value = ""; aiKeyRef.current = ""; setBusy(""); }
  }
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
  return <div className="onboarding-layer" aria-hidden="false"><section ref={dialogRef} className="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
    <header className="onboarding-header"><div className="onboarding-brand"><img src="/static/venus-mark.png" alt="" /><strong>Venus</strong></div><div><span>首次设置</span><small>完成后即可开始自动剪片</small></div><button onClick={() => void pause()} disabled={Boolean(busy)}>稍后继续</button></header>
    {conflict && <div className="onboarding-conflict" role="alert"><span>设置已在另一个窗口更新</span><button onClick={() => void onRefresh().then((next) => { if (next.session) { adoptSession(next.session); draftRef.current = draftFrom(next); setDraft(draftRef.current); setStep(next.session.current_step); setConflict(false); setError(""); setSaveState("saved"); } })}>重新加载</button></div>}
    <div className="onboarding-layout"><aside className="onboarding-steps" aria-label="首次设置步骤">{STEPS.map((item, index) => <button key={item.id} className={index === stepIndex ? "active" : index < stepIndex ? "done" : ""} disabled={index > stepIndex || session.state !== "in_progress"} onClick={() => index < stepIndex && void go(item.id)}><span>{index < stepIndex ? "✓" : index + 1}</span><div><strong>{item.label}</strong><small>{item.note}</small></div></button>)}<p><strong>设置自动保留</strong><span aria-live="polite">{saveState === "saving" ? "正在保存非密钥设置…" : saveState === "failed" ? "自动保存失败，请处理后重试。" : "非密钥设置已保存。"}</span><br />关闭窗口或稍后继续，不会删除已下载模型和已提交配置。</p></aside>
      <main className="onboarding-content"><h1 ref={titleRef} tabIndex={-1} id="onboarding-title">{STEPS[stepIndex]?.label ?? "首次设置"}</h1>{error && <div className="onboarding-error" id="onboarding-action-error" role="alert"><span>{error}</span>{errorId && <small>问题编号：{errorId}</small>}</div>}
        {session.state === "activation_pending" ? <ActivationPending session={session} busy={busy} diagnosticsOpen={diagnosticsOpen} setDiagnosticsOpen={setDiagnosticsOpen} retry={retryService} /> : session.state === "completed" ? <Completed snapshot={snapshot} session={session} files={trialFiles} enter={() => { onClose(); navigate(`/projects/${session.first_project?.project_id}`); }} openTrial={() => setTrialOpen(true)} /> : step === "welcome" ? <Welcome environment={environment} busy={busy} recheck={recheckEnvironment} next={() => void go("asr")} pause={pause} /> : step === "asr" ? <AsrStep draft={draft} models={models} selected={selectedModel} mode={asrMode} status={asrStatus} busy={busy} job={downloadJob} error={error} keyInput={asrKeyInput} keyRef={asrKeyRef} update={updateDraft} prepareLocal={prepareLocalAsr} submitCloud={submitCloudAsr} back={() => void go("welcome")} next={() => void go("ai")} pause={pause} /> : step === "ai" ? <AiStep snapshot={snapshot} draft={draft} status={aiStatus} busy={busy} error={error} keyInput={aiKeyInput} keyRef={aiKeyRef} update={updateDraft} submit={submitAi} back={() => void go("asr")} next={() => void go("project")} pause={pause} /> : step === "project" ? <ProjectStep draft={draft} snapshot={snapshot} busy={busy} update={updateDraft} markNameEdited={() => { nameEdited.current = true; }} selectFolder={selectFolder} back={() => void go("ai")} validate={validateProject} pause={pause} /> : <ReviewStep draft={draft} snapshot={snapshot} validation={validation} busy={busy} back={() => void go("project")} validate={validateProject} finish={finish} pause={pause} />}
      </main></div>
  </section>{trialOpen && trialFiles && <TrialDialog files={trialFiles} selected={trialFile} setSelected={setTrialFile} close={() => setTrialOpen(false)} confirm={runTrial} busy={busy === "trial"} />}</div>;
}

function StepFooter({ back, pause, action, label, disabled, note }: { back?: () => void; pause(): void | Promise<void>; action(): void; label: string; disabled?: boolean; note: string }) { return <footer className="onboarding-footer">{back ? <button className="button" onClick={back}>上一步</button> : <span />}<button className="onboarding-pause" onClick={() => void pause()}>稍后继续</button><small>{note}</small><button className="button primary" disabled={disabled} onClick={action}>{label}</button></footer>; }
function Welcome({ environment, busy, recheck, next, pause }: { environment: OnboardingEnvironment; busy: string; recheck(): void; next(): void; pause(): void | Promise<void> }) {
  const groups = [{ label: "保存设置", names: ["app_home", "service_dir", "workspace_root", "sqlite"] }, { label: "媒体处理", names: ["ffmpeg", "ffprobe", "asr_runtime"] }, { label: "本地服务", names: ["embedded_service"] }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">欢迎使用 Venus</span><h2>从一段录像，到可以发布的成片</h2><p>把录像放入项目目录，Venus 会自动转写、分析和选片，并生成成片、标题、描述和标签。</p><div className="onboarding-flow"><div><b>1</b><strong>放入录像</strong></div><i>→</i><div><b>2</b><strong>自动理解与剪片</strong></div><i>→</i><div><b>3</b><strong>获得发布物料</strong></div></div><section className="onboarding-checks"><header><div><strong>{environment.status === "ready" ? "运行环境已准备好" : "运行环境需要处理"}</strong><p>以下结果来自本机实时状态。</p></div><button className="button" data-busy={busy === "environment" ? "true" : undefined} disabled={busy === "environment"} onClick={recheck}>{busy === "environment" ? "检查中…" : "重新检查"}</button></header>{groups.map((group) => { const checks = environment.checks.filter((item) => group.names.includes(item.name)); const ready = checks.every((item) => item.status === "ready"); return <div key={group.label} className={ready ? "ready" : "blocked"}><span>{ready ? "✓" : "!"}</span><strong>{group.label}</strong><small>{ready ? "已就绪" : checks.find((item) => item.problem)?.problem || "需要检查"}</small></div>; })}</section></div><StepFooter pause={pause} action={next} label="开始设置" disabled={environment.status !== "ready"} note="预计需要 5–10 分钟，模型下载可以恢复" /></div>;
}
function AsrStep({ draft, models, selected, mode, status, busy, job, error, keyInput, keyRef, update, prepareLocal, submitCloud, back, next, pause }: { draft: OnboardingDraft; models: OnboardingModel[]; selected?: OnboardingModel; mode: "local" | "cloud"; status: string; busy: string; job: ModelJob | null; error: string; keyInput: React.RefObject<HTMLInputElement | null>; keyRef: React.MutableRefObject<string>; update: <S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => void; prepareLocal(): void; submitCloud(): void; back(): void; next(): void; pause(): void | Promise<void> }) {
  const progress = job?.bytes_total ? Math.min(100, Math.round(((job.bytes_downloaded || 0) / job.bytes_total) * 100)) : selected?.bytes_total ? Math.min(100, Math.round((selected.bytes_downloaded / selected.bytes_total) * 100)) : 0;
  const localReady = status === "ready" && mode === "local"; const cloudReady = status === "ready" && mode === "cloud";
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">语音识别</span><h2>让 Venus 听懂录像内容</h2><p>推荐在本机完成识别，录像无需上传。也可以使用已有云端服务。</p><div className="onboarding-modes"><button className={mode === "local" ? "selected" : ""} onClick={() => update("asr", "mode", "local")}><strong>本机识别</strong><small>隐私更好，无持续调用费用</small><em>推荐</em></button><button className={mode === "cloud" ? "selected" : ""} onClick={() => update("asr", "mode", "cloud")}><strong>云端识别</strong><small>使用已有服务，不占本机空间</small></button></div>{mode === "local" ? <><div className="onboarding-models">{models.map((model) => <button key={model.id} className={selected?.id === model.id ? "selected" : ""} disabled={Boolean(job && ["queued", "running"].includes(job.status))} onClick={() => update("asr", "local_model_id", model.id)}><span>{model.recommended ? "建议" : model.tier_label}</span><strong>{model.tier_label}</strong><p>{model.speed_note} · {model.accuracy_note}</p><small>{model.size_note} · {model.ram_note}</small></button>)}</div>{selected && <section className="onboarding-download"><header><div><strong>{selected.tier_label}识别模型</strong><p>{selected.size_note} · 下载一次，多项目共用</p></div><span>{selected.state === "installed" ? "已就绪" : selected.state === "damaged" ? "需要修复" : job?.status === "running" || selected.state === "downloading" ? "正在下载" : selected.partial_bytes > 0 ? "可继续下载" : "尚未下载"}</span></header>{(job || selected.state === "downloading") && <div><div className="onboarding-progress" role="progressbar" aria-label="模型下载进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><i style={{ width: `${progress}%` }} /></div><small>{humanBytes(job?.bytes_downloaded ?? selected.bytes_downloaded)} / {humanBytes(job?.bytes_total ?? selected.bytes_total)} · 关闭窗口后可继续</small></div>}<button className="button primary" data-busy={busy === "asr" ? "true" : undefined} disabled={busy === "asr" || Boolean(job && ["queued", "running"].includes(job.status))} onClick={prepareLocal}>{selected.state === "installed" ? localReady ? "已保存" : "使用这个模型" : selected.partial_bytes ? "继续下载" : "下载模型"}</button></section>}</> : <section className="onboarding-connection"><label>服务地址<input value={draft.asr?.api_base || ""} aria-describedby={error ? "onboarding-action-error" : undefined} onChange={(event) => { update("asr", "api_base", event.target.value); }} /></label><label>识别模型<input value={draft.asr?.model || ""} onChange={(event) => update("asr", "model", event.target.value)} /></label><label>API key<input ref={keyInput} className="onboarding-secret-input" type="password" autoComplete="off" aria-invalid={error.includes("API key")} aria-errormessage={error ? "onboarding-action-error" : undefined} onInput={(event) => { keyRef.current = event.currentTarget.value; }} /></label><div className={`onboarding-test ${status}`}><div><strong>{cloudReady ? "云端识别可以使用" : status === "error" ? "连接需要修改" : "连接尚未验证"}</strong><p>{cloudReady ? "已保存凭据" : "通过真实请求确认服务和模型可以完成识别。"}</p></div><button className="button" data-busy={busy === "asr" ? "true" : undefined} disabled={busy === "asr"} onClick={submitCloud}>{busy === "asr" ? "测试中…" : "测试并保存"}</button></div></section>}</div><VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">{busy === "asr" ? "正在准备语音识别" : ""}</VisuallyHidden><StepFooter back={back} pause={pause} action={next} label="继续" disabled={mode === "local" ? !localReady : !cloudReady} note="只需完成一种识别方式" /></div>;
}
function AiStep({ snapshot, draft, status, busy, error, keyInput, keyRef, update, submit, back, next, pause }: { snapshot: OnboardingSnapshot; draft: OnboardingDraft; status: string; busy: string; error: string; keyInput: React.RefObject<HTMLInputElement | null>; keyRef: React.MutableRefObject<string>; update: <S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => void; submit(): void; back(): void; next(): void; pause(): void | Promise<void> }) {
  const providerId = draft.ai?.provider_id || snapshot.provider_presets[0]?.id || "custom";
  function choose(id: string) { const preset = snapshot.provider_presets.find((item) => item.id === id); update("ai", "provider_id", id); if (preset?.api_base) update("ai", "api_base", preset.api_base); if (preset?.model) update("ai", "model", preset.model); }
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">AI 服务</span><h2>连接内容分析与自动剪片能力</h2><p>首次设置只需验证一个服务，后续分析、审阅、选片和发布物料都会使用它。</p><section className="onboarding-connection"><div className="onboarding-providers">{snapshot.provider_presets.map((item) => <button key={item.id} className={providerId === item.id ? "selected" : ""} onClick={() => choose(item.id)}><strong>{item.label}</strong><small>{item.id === "custom" ? "兼容接口" : "填入建议配置"}</small></button>)}</div><label>服务地址<input value={draft.ai?.api_base || ""} aria-describedby={error ? "onboarding-action-error" : undefined} onChange={(event) => update("ai", "api_base", event.target.value)} /></label><label>模型<input value={draft.ai?.model || ""} onChange={(event) => update("ai", "model", event.target.value)} /></label><label>API key<input ref={keyInput} className="onboarding-secret-input" type="password" autoComplete="off" aria-invalid={error.includes("API key")} aria-errormessage={error ? "onboarding-action-error" : undefined} onInput={(event) => { keyRef.current = event.currentTarget.value; }} /></label><div className={`onboarding-test ${status}`}><div><strong>{status === "ready" ? "AI 服务连接成功" : status === "error" ? "连接需要修改" : "连接尚未验证"}</strong><p>{status === "ready" ? "已保存凭据" : "测试会发送最小请求，确认服务、凭据和模型可用。"}</p></div><button className="button" data-busy={busy === "ai" ? "true" : undefined} disabled={busy === "ai"} onClick={submit}>{busy === "ai" ? "测试中…" : "测试并保存"}</button></div></section><div className="onboarding-shared-service"><strong>内容分析 + AI 审阅与选片 + 标题、描述和标签</strong><span>使用同一个已验证服务</span></div></div><VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">{busy === "ai" ? "正在测试 AI 服务连接" : ""}</VisuallyHidden><StepFooter back={back} pause={pause} action={next} label="继续" disabled={status !== "ready"} note={status === "ready" ? "连接信息已安全保存" : "连接通过后才能继续"} /></div>;
}
function ProjectStep({ draft, snapshot, busy, update, markNameEdited, selectFolder, back, validate, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; busy: string; update: <S extends keyof OnboardingDraft>(section: S, field: string, value: string | number) => void; markNameEdited(): void; selectFolder(kind: "source" | "output"): void; back(): void; validate(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const scheduled = project.trigger_mode === "scheduled";
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">第一个项目</span><h2>告诉 Venus 从哪里开始工作</h2><p>项目是一条长期工作的内容生产线，其余选项使用安全默认值。</p><section className="onboarding-project-form"><label>项目名称<input value={project.name || ""} onChange={(event) => { markNameEdited(); update("project", "name", event.target.value); }} /></label><label>发现新录像<select value={project.trigger_mode || "manual"} onChange={(event) => update("project", "trigger_mode", event.target.value)}><option value="manual">仅手动扫描</option><option value="scheduled">定时扫描 + 手动扫描</option></select></label><label className="span-two">录像目录<div className="onboarding-folder"><input value={project.source_directory || ""} onChange={(event) => update("project", "source_directory", event.target.value)} /><button className="button" type="button" onClick={() => void selectFolder("source")}>选择…</button></div><small>{project.source_directory ? "目录将在最终检查中读取并统计已有录像" : "选择存放直播录像的文件夹"}</small></label>{scheduled && <><label>定时方式<select value={project.schedule_mode || "daily"} onChange={(event) => update("project", "schedule_mode", event.target.value)}><option value="daily">每天固定时间</option><option value="interval">固定间隔</option></select></label>{project.schedule_mode === "interval" ? <label>扫描间隔<select value={project.interval_minutes || 60} onChange={(event) => update("project", "interval_minutes", Number(event.target.value))}><option value={30}>每 30 分钟</option><option value={60}>每 1 小时</option><option value={180}>每 3 小时</option><option value={360}>每 6 小时</option><option value={720}>每 12 小时</option></select></label> : <label>扫描时间<input type="time" value={project.daily_time || "22:00"} onChange={(event) => update("project", "daily_time", event.target.value)} /></label>}</>}<label className="span-two">成片保存位置<div className="onboarding-folder"><input value={project.output_directory || ""} onChange={(event) => update("project", "output_directory", event.target.value)} /><button className="button" type="button" onClick={() => void selectFolder("output")}>选择…</button></div></label></section><div className="onboarding-defaults"><div><span>已准备</span><strong>{snapshot.resources.asr.model_label || "语音识别"}与 {snapshot.resources.ai.model || "AI 服务"}</strong></div><div><span>自动采用</span><strong>AI 自动审阅并生成成片，只处理创建后新增录像</strong></div><div><span>文件保护</span><strong>原始录像永不自动删除，成片与临时文件分开保存</strong></div></div></div><StepFooter back={back} pause={pause} action={validate} label={busy === "validate" ? "检查中…" : "检查配置"} disabled={busy === "validate" || !project.name || !project.source_directory || !project.output_directory} note="继续前会执行真实目录与资源检查" /></div>;
}
function ReviewStep({ draft, snapshot, validation, busy, back, validate, finish, pause }: { draft: OnboardingDraft; snapshot: OnboardingSnapshot; validation: OnboardingValidationPayload | null; busy: string; back(): void; validate(): void; finish(): void; pause(): void | Promise<void> }) {
  const project = draft.project ?? {}; const canFinish = Boolean(validation && validation.fatal.length === 0 && validation.blockers.length === 0);
  if (!validation) return <div className="onboarding-step"><div className="onboarding-scroll"><h2>正在等待最终检查</h2><p>请重新检查语音识别、AI 服务、录像目录和成片位置。</p></div><StepFooter back={back} pause={pause} action={validate} label="重新检查" disabled={busy === "validate"} note="结果只来自本机实时检查" /></div>;
  const checks = [{ label: "语音识别", ready: validation.checks.asr.ready }, { label: "AI 服务", ready: validation.checks.ai.ready }, { label: "录像目录", ready: validation.checks.source_directory.status === "ready" }, { label: "成片位置", ready: ["ready", "creatable"].includes(validation.checks.output_directory.status) }];
  return <div className="onboarding-step"><div className="onboarding-scroll"><span className="onboarding-eyebrow">完成</span><h2>检查配置并创建第一个项目</h2><p>{canFinish ? "所有必要配置均已就绪。" : "仍有配置需要处理。"}</p><section className="onboarding-final-checks">{checks.map((item) => <div key={item.label} className={item.ready ? "ready" : "blocked"}><span>{item.ready ? "✓" : "!"}</span><strong>{item.label}</strong><small>{item.ready ? "已就绪" : "需要处理"}</small></div>)}</section>{[...validation.fatal, ...validation.blockers, ...validation.warnings].length > 0 && <div className="onboarding-validation" role="alert">{validation.fatal.map((item) => <p key={`fatal-${item.field}`}>必须修正：{item.message}</p>)}{validation.blockers.map((item) => <p key={`block-${item.field}`}>启用前需处理：{item.message}</p>)}{validation.warnings.map((item) => <p key={`warn-${item.field}`}>提醒：{item.message}</p>)}</div>}<dl className="onboarding-review"><div><dt>录像来源</dt><dd>{validation.summary.recording_source}</dd><small>目录可读 · 发现 {validation.existing_video_count} 个已有录像，默认不会自动处理</small></div><div><dt>发现方式</dt><dd>{project.trigger_mode === "scheduled" ? project.schedule_mode === "interval" ? `每 ${project.interval_minutes} 分钟扫描` : `每天 ${project.daily_time} 扫描` : "仅手动扫描"}</dd></div><div><dt>处理能力</dt><dd>{snapshot.resources.asr.model_label || "语音识别"} · {snapshot.resources.ai.model || "AI 服务"}</dd></div><div><dt>成片位置</dt><dd>{validation.summary.output}</dd></div></dl></div><VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">{busy === "finish" ? "正在创建项目并准备本机服务" : ""}</VisuallyHidden><StepFooter back={back} pause={pause} action={finish} label={busy === "finish" ? "正在创建项目并准备本机服务…" : "完成设置并创建项目"} disabled={!canFinish || busy === "finish"} note="创建期间请不要重复提交" /></div>;
}
function ActivationPending({ session, busy, diagnosticsOpen, setDiagnosticsOpen, retry }: { session: OnboardingSession; busy: string; diagnosticsOpen: boolean; setDiagnosticsOpen(value: boolean): void; retry(): void }) { return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish pending"><span>还差一步</span><h2>项目已保存，本机服务尚未启动</h2><p>项目和资源均已保留。当前不会扫描或处理录像。</p></section><div className="onboarding-service-issue"><strong>{session.failure?.summary || "本机处理服务尚未就绪"}</strong>{session.failure?.code && <small>问题编号：{session.failure.code}</small>}<div><button className="button" onClick={() => setDiagnosticsOpen(!diagnosticsOpen)}>{diagnosticsOpen ? "收起诊断" : "查看诊断"}</button><button className="button primary" disabled={busy === "retry"} onClick={retry}>{busy === "retry" ? "正在重新启动…" : "重新启动服务"}</button></div></div>{diagnosticsOpen && <div className="onboarding-diagnostic"><strong>诊断摘要</strong><p>{session.failure?.summary || "服务启动未完成。项目数据没有丢失。"}</p></div>}</div></div>; }
function Completed({ snapshot, session, files, enter, openTrial }: { snapshot: OnboardingSnapshot; session: OnboardingSession; files: SourceFile[] | null; enter(): void; openTrial(): void }) { const project = session.first_project!; const draft = session.draft.project ?? {}; return <div className="onboarding-step"><div className="onboarding-scroll"><section className="onboarding-finish complete"><span>✓ 设置完成</span><h2>{project.name} 已经可以工作</h2><p>项目已启用，语音识别和 AI 服务均已就绪。</p></section><dl className="onboarding-review"><div><dt>录像目录</dt><dd>{draft.source_directory || "已保存"}</dd></div><div><dt>发现新录像</dt><dd>{draft.trigger_mode === "scheduled" ? "定时扫描 + 手动扫描" : "仅手动扫描"}</dd></div><div><dt>成片保存位置</dt><dd>{draft.output_directory || snapshot.suggestions.output_directory}</dd></div><div><dt>处理能力</dt><dd>{snapshot.resources.asr.model_label || "语音识别"} · {snapshot.resources.ai.model || "AI 服务"}</dd></div></dl>{files?.length === 0 && <p className="onboarding-quiet">后续把新录像放入目录后，可从项目中手动扫描。</p>}</div><footer className="onboarding-footer complete-actions"><span /><span />{files && files.length > 0 && <button className="button" onClick={openTrial}>选择一条录像试运行</button>}<button className="button primary" onClick={enter}>进入项目</button></footer></div>; }
function TrialDialog({ files, selected, setSelected, close, confirm, busy }: { files: SourceFile[]; selected: string; setSelected(value: string): void; close(): void; confirm(): void; busy: boolean }) { return <div className="onboarding-trial-backdrop"><section className="onboarding-trial" role="dialog" aria-modal="true" aria-labelledby="onboarding-trial-title"><header><div><span>可选</span><h2 id="onboarding-trial-title">选择一条录像试运行</h2><p>会创建正式剪辑记录，处理可能需要一些时间。</p></div><button aria-label="关闭" onClick={close}>×</button></header><div>{files.map((file) => <label key={file.relative_path}><input type="radio" name="trial-file" checked={selected === file.relative_path} onChange={() => setSelected(file.relative_path)} /><span><strong>{file.relative_path}</strong><small>{humanBytes(file.bytes)}</small></span></label>)}</div><footer><button className="button" onClick={close}>暂不试运行</button><button className="button primary" disabled={!selected || busy} onClick={confirm}>{busy ? "正在受理…" : "用这条录像试运行"}</button></footer></section></div>; }
