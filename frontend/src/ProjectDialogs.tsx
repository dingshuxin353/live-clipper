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
 const [step, setStep] = useState(() => Math.min(4, Math.max(1, Number(localStorage.getItem(draftKey + ".step")) || 1))); const [options, setOptions] = useState<FormOptionsPayload | null>(null); const [draft, setDraft] = useState<ProjectDraft>(() => { try { const stored = localStorage.getItem(draftKey); return stored ? { ...emptyDraft(), ...JSON.parse(stored) as ProjectDraft } : emptyDraft(); } catch { return emptyDraft(); } }); const [preview, setPreview] = useState<{ estimated_files: number; warnings: string[] } | null>(null); const [validation, setValidation] = useState<ValidationPayload | null>(null); const [busy, setBusy] = useState(false); const [uncertain, setUncertain] = useState(false); const [error, setError] = useState(""); const submitId = useRef(localStorage.getItem(draftKey + ".operation") || requestId("project-create"));
  useEffect(() => { const controller = new AbortController(); projectApi.formOptions(controller.signal).then(value => { setOptions(value); const query = new URLSearchParams(window.location.search); const selected = query.get("selectedResource"); const purpose = query.get("resourcePurpose"); if (selected && ["asr", "analysis", "review"].includes(purpose || "") && value.resources.some(r => r.resource_id === selected && r.purposes.includes(purpose!))) { setDraft(current => ({ ...current, [`${purpose}Ref`]: selected })); query.delete("selectedResource"); query.delete("resourcePurpose"); navigate({ pathname: location.pathname, search: query.toString() }, { replace: true }); } }).catch((reason) => setError((reason as Error).message)); return () => controller.abort(); }, []);
  useEffect(() => { localStorage.setItem(draftKey, JSON.stringify(draft)); localStorage.setItem(draftKey + ".operation", submitId.current); setValidation(null); }, [draft]);
  useEffect(() => { localStorage.setItem(draftKey + ".step", String(step)); }, [step]);
  useEffect(() => { api<{ project: ProjectSummary | null }>(`/api/projects/operations/${submitId.current}`).then(value => { if (value.project) { localStorage.removeItem(DRAFT_KEY + '.active'); navigate(`/projects/${value.project.project_id}`, { replace: true }); } }).catch(() => setError('暂时无法确认上次创建结果，请稍后重试。')); }, [draftKey, navigate]);
  const update = <K extends keyof ProjectDraft>(key: K, value: ProjectDraft[K]) => setDraft((current) => ({ ...current, [key]: value })); const project = options ? { name: draft.name, description: draft.description, config: configFromDraft(draft, options.timezone) } : null;
  const next = async () => { if (step === 2 && draft.sourceDirectory) { try { const result = await projectApi.scanPreview(draft.sourceDirectory, draft.firstScanMode, draft.lookbackDays); setPreview({ estimated_files: result.processable_files, warnings: result.warnings }); } catch (reason) { setError((reason as Error).message); return; } } if (step === 3 && project) { try { setValidation(await projectApi.validate(project, "active")); } catch (reason) { setError((reason as Error).message); return; } } setError(""); setStep((value) => Math.min(4, value + 1)); };
  const submit = async (activation: "active" | "inactive") => { if (!project || busy) return; setBusy(true); setUncertain(true); setError(""); try { const result = await projectApi.create(submitId.current, project, activation); localStorage.removeItem(draftKey); localStorage.removeItem(draftKey + ".step"); localStorage.removeItem(draftKey + ".operation"); localStorage.removeItem(DRAFT_KEY + ".active"); notify(result.initial_scan?.status === "failed" ? `项目已创建，但首次扫描失败：${result.initial_scan.error_summary ?? "请查看项目状态"}` : "项目已创建"); navigate(`/projects/${result.project.project_id}`, { replace: true }); } catch (reason) { const apiError = reason as ApiError; setError(apiError.message); try { const recovered = await api<{ project: ProjectSummary | null }>(`/api/projects/operations/${submitId.current}`); if (recovered.project) { localStorage.removeItem(DRAFT_KEY + '.active'); navigate(`/projects/${recovered.project.project_id}`, { replace: true }); return; } if (apiError.status >= 400 && apiError.status < 500 && apiError.status !== 408 && !['network_error', 'request_id_conflict', 'invalid_response'].includes(apiError.code)) { setUncertain(false); submitId.current = requestId('project-create'); localStorage.setItem(draftKey + '.operation', submitId.current); } } catch { setError('创建结果暂时无法确认，请保留当前草稿并重试原操作。'); } } finally { setBusy(false); } };
  const blockers = validation?.blockers ?? []; const fatal = validation?.fatal ?? []; const warnings = validation?.warnings ?? []; const migrationBlocked = options?.data_mode === "legacy";
  return <DialogFrame wide closeDisabled={busy || uncertain} title="新建项目" description={`第 ${step} 步，共 4 步 · ${["基本信息", "扫描设置", "处理与输出", "确认配置"][step - 1]}`} footer={<><Button isDisabled={step === 1 || busy || uncertain} onClick={() => setStep((value) => value - 1)} label={"上一步"} /><span className="footer-spacer" />{step < 4 ? <Button isDisabled={migrationBlocked || !canContinue(step, draft) || busy} onClick={() => void next()} label={"下一步"} variant="primary" /> : <><Button isDisabled={migrationBlocked || busy || fatal.length > 0} onClick={() => void submit("inactive")} label={"保存为未启用"} /><Button isDisabled={migrationBlocked || busy || fatal.length > 0 || blockers.length > 0} onClick={() => void submit("active")} label={(busy ? "创建中…" : "创建并启用")} variant="primary" /></>}</>}>
    <ol className="stepper">{[1,2,3,4].map((value) => <li className={value === step ? "active" : value < step ? "done" : ""} key={value}><span>{value < step ? <RemixIcon name="check" /> : value}</span></li>)}</ol>{error && <p className="form-error" role="alert">{error}</p>}{migrationBlocked && <p className="form-error" role="alert">旧版数据尚未完成迁移确认，暂时不能创建项目。</p>}{!options ? <LoadingState /> : <>
      {step === 1 && <FormLayout className="form-surface"><TextInput hasAutoFocus isRequired label="项目名称" onChange={(value) => update("name", value)} value={draft.name} width="100%" /><Field inputID="new-project-description" label="项目描述" width="100%"><textarea className="form-control" id="new-project-description" rows={4} value={draft.description} onChange={(event) => update("description", event.target.value)} /></Field><ProjectPathField label="录像目录" value={draft.sourceDirectory} onChange={(value) => update("sourceDirectory", value)} /></FormLayout>}
      {step === 2 && <FormLayout className="form-surface"><Selector label="首次扫描" onChange={(value) => update("firstScanMode", value as ProjectDraft["firstScanMode"])} options={[{ value: "new_only", label: "只处理创建后新增录像" }, { value: "recent", label: "回溯最近录像" }, { value: "choose_existing", label: "创建后手动选择已有录像" }]} value={draft.firstScanMode} width="100%" />{draft.firstScanMode === "recent" && <Selector label="回溯范围" onChange={(value) => update("lookbackDays", Number(value) as ProjectDraft["lookbackDays"])} options={options.lookback_days.map((days) => ({ value: String(days), label: `最近 ${days} 天` }))} value={String(draft.lookbackDays)} width="100%" />}<CheckboxInput label="定时扫描" description="关闭时仍可随时手动扫描" aria-label="定时扫描" value={draft.scheduleEnabled} onChange={value => update("scheduleEnabled", value)}  />{draft.scheduleEnabled && <FormLayout className="form-subgroup"><Selector label="扫描频率" onChange={(value) => update("scheduleMode", value as ProjectDraft["scheduleMode"])} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "按时间间隔" }]} value={draft.scheduleMode} width="100%" />{draft.scheduleMode === "daily" ? <Field inputID="new-project-daily-time" label="每天时间" width="100%"><input className="form-control" id="new-project-daily-time" type="time" value={draft.dailyTime} onChange={(event) => update("dailyTime", event.target.value)} /></Field> : <Selector label="扫描间隔" onChange={(value) => update("intervalMinutes", Number(value) as ProjectDraft["intervalMinutes"])} options={options.interval_minutes.map((minutes) => ({ value: String(minutes), label: `每 ${minutes} 分钟` }))} value={String(draft.intervalMinutes)} width="100%" />}</FormLayout>}{preview && <div className="preview-card"><strong>预计可创建 {preview.estimated_files} 条剪辑记录</strong>{preview.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}</FormLayout>}
      {step === 3 && <FormLayout className="form-surface"><ResourceAssignment draft={draft} options={options} change={setDraft} /><ProjectPathField label="成片输出目录" value={draft.outputDirectory} onChange={(value) => update("outputDirectory", value)} /><Selector label="中间产物保留" onChange={(value) => update("retention", value as ProjectDraft["retention"])} options={retentionOptions} value={draft.retention} width="100%" /><p className="retention-note">AI 会判断候选片段并生成发布物料。原始录像不会自动删除，成片会保留。</p></FormLayout>}
      {step === 4 && <div className="summary-step"><IssueGroup title="必须修正" tone="error" issues={fatal} /><IssueGroup title="启用前需处理" tone="warning" issues={blockers} /><IssueGroup title="提醒" tone="info" issues={warnings} />{!fatal.length && !blockers.length && <div className="readiness success"><StatusPill status="idle" label="可以运行" /><div><strong>项目可以启用</strong><p>启用后会按当前设置扫描录像。</p></div></div>}<dl className="summary-list"><div><dt>项目</dt><dd>{draft.name}</dd><small>{draft.sourceDirectory}</small></div><div><dt>扫描</dt><dd>{draft.scheduleEnabled ? "定时 + 手动" : "仅手动扫描"}</dd><small>{draft.firstScanMode === "recent" ? `首次回溯 ${draft.lookbackDays} 天` : draft.firstScanMode === "choose_existing" ? "创建后选择已有录像" : "仅处理新录像"}</small></div><div><dt>输出</dt><dd>{draft.outputDirectory}</dd><small>原始录像永不自动删除</small></div></dl></div>}
    </>}</DialogFrame>;
}

function canContinue(step: number, draft: ProjectDraft) { if (step === 1) return Boolean(draft.name.trim() && draft.sourceDirectory.trim()); if (step === 3) return Boolean(draft.outputDirectory.trim()); return true; }
const retentionOptions = [{ value: "remind_immediately", label: "完成后立即提醒清理" }, { value: "remind_after_7_days", label: "完成 7 天后提醒清理" }, { value: "keep", label: "始终保留" }];
function ProjectPathField({ label, value, onChange }: { label: string; value: string; onChange(value: string): void }) { const shell = window.liveClipperShell?.selectFolder; const choose = shell ? async () => { const selected = await shell(`选择${label}`); if (selected) onChange(selected); } : undefined; return <PathField choose={choose} label={label} onChange={onChange} value={value} />; }
function IssueGroup({ title, tone, issues }: { title: string; tone: string; issues: Array<{ field: string; message: string }> }) { if (!issues.length) return null; return <div className={`issue-group ${tone}`} role={tone === "error" ? "alert" : undefined}><strong>{title}</strong>{issues.map((issue) => <p key={`${issue.field}-${issue.message}`}>{issue.message}</p>)}</div>; }

export function ProjectSettingsDialog({ project, onSaved }: { project: ProjectSummary; onSaved(): Promise<void> }) {
  const navigate = useNavigate(); const location = useLocation(); const [draft, setDraft] = useState(() => { try { const stored = localStorage.getItem(`venus.project-draft.${project.project_id}`); return stored ? JSON.parse(stored).draft as ProjectDraft : draftFromProject(project); } catch { return draftFromProject(project); } }); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const id = useRef<string>((() => { try { return JSON.parse(localStorage.getItem(`venus.project-draft.${project.project_id}`) || "null")?.operation || requestId("project-update"); } catch { return requestId("project-update"); } })()); const [latest, setLatest] = useState(project); const submitted = useRef(""); const config = project.config!.config; const returnTo = safeRunReturn(new URLSearchParams(location.search).get("returnTo"));
  const [options, setOptions] = useState<FormOptionsPayload | null>(null);
  const baseRevision = useRef<number>((() => { try { return JSON.parse(localStorage.getItem(`venus.project-draft.${project.project_id}`) || "null")?.revision ?? project.current_config_revision; } catch { return project.current_config_revision; } })());
  useEffect(() => { localStorage.setItem(`venus.project-draft.${project.project_id}`, JSON.stringify({ draft, revision: baseRevision.current, operation: id.current })); }, [draft, project.project_id]);
  useEffect(() => { projectApi.formOptions().then(value => { setOptions(value); const query = new URLSearchParams(location.search); const selected = query.get("selectedResource"); const purpose = query.get("resourcePurpose"); if (selected && value.resources.some(r => r.resource_id === selected && r.purposes.includes(purpose || ""))) { setDraft(current => ({ ...current, [`${purpose}Ref`]: selected })); query.delete("selectedResource"); query.delete("resourcePurpose"); navigate({ pathname: location.pathname, search: query.toString() }, { replace: true }); } }).catch(e => setError(e.message)); }, [location.search]);
  const resourceReturn = new URLSearchParams(location.search).get("resourceReturn");
  const close = () => navigate(resourceReturn && /^[a-zA-Z0-9_-]+$/.test(resourceReturn) ? `/resources/${resourceReturn}` : returnTo ?? `/projects/${project.project_id}`, { replace: true });
  const finish = async () => { localStorage.removeItem(`venus.project-draft.${project.project_id}`); await onSaved(); close(); };
  const save = async () => {
    if (busy) return;
    setBusy(true); setError('');
    const payload = { name: draft.name, description: draft.description, config: configFromDraft(draft, config.schedule.timezone) };
    const encoded = JSON.stringify(payload);
    try {
      const original = await api<{ project: ProjectSummary | null }>(`/api/projects/${project.project_id}/operations/${id.current}`);
      if (original.project) { await finish(); return; }
      if (submitted.current && submitted.current !== encoded) { setError('上次保存结果尚未确认，请恢复原草稿后重试。'); return; }
      submitted.current = encoded;
      await projectApi.update(project.project_id, id.current, baseRevision.current, payload); await finish();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && reason.status !== 408 && reason.code !== 'invalid_response') {
        submitted.current = ''; id.current = requestId('project-update');
        localStorage.setItem(`venus.project-draft.${project.project_id}`, JSON.stringify({ draft, revision: baseRevision.current, operation: id.current }));
        if (reason.code === 'revision_conflict') { try { setLatest((await projectApi.project(project.project_id)).project); } catch { /* Preserve draft until latest revision can be read. */ } }
      }
      setError((reason as Error).message);
    } finally { setBusy(false); }
  };

  return <DialogFrame closeDisabled={busy || !!submitted.current} onClose={close} title="项目设置" description="更改仅影响后续扫描和新建的剪辑记录。" footer={<><Button isDisabled={busy || !!submitted.current} onClick={close} label={"取消"} /><span className="footer-spacer" /><Button isLoading={busy} isDisabled={busy || !draft.name.trim()} onClick={() => void save()} label={(busy ? "保存中…" : !draft.asrRef || !draft.analysisRef || !draft.reviewRef ? "保存并等待配置" : "保存项目设置")} variant="primary" /></>}><FormLayout className="form-surface">{error && <p className="form-error" role="alert">{error}</p>}{baseRevision.current !== latest.current_config_revision && <section role="alert"><p>项目已在其他位置修改。请比较当前设置与草稿后再保存。</p><dl><dt>当前项目</dt><dd>{latest.name} · {latest.config?.config.source.directory}</dd><dt>当前资源</dt><dd>{JSON.stringify(latest.config?.config.resources)}</dd><dt>草稿资源</dt><dd>{draft.asrRef} · {draft.analysisRef} · {draft.reviewRef}</dd></dl><Button onClick={() => { baseRevision.current = latest.current_config_revision; id.current = requestId('project-update'); submitted.current = ''; setDraft({ ...draft }); setError('已采用最新修订作为比较基线，请确认草稿后保存。'); }} label={"已比较，继续编辑草稿"} /><Button onClick={() => { baseRevision.current = latest.current_config_revision; id.current = requestId('project-update'); submitted.current = ''; setDraft(draftFromProject(latest)); setError(''); }} label={"使用当前项目设置"} /></section>}<TextInput label="项目名称" onChange={(value) => setDraft({ ...draft, name: value })} value={draft.name} width="100%" /><Field inputID="project-settings-description" label="项目描述" width="100%"><textarea className="form-control" id="project-settings-description" rows={4} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></Field><ProjectPathField label="录像目录" value={draft.sourceDirectory} onChange={(value) => setDraft({ ...draft, sourceDirectory: value })} />{options && <ResourceAssignment disabled={busy || !!submitted.current} draft={draft} options={options} change={setDraft} projectId={project.project_id} />}<ProjectPathField label="输出目录" value={draft.outputDirectory} onChange={(value) => setDraft({ ...draft, outputDirectory: value })} /><Selector label="中间产物保留" onChange={(value) => setDraft({ ...draft, retention: value as ProjectDraft["retention"] })} options={retentionOptions} value={draft.retention} width="100%" /><CheckboxInput label="定时扫描" description="手动扫描始终可用" aria-label="定时扫描" value={draft.scheduleEnabled} onChange={value => setDraft({ ...draft, scheduleEnabled: value })}  />{draft.scheduleEnabled && <FormLayout className="form-subgroup"><Selector label="频率" onChange={(value) => setDraft({ ...draft, scheduleMode: value as ProjectDraft["scheduleMode"] })} options={[{ value: "daily", label: "每天固定时间" }, { value: "interval", label: "按时间间隔" }]} value={draft.scheduleMode} width="100%" />{draft.scheduleMode === "daily" ? <Field inputID="project-settings-daily-time" label="每天时间" width="100%"><input className="form-control" id="project-settings-daily-time" type="time" value={draft.dailyTime} onChange={(event) => setDraft({ ...draft, dailyTime: event.target.value })} /></Field> : <Selector label="扫描间隔" onChange={(value) => setDraft({ ...draft, intervalMinutes: Number(value) as ProjectDraft["intervalMinutes"] })} options={[30, 60, 180, 360, 720].map((minutes) => ({ value: String(minutes), label: minutes === 60 ? "每 1 小时" : `每 ${minutes} 分钟` }))} value={String(draft.intervalMinutes)} width="100%" />}</FormLayout>}</FormLayout></DialogFrame>;
}

export function LatestScanDialog({ project }: { project: ProjectSummary }) {
  const state = usePolling((signal) => projectApi.latestScan(project.project_id, signal), project.latest_scan?.status === "running" ? 5000 : 15000); const scan = state.data?.scan ?? project.latest_scan;
  return (
    <DialogFrame title="最近扫描结果" description={`${project.name} · 扫描不会重复创建剪辑记录。`}>
      {!scan ? <p className="quiet-state">这个项目还没有扫描记录。</p> : (
        <>
          <div className={`readiness ${statusTone(scan.status)}`}>
            <StatusPill status={scan.status} label={scan.status === "partial" ? "部分完成" : scan.status === "success" ? "已完成" : undefined} />
            <div><strong>{scanMessage(scan)}</strong><p>{scan.trigger_source === "scheduled" ? "定时触发" : "手动触发"} · {time(scan.completed_at ?? scan.started_at)}</p></div>
          </div>
          <div className="scan-stats"><Metric label="新建记录" value={scan.created_count ?? 0} /><Metric label="已处理过" value={scan.duplicate_count ?? 0} /><Metric label="等待稳定" value={scan.unstable_count ?? 0} /><Metric label="不支持" value={scan.unsupported_count ?? 0} /><Metric label="范围外" value={scan.excluded_count ?? 0} /><Metric label="失败" value={scan.failed_count ?? 0} tone="error" /></div>
          {scan.error_summary && <p className="form-error" role="alert">{scan.error_summary}</p>}
        </>
      )}
    </DialogFrame>
  );
}

export function ChooseRecordingsDialog({ project, onScanned }: { project: ProjectSummary; onScanned(scan: ScanEvent): Promise<void> }) {
  const navigate = useNavigate(); const [files, setFiles] = useState<SourceFile[]>([]); const [selected, setSelected] = useState<string[]>([]); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const id = useRef(requestId("scan-selected"));
  useEffect(() => { const controller = new AbortController(); projectApi.sourceFiles(project.project_id, controller.signal).then((result) => setFiles(result.files)).catch((reason) => setError((reason as Error).message)); return () => controller.abort(); }, [project.project_id]);
  const scan = async () => { if (!selected.length || busy) return; setBusy(true); try { const result = await projectApi.scan(project.project_id, id.current, "selected", selected); await onScanned(result.scan); navigate(`/projects/${project.project_id}`, { replace: true }); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } };
  return <DialogFrame wide closeDisabled={busy} title="选择已有录像" description="只会提交当前勾选且可处理的相对路径。" footer={<><span>{selected.length} 个已选择</span><span className="footer-spacer" /><Button isLoading={busy} isDisabled={!selected.length || busy} onClick={() => void scan()} label={(busy ? "扫描中…" : "扫描所选录像")} variant="primary" /></>}>{error && <p className="form-error" role="alert">{error}</p>}<div className="source-files">{files.map((file) => <label className={!file.selectable ? "disabled" : ""} key={file.relative_path}><input type="checkbox" disabled={!file.selectable} checked={selected.includes(file.relative_path)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, file.relative_path] : current.filter((item) => item !== file.relative_path))} /><span><strong>{file.relative_path}</strong><small>{formatBytes(file.bytes)} · {time(file.modified_at)}{file.reason ? ` · ${file.reason}` : ""}</small></span></label>)}{!files.length && !error && <LoadingState />}</div></DialogFrame>;
}

export function PauseProjectDialog({ projectName, onClose, onConfirm }: { projectName: string; onClose(): void; onConfirm(): Promise<boolean> }) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => { if (busy) return; setBusy(true); const succeeded = await onConfirm(); setBusy(false); if (succeeded) onClose(); };
  return <DialogFrame alert closeDisabled={busy} title="暂停项目" description={`确认暂停“${projectName}”的自动扫描？`} onClose={onClose} footer={<><Button isDisabled={busy} onClick={onClose} label={"取消"} /><span className="footer-spacer" /><Button isDisabled={busy} onClick={() => void confirm()} label={(busy ? "暂停中…" : "确认暂停")} variant="primary" /></>}><div className="readiness warning"><StatusPill status="paused" label="暂停自动扫描" /><div><strong>已有工作会继续，手动扫描仍可用</strong><p>暂停只会停止后续自动扫描，不会中断正在处理或排队的剪辑记录。</p></div></div></DialogFrame>;
}
