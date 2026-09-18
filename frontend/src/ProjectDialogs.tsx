import { createPortal } from 'react-dom';
import { Dialog } from '@astryxdesign/core/Dialog';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { Button } from '@astryxdesign/core/Button';
import { RemixIcon } from "./ui/RemixIcon";
import { useEffect, useId, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Field } from "@astryxdesign/core/Field";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";

import { api, ApiError } from "./api";
import { ResourceAssignment } from "./ResourceAssignment";
import { projectApi, requestId } from "./project-api";
import type { FormOptionsPayload, ProjectSummary, ScanEvent, SourceFile, ValidationPayload } from "./project-dto";
import { DRAFT_KEY, LoadingState, Metric, PathField, ProjectDraft, StatusPill, configFromDraft, draftFromProject, emptyDraft, formatBytes, scanMessage, statusTone, time, usePolling } from "./workbench-shared";

export function safeRunReturn(value: string | null): string | null {
  if (!value) return null;
  const target = new URL(value, window.location.origin);
  return target.origin === window.location.origin && /^\/projects\/[^/?#]+\/runs\/[^/?#]+$/.test(target.pathname)
    ? `${target.pathname}${target.search}${target.hash}`
    : null;
}

export function DialogFrame({ title, description, children, footer, wide = false, alert = false, onClose, closeDisabled = false }: { title: string; description: string; children: React.ReactNode; footer?: React.ReactNode; wide?: boolean; alert?: boolean; onClose?(): void; closeDisabled?: boolean }) {
  const navigate = useNavigate(); const location = useLocation(); const titleID = useId(); const descriptionID = useId();
  const close = () => { if (closeDisabled) return; if (onClose) { onClose(); return; } const params = new URLSearchParams(location.search); params.delete("dialog"); params.delete("projectId"); navigate({ pathname: location.pathname, search: params.toString() }, { replace: true }); };
  const sourceRef = useRef(document.activeElement as HTMLElement | null);
  useEffect(() => {
    const source = sourceRef.current;
    return () => { queueMicrotask(() => {
      if (source?.isConnected && source !== document.body) source.focus();
      else { const heading = document.querySelector<HTMLElement>('main h1'); if (heading) { heading.tabIndex = -1; heading.focus(); } }
    }); };
  }, []);
  return createPortal(<Dialog isOpen purpose={closeDisabled ? "required" : "form"} onOpenChange={open => { if (!open) close(); }} role={alert ? "alertdialog" : "dialog"} aria-labelledby={titleID} aria-describedby={descriptionID} width={wide ? 960 : 680} maxHeight="90dvh">
    <header className="project-dialog-header"><div><h1 id={titleID}>{title}</h1><p id={descriptionID}>{description}</p></div><Button label="关闭" icon={<RemixIcon name="close" />} isIconOnly isDisabled={closeDisabled} data-autofocus onClick={close} /></header>
    <div className="project-dialog-body">{children}</div>{footer && <footer className="project-dialog-footer">{footer}</footer>}
  </Dialog>, document.body);
}

export function NewProjectDialog({ notify }: { notify(message: string): void }) {
  const navigate = useNavigate(); const location = useLocation();
  const draftIdentity = useRef(new URLSearchParams(location.search).get('draft') || localStorage.getItem(DRAFT_KEY + '.active') || requestId('project-draft'));
  const draftKey = `${DRAFT_KEY}.${draftIdentity.current}`;
  useEffect(() => { const query = new URLSearchParams(location.search); if (!query.has('draft')) { query.set('draft', draftIdentity.current); navigate(`${location.pathname}?${query}`, { replace: true }); } localStorage.setItem(DRAFT_KEY + '.active', draftIdentity.current); }, [location.pathname, location.search, navigate]);
 const [step, setStep] = useState(() => Math.min(4, Math.max(1, Number(localStorage.getItem(draftKey + ".step")) || 1))); const [options, setOptions] = useState<FormOptionsPayload | null>(null); const [draft, setDraft] = useState<ProjectDraft>(() => { try { const stored = localStorage.getItem(draftKey); return stored ? { ...emptyDraft(), ...JSON.parse(stored) as ProjectDraft } : emptyDraft(); } catch { return emptyDraft(); } }); const [preview, setPreview] = useState<{ estimated_files: number; warnings: string[] } | null>(null); const [validation, setValidation] = useState<ValidationPayload | null>(null); const [busy, setBusy] = useState(false); const [uncertain, setUncertain] = useState(Boolean(localStorage.getItem(draftKey + ".submission"))); const [error, setError] = useState(""); const submitId = useRef(localStorage.getItem(draftKey + ".operation") || requestId("project-create"));
  useEffect(() => { const controller = new AbortController(); projectApi.formOptions(controller.signal).then(value => { setOptions(value); const query = new URLSearchParams(window.location.search); const selected = query.get("selectedResource"); const purpose = query.get("resourcePurpose"); if (selected && ["asr", "analysis", "review"].includes(purpose || "") && value.resources.some(r => r.resource_id === selected && r.purposes.includes(purpose!))) { setDraft(current => ({ ...current, [`${purpose}Ref`]: selected })); query.delete("selectedResource"); query.delete("resourcePurpose"); navigate({ pathname: location.pathname, search: query.toString() }, { replace: true }); } }).catch((reason) => setError((reason as Error).message)); return () => controller.abort(); }, []);
  useEffect(() => { localStorage.setItem(draftKey, JSON.stringify(draft)); localStorage.setItem(draftKey + ".operation", submitId.current); setValidation(null); }, [draft]);
  useEffect(() => { localStorage.setItem(draftKey + ".step", String(step)); }, [step]);
  useEffect(() => { api<{ project: ProjectSummary | null }>(`/api/projects/operations/${submitId.current}`).then(value => { if (value.project) { localStorage.removeItem(DRAFT_KEY + '.active'); navigate(`/projects/${value.project.project_id}`, { replace: true }); } }).catch(() => setError('暂时无法确认上次是否创建成功，请稍后重试。')); }, [draftKey, navigate]);
  const update = <K extends keyof ProjectDraft>(key: K, value: ProjectDraft[K]) => setDraft((current) => ({ ...current, [key]: value })); const project = options ? { name: draft.name, description: draft.description, config: configFromDraft(draft, options.timezone) } : null;
  const next = async () => { if (step === 2 && draft.sourceDirectory) { try { const result = await projectApi.scanPreview(draft.sourceDirectory, draft.firstScanMode, draft.lookbackDays); setPreview({ estimated_files: result.processable_files, warnings: result.warnings }); } catch (reason) { setError((reason as Error).message); return; } } if (step === 3 && project) { try { setValidation(await projectApi.validate(project, "active")); } catch (reason) { setError((reason as Error).message); return; } } setError(""); setStep((value) => Math.min(4, value + 1)); };
  const submission = useRef(localStorage.getItem(draftKey + ".submission") || "");
  const submit = async (activation: "active" | "inactive") => { if (!project || busy) return; setBusy(true); setUncertain(true); setError(""); try { if (!submission.current) { submission.current = JSON.stringify({ project, activation }); localStorage.setItem(draftKey + ".submission", submission.current); } const original = JSON.parse(submission.current); const result = await projectApi.create(submitId.current, original.project, original.activation); localStorage.removeItem(draftKey + ".submission"); localStorage.removeItem(draftKey); localStorage.removeItem(draftKey + ".step"); localStorage.removeItem(draftKey + ".operation"); localStorage.removeItem(DRAFT_KEY + ".active"); notify(result.initial_scan?.status === "failed" ? `项目已创建，但首次扫描失败：${result.initial_scan.error_summary ?? "请查看项目状态"}` : "项目已创建"); navigate(`/projects/${result.project.project_id}`, { replace: true }); } catch (reason) { const apiError = reason as ApiError; setError(apiError.message); try { const recovered = await api<{ project: ProjectSummary | null }>(`/api/projects/operations/${submitId.current}`); if (recovered.project) { localStorage.removeItem(draftKey + ".submission"); localStorage.removeItem(DRAFT_KEY + '.active'); navigate(`/projects/${recovered.project.project_id}`, { replace: true }); return; } if (apiError instanceof ApiError && !apiError.outcomeUnknown && apiError.code !== 'request_id_conflict') { setUncertain(false); submission.current = ""; localStorage.removeItem(draftKey + ".submission"); submitId.current = requestId('project-create'); localStorage.setItem(draftKey + '.operation', submitId.current); } } catch { setError('暂时无法确认项目是否创建成功，请留在此窗口重试。'); } } finally { setBusy(false); } };
  const blockers = validation?.blockers ?? []; const fatal = validation?.fatal ?? []; const warnings = validation?.warnings ?? []; const migrationBlocked = options?.data_mode === "legacy";
  return <DialogFrame wide closeDisabled={busy || uncertain} title="新建项目" description={`第 ${step} 步，共 4 步 · ${["基本信息", "扫描设置", "模型与成片", "确认项目信息"][step - 1]}`} footer={<><Button isDisabled={step === 1 || busy || uncertain} onClick={() => setStep((value) => value - 1)} label={"上一步"} /><span className="footer-spacer" />{step < 4 ? <Button isDisabled={migrationBlocked || !canContinue(step, draft) || busy} onClick={() => void next()} label={"下一步"} variant="primary" /> : uncertain ? <Button isLoading={busy} onClick={() => void submit("active")} label="核对创建结果" variant="primary" /> : <><Button isDisabled={migrationBlocked || busy || fatal.length > 0} onClick={() => void submit("inactive")} label={"创建但不启用"} /><Button isDisabled={migrationBlocked || busy || fatal.length > 0 || blockers.length > 0} onClick={() => void submit("active")} label={(busy ? "创建中…" : "创建并启用")} variant="primary" /></>}</>}>
    <ol className="stepper">{[1,2,3,4].map((value) => <li className={value === step ? "active" : value < step ? "done" : ""} key={value}><span>{value < step ? <RemixIcon name="check" /> : value}</span></li>)}</ol>{error && <p className="form-error" role="alert">{error}</p>}{migrationBlocked && <p className="form-error" role="alert">请先完成旧数据迁移，再创建项目。</p>}{!options ? <LoadingState /> : <>
      {step === 1 && <FormLayout className="form-surface"><TextInput hasAutoFocus isRequired label="项目名称" onChange={(value) => update("name", value)} value={draft.name} width="100%" /><Field inputID="new-project-description" label="项目描述（选填）" width="100%"><textarea className="form-control" id="new-project-description" rows={4} value={draft.description} onChange={(event) => update("description", event.target.value)} /></Field><ProjectPathField label="录像文件夹" value={draft.sourceDirectory} onChange={(value) => update("sourceDirectory", value)} /></FormLayout>}
      {step === 2 && <FormLayout className="form-surface"><Selector label="已有录像怎么处理" onChange={(value) => update("firstScanMode", value as ProjectDraft["firstScanMode"])} options={[{ value: "new_only", label: "只处理项目创建后新增的录像" }, { value: "recent", label: "同时处理最近的录像" }, { value: "choose_existing", label: "创建后手动选择已有录像" }]} value={draft.firstScanMode} width="100%" />{draft.firstScanMode === "recent" && <Selector label="处理最近几天的录像" onChange={(value) => update("lookbackDays", Number(value) as ProjectDraft["lookbackDays"])} options={options.lookback_days.map((days) => ({ value: String(days), label: `最近 ${days} 天` }))} value={String(draft.lookbackDays)} width="100%" />}<CheckboxInput label="定时扫描" description="关闭后可手动扫描" aria-label="定时扫描" value={draft.scheduleEnabled} onChange={value => update("scheduleEnabled", value)}  />{draft.scheduleEnabled && <FormLayout className="form-subgroup"><Selector label="定时方式" onChange={(value) => update("scheduleMode", value as ProjectDraft["scheduleMode"])} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "固定间隔" }]} value={draft.scheduleMode} width="100%" />{draft.scheduleMode === "daily" ? <Field inputID="new-project-daily-time" label="扫描时间" width="100%"><input className="form-control" id="new-project-daily-time" type="time" value={draft.dailyTime} onChange={(event) => update("dailyTime", event.target.value)} /></Field> : <Selector label="扫描间隔" onChange={(value) => update("intervalMinutes", Number(value) as ProjectDraft["intervalMinutes"])} options={options.interval_minutes.map((minutes) => ({ value: String(minutes), label: minutes < 60 ? `每 ${minutes} 分钟` : `每 ${minutes / 60} 小时` }))} value={String(draft.intervalMinutes)} width="100%" />}</FormLayout>}{preview && <div className="preview-card"><strong>预计有 {preview.estimated_files} 个录像可处理</strong>{preview.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}</FormLayout>}
      {step === 3 && <FormLayout className="form-surface"><ResourceAssignment draft={draft} options={options} change={setDraft} /><ProjectPathField label="成片保存位置" value={draft.outputDirectory} onChange={(value) => update("outputDirectory", value)} /><Selector label="临时文件清理提醒" onChange={(value) => update("retention", value as ProjectDraft["retention"])} options={retentionOptions} value={draft.retention} width="100%" /><p className="retention-note">AI 会挑选片段并生成发布文案。原始录像和成片不会自动删除。</p></FormLayout>}
      {step === 4 && <div className="summary-step"><IssueGroup title="以下问题会阻止创建" tone="error" issues={fatal} /><IssueGroup title="启用前请解决这些问题" tone="warning" issues={blockers} /><IssueGroup title="提醒" tone="info" issues={warnings} />{!fatal.length && !blockers.length && <div className="readiness success"><StatusPill status="idle" label="检查通过" /><div><p>启用后会按所选范围和扫描方式处理录像。</p></div></div>}<dl className="summary-list"><div><dt>项目</dt><dd>{draft.name}</dd><small>{draft.sourceDirectory}</small></div><div><dt>扫描方式</dt><dd>{draft.scheduleEnabled ? "定时扫描（也可手动）" : "手动扫描"}</dd><small>{draft.firstScanMode === "recent" ? `包含最近 ${draft.lookbackDays} 天的录像` : draft.firstScanMode === "choose_existing" ? "创建后选择已有录像" : "只处理项目创建后新增的录像"}</small></div><div><dt>成片保存位置</dt><dd>{draft.outputDirectory}</dd><small>原始录像不会自动删除</small></div></dl></div>}
    </>}</DialogFrame>;
}

function canContinue(step: number, draft: ProjectDraft) { if (step === 1) return Boolean(draft.name.trim() && draft.sourceDirectory.trim()); if (step === 3) return Boolean(draft.outputDirectory.trim()); return true; }
const retentionOptions = [{ value: "remind_immediately", label: "处理完成后提醒" }, { value: "remind_after_7_days", label: "处理完成 7 天后提醒" }, { value: "keep", label: "不提醒，保留文件" }];
function ProjectPathField({ label, value, onChange }: { label: string; value: string; onChange(value: string): void }) { const shell = window.liveClipperShell?.selectFolder; const choose = shell ? async () => { const selected = await shell(`选择${label}`); if (selected) onChange(selected); } : undefined; return <PathField choose={choose} label={label} onChange={onChange} value={value} />; }
function IssueGroup({ title, tone, issues }: { title: string; tone: string; issues: Array<{ field: string; message: string }> }) { if (!issues.length) return null; return <div className={`issue-group ${tone}`} role={tone === "error" ? "alert" : undefined}><strong>{title}</strong>{issues.map((issue) => <p key={`${issue.field}-${issue.message}`}>{issue.message}</p>)}</div>; }

function ProjectChanges({ latest, draft, options }: { latest: ProjectSummary; draft: ProjectDraft; options: FormOptionsPayload | null }) {
  const original = draftFromProject(latest);
  const labels: Record<keyof ProjectDraft, string> = { name: '项目名称', description: '项目描述', sourceDirectory: '录像文件夹', outputDirectory: '成片保存位置', firstScanMode: '已有录像怎么处理', lookbackDays: '回溯天数', scheduleEnabled: '定时扫描', scheduleMode: '定时方式', dailyTime: '扫描时间', intervalMinutes: '扫描间隔（分钟）', asrRef: '语音识别', analysisRef: '内容分析', reviewRef: '片段筛选', retention: '临时文件清理提醒' };
  const value = (key: keyof ProjectDraft, item: ProjectDraft[keyof ProjectDraft]) => {
    if (['asrRef','analysisRef','reviewRef'].includes(key)) return item === 'reuse_analysis' ? '与内容分析使用同一模型' : !item ? '未选择' : options?.resources.find(r => r.resource_id === item)?.display_name || '模型名称未能读取或模型已不可用';
    if (key === 'retention') return retentionOptions.find(v => v.value === item)?.label || '未识别的提醒方式';
    if (key === 'scheduleMode') return item === 'daily' ? '每天固定时间' : '固定间隔';
    if (key === 'firstScanMode') return ({ new_only: '只处理新录像', recent: '处理近期录像', choose_existing: '手动选择已有录像' } as Record<string,string>)[String(item)] || '未识别的扫描方式';
    if (typeof item === 'boolean') return item ? '开启' : '关闭';
    return String(item) || '未填写';
  };
  const changed = (Object.keys(labels) as Array<keyof ProjectDraft>).filter(key => original[key] !== draft[key]);
  return <dl className="summary-list">{changed.map(key => <div key={key}><dt>{labels[key]}</dt><dd>最新设置：{value(key, original[key])} → 本次修改：{value(key, draft[key])}</dd></div>)}</dl>;
}

export function ProjectSettingsDialog({ project, onSaved }: { project: ProjectSummary; onSaved(): Promise<void> }) {
  const navigate = useNavigate(); const location = useLocation(); const [draft, setDraft] = useState(() => { try { const stored = localStorage.getItem(`venus.project-draft.${project.project_id}`); return stored ? JSON.parse(stored).draft as ProjectDraft : draftFromProject(project); } catch { return draftFromProject(project); } }); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const id = useRef<string>((() => { try { return JSON.parse(localStorage.getItem(`venus.project-draft.${project.project_id}`) || "null")?.operation || requestId("project-update"); } catch { return requestId("project-update"); } })()); const [latest, setLatest] = useState(project); const submitted = useRef((() => { try { return JSON.parse(localStorage.getItem(`venus.project-draft.${project.project_id}`) || "null")?.submitted || ""; } catch { return ""; } })()); const draftRef = useRef(draft); draftRef.current = draft; const config = project.config!.config; const returnTo = safeRunReturn(new URLSearchParams(location.search).get("returnTo"));
  const [options, setOptions] = useState<FormOptionsPayload | null>(null);
  const baseRevision = useRef<number>((() => { try { return JSON.parse(localStorage.getItem(`venus.project-draft.${project.project_id}`) || "null")?.revision ?? project.current_config_revision; } catch { return project.current_config_revision; } })());
  const persistDraft = () => localStorage.setItem(`venus.project-draft.${project.project_id}`, JSON.stringify({ draft: draftRef.current, revision: baseRevision.current, operation: id.current, submitted: submitted.current }));
  useEffect(() => { persistDraft(); }, [draft, project.project_id]);
  useEffect(() => { projectApi.formOptions().then(value => { setOptions(value); const query = new URLSearchParams(location.search); const selected = query.get("selectedResource"); const purpose = query.get("resourcePurpose"); if (selected && value.resources.some(r => r.resource_id === selected && r.purposes.includes(purpose || ""))) { setDraft(current => ({ ...current, [`${purpose}Ref`]: selected })); query.delete("selectedResource"); query.delete("resourcePurpose"); navigate({ pathname: location.pathname, search: query.toString() }, { replace: true }); } }).catch(e => setError(e.message)); }, [location.search]);
  const resourceReturn = new URLSearchParams(location.search).get("resourceReturn");
  const close = () => navigate(resourceReturn && /^[a-zA-Z0-9_-]+$/.test(resourceReturn) ? `/resources/${resourceReturn}` : returnTo ?? `/projects/${project.project_id}`, { replace: true });
  const finish = async (result: ProjectSummary) => {
    const original = submitted.current ? JSON.parse(submitted.current).draft : null;
    const changed = JSON.stringify(original) !== JSON.stringify(draftRef.current);
    submitted.current = ''; baseRevision.current = result.current_config_revision; id.current = requestId('project-update');
    setLatest(result);
    if (changed) { persistDraft(); setError('上次提交已保存。当前草稿中的新修改仍保留，请核对后再保存。'); return; }
    localStorage.removeItem(`venus.project-draft.${project.project_id}`); await onSaved(); close();
  };
  const save = async () => {
    if (busy || !submitted.current && baseRevision.current !== latest.current_config_revision) return;
    setBusy(true); setError(''); let sent = false;
    try {
      const original = await api<{ project: ProjectSummary | null }>(`/api/projects/${project.project_id}/operations/${id.current}`);
      if (original.project) { await finish(original.project); return; }
      if (!submitted.current) {
        submitted.current = JSON.stringify({ payload: { name: draft.name, description: draft.description, config: configFromDraft(draft, config.schedule.timezone) }, revision: baseRevision.current, draft });
        persistDraft();
      }
      const attempt = JSON.parse(submitted.current); sent = true;
      const result = await projectApi.update(project.project_id, id.current, attempt.revision, attempt.payload);
      await finish(result.project);
    } catch (reason) {
      if (sent && reason instanceof ApiError && !reason.outcomeUnknown && reason.code !== 'request_id_conflict') {
        submitted.current = ''; id.current = requestId('project-update'); persistDraft();
        if (reason.code === 'revision_conflict') { try { setLatest((await projectApi.project(project.project_id)).project); } catch { /* Preserve draft until latest revision can be read. */ } }
      }
      setError(submitted.current ? '暂时无法确认保存结果，请核对原操作。你的新修改仍保留在草稿中。' : (reason as Error).message);
    } finally { setBusy(false); }
  };

  return <DialogFrame closeDisabled={busy || !!submitted.current} onClose={close} title="项目设置" description="修改后的设置用于后续扫描和新建的剪辑记录。" footer={<><Button isDisabled={busy || !!submitted.current} onClick={close} label={"取消"} /><span className="footer-spacer" /><Button isLoading={busy} isDisabled={busy || !submitted.current && (!draft.name.trim() || baseRevision.current !== latest.current_config_revision)} onClick={() => void save()} label={(busy ? "保存中…" : submitted.current ? "核对保存结果" : "保存设置")} variant="primary" /></>}><FormLayout className="form-surface">{error && <p className="form-error" role="alert">{error}</p>}{baseRevision.current !== latest.current_config_revision && <section role="alert"><p>项目设置已被其他操作更新。请核对最新设置，再决定保留哪份内容。</p><ProjectChanges latest={latest} draft={draft} options={options} /><Button onClick={() => { baseRevision.current = latest.current_config_revision; id.current = requestId('project-update'); submitted.current = ''; setDraft({ ...draft }); setError('已保留你的修改。保存后将以当前表单内容为准。'); }} label={"保留我的修改，继续编辑"} /><Button onClick={() => { baseRevision.current = latest.current_config_revision; id.current = requestId('project-update'); submitted.current = ''; setDraft(draftFromProject(latest)); setError(''); }} label={"放弃我的修改，使用最新设置"} /></section>}{(!draft.asrRef || !draft.analysisRef || !draft.reviewRef) && <p>还未选齐处理所需的模型或工具，保存后项目暂时无法处理新录像。</p>}<TextInput label="项目名称" onChange={(value) => setDraft({ ...draft, name: value })} value={draft.name} width="100%" /><Field inputID="project-settings-description" label="项目描述（选填）" width="100%"><textarea className="form-control" id="project-settings-description" rows={4} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></Field><ProjectPathField label="录像文件夹" value={draft.sourceDirectory} onChange={(value) => setDraft({ ...draft, sourceDirectory: value })} />{options && <ResourceAssignment disabled={busy || !!submitted.current} draft={draft} options={options} change={setDraft} projectId={project.project_id} />}<ProjectPathField label="成片保存位置" value={draft.outputDirectory} onChange={(value) => setDraft({ ...draft, outputDirectory: value })} /><Selector label="临时文件清理提醒" onChange={(value) => setDraft({ ...draft, retention: value as ProjectDraft["retention"] })} options={retentionOptions} value={draft.retention} width="100%" /><CheckboxInput label="定时扫描" description="关闭后可手动扫描" aria-label="定时扫描" value={draft.scheduleEnabled} onChange={value => setDraft({ ...draft, scheduleEnabled: value })}  />{draft.scheduleEnabled && <FormLayout className="form-subgroup"><Selector label="定时方式" onChange={(value) => setDraft({ ...draft, scheduleMode: value as ProjectDraft["scheduleMode"] })} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "固定间隔" }]} value={draft.scheduleMode} width="100%" />{draft.scheduleMode === "daily" ? <Field inputID="project-settings-daily-time" label="扫描时间" width="100%"><input className="form-control" id="project-settings-daily-time" type="time" value={draft.dailyTime} onChange={(event) => setDraft({ ...draft, dailyTime: event.target.value })} /></Field> : <Selector label="扫描间隔" onChange={(value) => setDraft({ ...draft, intervalMinutes: Number(value) as ProjectDraft["intervalMinutes"] })} options={[30, 60, 180, 360, 720].map((minutes) => ({ value: String(minutes), label: minutes < 60 ? `每 ${minutes} 分钟` : `每 ${minutes / 60} 小时` }))} value={String(draft.intervalMinutes)} width="100%" />}</FormLayout>}</FormLayout></DialogFrame>;
}

export function LatestScanDialog({ project }: { project: ProjectSummary }) {
  const state = usePolling((signal) => projectApi.latestScan(project.project_id, signal), project.latest_scan?.status === "running" ? 5000 : 15000); const scan = state.data ? state.data.scan : project.latest_scan;
  return (
    <DialogFrame title="最近扫描结果" description={`${project.name} · 已有剪辑记录的录像会跳过`}>
      {state.error && <p role="alert" className="form-error">{state.error}</p>}
      {!scan ? state.loading ? <LoadingState /> : state.error ? null : <p className="quiet-state">还没有扫描记录</p> : (
        <>
          <div className={`readiness ${statusTone(scan.status)}`}>
            <StatusPill status={scan.status} context="scan" />
            <div><strong>{scanMessage(scan)}</strong><p>{scan.trigger_source === "scheduled" ? "定时扫描" : "手动扫描"} · {time(scan.completed_at ?? scan.started_at)}</p></div>
          </div>
          <div className="scan-stats"><Metric label="新增记录" value={scan.created_count ?? 0} /><Metric label="已有记录" value={scan.duplicate_count ?? 0} /><Metric label="需稍后扫描" value={scan.unstable_count ?? 0} /><Metric label="格式不支持" value={scan.unsupported_count ?? 0} /><Metric label="不在扫描范围" value={scan.excluded_count ?? 0} /><Metric label="扫描出错" value={scan.failed_count ?? 0} tone="error" /></div>
          {(scan.unstable_count ?? 0) > 0 && <p>文件刚有更新，请稍后再扫描。</p>}
          {scan.error_summary && <p className="form-error" role="alert">{scan.error_summary}</p>}
        </>
      )}
    </DialogFrame>
  );
}

export function ChooseRecordingsDialog({ project, onScanned }: { project: ProjectSummary; onScanned(scan: ScanEvent): Promise<void> }) {
  const navigate = useNavigate(); const [files, setFiles] = useState<SourceFile[]>([]); const [selected, setSelected] = useState<string[]>([]); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const id = useRef(requestId("scan-selected")); const submission = useRef<string[] | null>(null); const [loaded, setLoaded] = useState(false);
  useEffect(() => { const controller = new AbortController(); projectApi.sourceFiles(project.project_id, controller.signal).then((result) => { setFiles(result.files); setLoaded(true); }).catch((reason) => { if (!controller.signal.aborted) { setError((reason as Error).message); setLoaded(true); } }); return () => controller.abort(); }, [project.project_id]);
  const scan = async () => { if (!selected.length || busy) return; setBusy(true); try { submission.current ??= [...selected]; const result = await projectApi.scan(project.project_id, id.current, "selected", submission.current); await onScanned(result.scan); navigate(`/projects/${project.project_id}`, { replace: true }); } catch (reason) { if (reason instanceof ApiError && !reason.outcomeUnknown) { submission.current = null; id.current = requestId("scan-selected"); } setError(submission.current ? "暂时无法确认扫描结果，请核对原操作。" : (reason as Error).message); } finally { setBusy(false); } };
  return <DialogFrame wide closeDisabled={busy || !!submission.current} title="选择已有录像" description="勾选要处理的录像。扫描后，可处理的录像会加入队列，已有记录的会跳过。使用云端模型可能产生调用费用。" footer={<><span>已选 {selected.length} 个录像</span><span className="footer-spacer" /><Button isLoading={busy} isDisabled={!selected.length || busy} onClick={() => void scan()} label={(busy ? "扫描中…" : submission.current ? "核对扫描结果" : "扫描并处理")} variant="primary" /></>}>{error && <p className="form-error" role="alert">{error}</p>}<div className="source-files">{files.map((file) => <label className={!file.selectable ? "disabled" : ""} key={file.relative_path}><input type="checkbox" disabled={!file.selectable || busy || !!submission.current} checked={selected.includes(file.relative_path)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, file.relative_path] : current.filter((item) => item !== file.relative_path))} /><span><strong>{file.relative_path}</strong><small>{formatBytes(file.bytes)} · {time(file.modified_at)}{file.reason ? ` · ${file.reason === "unsupported" ? "格式不支持" : file.reason}` : ""}</small></span></label>)}{!loaded && <LoadingState />}{loaded && !files.length && !error && <p>没有找到录像</p>}</div></DialogFrame>;
}

export function PauseProjectDialog({ projectName, onClose, onConfirm }: { projectName: string; onClose(): void; onConfirm(): Promise<boolean> }) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => { if (busy) return; setBusy(true); const succeeded = await onConfirm(); setBusy(false); if (succeeded) onClose(); };
  return <DialogFrame alert closeDisabled={busy} title="暂停项目" description={`暂停“${projectName}”的自动扫描？`} onClose={onClose} footer={<><Button isDisabled={busy} onClick={onClose} label={"取消"} /><span className="footer-spacer" /><Button isDisabled={busy} onClick={() => void confirm()} label={(busy ? "暂停中…" : "暂停自动扫描")} variant="primary" /></>}><p>暂停后，将停止自动扫描。正在处理和排队的录像会继续处理，你仍可手动扫描。</p></DialogFrame>;
}
