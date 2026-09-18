import { Field } from '@astryxdesign/core/Field';
import { FormLayout } from '@astryxdesign/core/FormLayout';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Selector } from '@astryxdesign/core/Selector';
import { NumberInput } from '@astryxdesign/core/NumberInput';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { Button } from '@astryxdesign/core/Button';
import { useEffect, useId, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useBlocker, useBeforeUnload } from 'react-router-dom';
import { api, post, ApiError } from './api';
import { requestId } from './project-api';
import { DialogFrame } from './ProjectDialogs';
import { ErrorState, LoadingState, PageHeading, usePolling } from './workbench-shared';

type Kind = 'ai' | 'local_asr' | 'cloud_asr' | 'local_agent';
type Purpose = 'asr' | 'analysis' | 'review';
type Config = { model?: string; endpoint?: string; provider?: string; region?: string; workspace?: string; model_source?: string; language?: string; purposes: Purpose[]; [key: string]: unknown };
type Proposal = { name: string; kind: Kind; config: Config };
export type Resource = Proposal & { model_files?: { state: string; installed_bytes: number; partial_bytes: number; bytes_downloaded: number; bytes_total: number; last_error?: string; job_id?: string }; preparation?: { id: string; status: string }; resource_id: string; revision: number; ready: boolean; deleted: boolean; has_credential: boolean; state: string; validation: Record<string, { state: string; code: string; checked_at?: string }>; projects: Array<{ project_id: string; name: string; purposes: Purpose[] }>; active_runs: Array<{ run_id: string; project_id: string; status: string }>; failed_runs: Array<{ run_id: string; project_id: string; status: string }> };
const purposes: Record<Purpose, string> = { asr: '语音识别', analysis: '内容分析', review: '片段筛选' };
const kinds: Record<Kind, string> = { local_asr: '本机语音识别', cloud_asr: '云端语音识别', ai: 'AI 模型', local_agent: 'Claude Code' };
const states: Record<string, string> = { pending: '待准备', preparing: '准备中', ready: '可用', needs_repair: '不可用' };
const validationMessages: Record<string, string> = {
  model_integrity_failed: '模型文件检查未通过，暂时无法使用。',
  revision_conflict: '模型配置已更新，请重新打开当前配置后准备。', insufficient_disk_space: '模型保存位置的可用空间不足。', model_directory_unwritable: '无法写入模型文件夹，请检查访问权限。', model_download_unavailable: '无法下载模型文件，请检查下载来源和网络连接。', model_preparation_failed: '模型准备未完成，已下载的文件已保留。请记录问题编号并联系开发者排查。',
  credential_invalid: 'API Key 无效，请核对后重新填写。', credential_unavailable: '已保存的 API Key 无法使用，请重新填写原账号的 API Key。',
  permission_or_quota: '账号额度或权限不足，请到供应商控制台检查。', rate_limited: '请求受到限制，请稍后重新检查。',
  connection_timeout: '连接超时，请检查网络和服务地址。', result_unknown: '暂时无法确认本次请求结果。',
  model_not_found: '找不到模型或账号无权使用，请核对模型 ID 和账号权限。', parameter_unsupported: '服务不支持当前参数，请按供应商说明调整。',
  output_format_invalid: '模型返回的内容格式不符合要求，暂时无法用于这项用途。', analysis_output_invalid: '模型返回的分析结果不符合要求。',
  correction_output_invalid: '模型返回的文字校对结果不符合要求。', capability_validation_failed: '检查未通过，请检查模型配置和运行环境。',
  model_not_installed: '模型尚未安装完整，请先准备模型。', ai_resource_unavailable: 'Claude Code 暂时无法使用，请检查安装和登录状态。',
};
function validationMessage(code: string, kind?: Kind) { const message = code ? validationMessages[code] || '检查未通过，请检查模型配置。' : ''; return kind === 'local_asr' ? message.replaceAll('API Key', 'Hugging Face Token') : message; }
const resourceFields: Record<string, string> = {
  name: '模型名称', kind: '模型类型', model: '模型 ID', endpoint: '服务地址', provider: '供应商', region: '地域', workspace: '业务空间 ID',
  purposes: '用途', language: '识别语言', model_source: '下载来源', timeout_seconds: '请求超时（秒）', request_attempts: '连接尝试次数（含首次）',
  retry_delay_seconds: '连接重试间隔（秒）', temperature: '片段筛选温度', max_tokens: '片段筛选输出上限（Token）',
  command_timeout_minutes: 'Claude Code 超时（分钟）', request_profile: '请求方式',
};
function resourceValue(key: string, value: unknown, providers: Array<{ id: string; name: string }>, regions: Record<string, string>): string {
  if (value === undefined || value === null || value === '') return '未填写';
  if (key === 'provider') return providers.find(p => p.id === value)?.name || '未知供应商';
  if (key === 'region') return regions[String(value)] || '未知地域';
  if (key === 'kind') return kinds[value as Kind] || '未知类型';
  if (key === 'purposes' && Array.isArray(value)) return value.map(p => purposes[p as Purpose]).join('、');
  if (key === 'language') return ({zh:'中文',en:'英语',auto:'自动识别'} as Record<string,string>)[String(value)] || '未知语言';
  if (key === 'model_source') return value === 'modelscope' ? 'ModelScope' : value === 'huggingface' ? 'Hugging Face' : '未知来源';
  if (key === 'request_profile') return value === 'kimi-k2.6-default' ? '供应商默认参数' : '自定义参数';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  return String(value);
}
function sameVisibleConfig(first: Config, second: Config) {
  return Object.keys(resourceFields).filter(key => key !== 'name' && key !== 'kind').every(key => JSON.stringify(first[key]) === JSON.stringify(second[key]));
}
function ResourceChanges({ current, proposal, providers, regions }: { current: Resource; proposal: Proposal; providers: Array<{id:string;name:string}>; regions: Record<string,string> }) {
  const before: Record<string, unknown> = { name: current.name, kind: current.kind, ...current.config };
  const after: Record<string, unknown> = { name: proposal.name, kind: proposal.kind, ...proposal.config };
  const changed = Object.keys(resourceFields).filter(key => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
  return <dl className="summary-list">{changed.map(key => <div key={key}><dt>{resourceFields[key]}</dt><dd>已保存：{resourceValue(key, before[key], providers, regions)} → 本次修改：{resourceValue(key, after[key], providers, regions)}</dd></div>)}</dl>;
}
const empty = (): Proposal => ({ name: '', kind: 'ai', config: { provider: '', model: '', purposes: ['analysis', 'review'] } });
type SavedDraft = { proposal: Proposal; revision?: number; operation?: string; validationOperation?: string; pendingSave?: boolean; resourceId?: string };
const readDraft = (key: string): SavedDraft | null => { try { return JSON.parse(localStorage.getItem(key) || 'null') as SavedDraft | null; } catch { return null; } };

export function resourceReturn(search: string, resource?: Resource): string | null {
  const query = new URLSearchParams(search); const origin = query.get('origin'); const project = query.get('project'); const purpose = query.get('purpose');
  if (!purpose || !(purpose in purposes)) return null;
  if (origin === 'onboarding') { const next = new URLSearchParams({ resourcePurpose: purpose }); if (resource && resource.config.purposes.includes(purpose as Purpose)) next.set('selectedResource', resource.resource_id); return `/studio?${next}`; }
  if (origin !== 'new-project' && (origin !== 'project' || !project || !/^[A-Za-z0-9_-]+$/.test(project))) return null;
  const next = new URLSearchParams({ dialog: origin === 'new-project' ? 'new-project' : 'project-settings', resourcePurpose: purpose });
  const sourceDraft = query.get('sourceDraft'); if (origin === 'new-project' && sourceDraft && /^[A-Za-z0-9_-]+$/.test(sourceDraft)) next.set('draft', sourceDraft);
  if (resource && resource.config.purposes.includes(purpose as Purpose)) next.set('selectedResource', resource.resource_id);
  return `${origin === 'new-project' ? '/projects' : `/projects/${project}`}?${next}`;
}

export function ResourcesPage() {
  const location = useLocation(); const parts = location.pathname.split('/').filter(Boolean);
  if (parts[1] === 'new') return <ResourceEditor key={location.pathname + location.search} />;
  if (parts[1] && parts[2] === 'edit') return <ResourceEditor key={location.pathname + location.search} resourceId={parts[1]} />;
  if (parts[1]) return <ResourceDetail resourceId={parts[1]} />;
  return <ResourceList />;
}

function ResourceList() {
  const state = usePolling((signal) => api<{ resources: Resource[]; migration: string; cleanups: Array<{ request_id: string; resource_id: string }> }>('/api/resources', {}, signal), 15000, 'resources');
  const [actionError, setActionError] = useState(''); const [search, setSearch] = useState(''); const [purpose, setPurpose] = useState(''); const [status, setStatus] = useState('');
  if (!state.data) return state.loading ? <LoadingState /> : <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const filtered = state.data.resources.filter(r => `${r.name} ${r.config.model || ''}`.toLowerCase().includes(search.toLowerCase()) && (!purpose || r.config.purposes.includes(purpose as Purpose)) && (!status || r.state === status || Object.values(r.validation).some(v => v.state === status)));
  return <section className="page resources-page"><PageHeading title="模型与工具" actions={<Button  label={"添加模型"} variant="primary" href={"/resources/new"} as={ResourceLink} />} />
    {actionError && <p role="alert">{actionError}</p>}{state.error && <p role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}
    {state.data.migration !== 'completed' && <p role="alert">旧版模型配置尚未导入完成，历史记录已保留。<Button onClick={() => void post('/api/resources/migration/retry', {}).then(() => state.refresh()).catch(reason => setActionError(reason.message))} label={"重试导入"} /></p>}
    {state.data.cleanups?.map(item => <p role="status" key={item.request_id}>模型已移除，相关文件尚未清理完。<Link to={`/resources/${item.resource_id}`}>查看已删除模型</Link><Button onClick={() => void post(`/api/resources/cleanup/${item.request_id}`, {}).then(() => state.refresh()).catch(reason => setActionError(reason.message))} label={"重试清理"} /></p>)}
    <div className="resource-filters"><TextInput label={"搜索名称或模型"} value={search} onChange={value => setSearch(value)} placeholder="输入名称或模型"  /><Selector label={"用途"} value={purpose} onChange={value => setPurpose(value)} options={[{value:"",label:"全部用途"},...Object.entries(purposes).map(([id, label]) => ({value:id,label:(label)}))]} width="100%"  /><Selector label={"状态"} value={status} onChange={value => setStatus(value)} options={[{value:"",label:"全部状态"},...Object.entries(states).map(([id, label]) => ({value:id,label:(label)}))]} width="100%"  /></div>
    <div className="resource-list">{filtered.map(r => <article className="resource-row" key={r.resource_id}><div><Link to={`/resources/${r.resource_id}`}><strong>{r.name}</strong></Link><small>{[kinds[r.kind], r.config.model || (r.kind === 'local_agent' ? '' : '未选择模型'), r.kind === 'local_agent' ? '本机启动' : r.kind === 'local_asr' ? '本机' : r.config.endpoint || '未设置服务地址'].filter(Boolean).join(' · ')}</small><ResourceStatus resource={r} /></div><Link to={`/resources/${r.resource_id}#projects`}>{r.projects.length ? `${r.projects.length} 个项目使用` : '暂无项目使用'}</Link></article>)}</div>
    {!filtered.length && <div className="empty-state"><strong>{state.data.resources.length ? '没有找到符合条件的模型' : '还没有添加模型或工具'}</strong>{!state.data.resources.length && <p>点击“添加模型”，添加语音识别模型、AI 模型或 Claude Code。</p>}{state.data.resources.length > 0 && <Button onClick={() => { setSearch(''); setPurpose(''); setStatus(''); }} label={"清除筛选"} />}</div>}
  </section>;
}

function ResourceLink({ href = '', ...props }: Omit<React.ComponentProps<typeof Link>, 'to'> & { href?: string }) { return <Link {...props} to={href} />; }

function ResourceStatus({ resource }: { resource: Resource }) { return <p role="status">{resource.config.purposes.map(p => `${purposes[p]}：${states[resource.validation[p]?.state || 'pending']}`).join(' · ')}</p>; }

function ResourceDetail({ resourceId }: { resourceId: string }) {
  const location = useLocation(); const state = usePolling(signal => api<{ resource: Resource }>(`/api/resources/${resourceId}`, {}, signal), 10000, resourceId); const [deleting, setDeleting] = useState(false);
  const catalog = usePolling(signal => api<{ providers: Array<{id:string;name:string}>; regions: Record<string,string> }>('/api/resources/providers', {}, signal), 60000);
  if (!state.data) return state.loading ? <LoadingState /> : <ErrorState message={state.error} retry={() => void state.refresh()} />;
  const r = state.data.resource; const back = resourceReturn(location.search);
  return <section className="page"><PageHeading title={r.name} eyebrow={kinds[r.kind]} description={r.deleted ? '模型已移除，历史记录仍可查看。' : r.kind === 'local_agent' ? undefined : r.config.model || '未选择模型'} actions={!r.deleted && <><Button  label={"编辑配置"} href={`/resources/${resourceId}/edit${location.search}`} as={ResourceLink} /><Button onClick={() => setDeleting(true)} label={"移除模型"} variant="destructive" /></>} />
    {state.error && <p role="alert">刷新失败，当前显示的是上次加载的内容。原因：{state.error}</p>}<ResourceStatus resource={r} />
    <dl className="summary-list"><div><dt>{r.kind === 'ai' || r.kind === 'cloud_asr' ? '服务地址' : '运行方式'}</dt><dd>{r.kind === 'local_asr' ? '本机' : r.kind === 'local_agent' ? '本机启动' : r.config.endpoint || '未设置服务地址'}</dd></div>
      {(r.kind === 'ai' || r.kind === 'cloud_asr' || r.kind === 'local_asr' && r.config.model_source === 'huggingface') && <div><dt>{r.kind === 'local_asr' ? 'Hugging Face Token（选填）' : 'API Key'}</dt><dd>{r.has_credential ? '已保存' : '未填写'}</dd></div>}<div><dt>配置版本</dt><dd>{r.revision}</dd></div></dl>
    <dl className="summary-list">{Object.entries(resourceFields).filter(([key]) => !['name','kind','model','endpoint','purposes','request_profile'].includes(key)).map(([key, label]) => r.config[key] !== undefined && !(key === 'temperature' && r.config.request_profile === 'kimi-k2.6-default') && <div key={key}><dt>{label}</dt><dd>{resourceValue(key, r.config[key], catalog.data?.providers || [], catalog.data?.regions || {})}</dd></div>)}</dl>
    <section><h2>最近检查结果</h2>{r.config.purposes.map(p => <p key={p}>{purposes[p]}：{states[r.validation[p]?.state || 'pending']}{r.validation[p]?.checked_at ? ` · 上次检查：${new Date(r.validation[p].checked_at!).toLocaleString()}` : !r.validation[p] ? ' · 尚未检查' : ''}{r.validation[p]?.code && ` · ${validationMessage(r.validation[p].code, r.kind)}`}</p>)}</section>
    {r.kind === 'local_asr' && !r.deleted && <ModelPreparation key={`${r.resource_id}:${r.revision}`} resource={r} refresh={state.refresh} />}
    <section id="projects"><h2>使用这个模型的项目</h2>{r.projects.map(p => <div className="resource-row" key={p.project_id}><span>{p.name} · {p.purposes.map(v => purposes[v]).join('、')}</span><Button  label={"项目设置"} href={`/projects/${p.project_id}?dialog=project-settings&resourceReturn=${resourceId}`} as={ResourceLink} /></div>)}{!r.projects.length && <p>暂无项目使用</p>}</section>
    <div className="resource-actions"><Button  label={"返回模型列表"} href={"/resources"} as={ResourceLink} />{back && <Button  label={(new URLSearchParams(location.search).get('origin') === 'onboarding' ? '返回首次设置' : '返回项目')} href={back} as={ResourceLink} />}</div>
    {deleting && <DeleteResource resource={r} close={() => setDeleting(false)} />}
  </section>;
}

function ModelPreparation({ resource, refresh }: { resource: Resource; refresh(): Promise<unknown> }) {
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState(''); const [jobId, setJobId] = useState(resource.preparation?.id || '');
  const job = usePolling(signal => jobId ? api<{ job: { status: string; error?: string; result?: { message?: string; code?: string; diagnostic_id?: string; resource_id?: string; revision?: number } } }>(`/api/jobs/${jobId}`, {}, signal) : Promise.resolve({ job: { status: '', error: '' } }), 2000, `model-${jobId}`);
  const running = job.data?.job.status === 'running' || !!resource.preparation;
  useEffect(() => { if (job.data?.job.status && job.data.job.status !== 'running') void refresh(); }, [job.data?.job.status]);
  const start = async () => { setBusy(true); try { const result = await post<{ job: { id: string } }>(`/api/resources/${resource.resource_id}/prepare`, { expected_revision: resource.revision }); setJobId(result.job.id); setMessage('模型准备已开始，离开此页后会继续。'); await refresh(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } };
  const files = resource.model_files; const percent = files?.bytes_total ? Math.min(100, Math.round((files.installed_bytes || files.bytes_downloaded) / files.bytes_total * 100)) : 0;
  return <section><h2>本机模型</h2><p>下载模型后会自动检查，检查通过才能使用。</p>{files && <><p>已安装：{(files.installed_bytes / 1024 ** 3).toFixed(2)} GiB · 下载中的文件： {(files.partial_bytes / 1024 ** 3).toFixed(2)} GiB</p>{running && <progress aria-label="模型文件下载进度" max={100} value={percent} />}</>}<Button isLoading={busy || running} isDisabled={busy || running} onClick={() => void start()} label={(running ? '正在准备模型…' : resource.ready ? '重新检查模型' : files?.partial_bytes ? '继续准备模型' : '准备模型')} /><p role="status">{job.data?.job.status === 'succeeded' ? resource.validation.asr?.state === 'ready' ? '模型可以使用了' : job.data.job.result?.resource_id === resource.resource_id && job.data.job.result?.revision === resource.revision ? '本次准备已完成' : '模型文件准备任务已结束，当前配置仍需检查。' : (['failed', 'interrupted'].includes(job.data?.job.status || '') ? job.data?.job.status === 'interrupted' ? '后台服务重启，模型准备已中断。已下载的文件已保留。' : job.data?.job.result?.code ? validationMessage(job.data.job.result.code) : '模型准备未完成，已下载的文件已保留。' : '') || message || (files?.last_error ? '上次准备未完成，已下载的文件已保留，可继续准备。' : '')}</p>{job.data?.job.result?.diagnostic_id && <small>问题编号：{job.data.job.result.diagnostic_id}</small>}{job.error && <p role="alert">暂时无法获取进度。原因：{job.error}</p>}</section>;
}

function DeleteResource({ resource, close }: { resource: Resource; close(): void }) {
  const navigate = useNavigate(); const state = usePolling(signal => api<{ preview: { can_delete: boolean; resource: Resource; projects: Resource['projects']; active_runs: Resource['active_runs']; failed_runs: Resource['failed_runs']; tasks: string[]; model_files: { can_clean: boolean; shared: boolean; bytes: number } } }>(`/api/resources/${resource.resource_id}/delete-preview`, {}, signal), 15000, `delete-${resource.resource_id}`);
  const [cleanModel, setCleanModel] = useState(false); const [cleanup, setCleanup] = useState<{ cleanup_state: string; cleanup_errors: string[] } | null>(null);
  const [uncertain, setUncertain] = useState(false); const [confirmed, setConfirmed] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const id = useRef(requestId('resource-delete'));
  const preview = state.data?.preview;
  const acceptDeleted = (result: { cleanup_state: string; cleanup_errors: string[] }) => { setUncertain(false); if (result.cleanup_state !== 'completed') setCleanup(result); else navigate('/resources', { replace: true }); };
  const recover = async () => { if (busy) return; setBusy(true); try { const operation = await api<{ result: { cleanup_state: string; cleanup_errors: string[] } | null }>(`/api/resources/operations/${id.current}`); if (operation.result) acceptDeleted(operation.result); else setError('暂时无法确认是否已移除，请点击“查询移除结果”。'); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const remove = async () => {
    if (busy || uncertain || !preview?.can_delete || state.error || (preview.failed_runs.length && !confirmed)) return;
    setBusy(true); setError('');
    try { const deleted = await api<{ result: { cleanup_state: string; cleanup_errors: string[] } }>(`/api/resources/${resource.resource_id}`, { method: 'DELETE', body: JSON.stringify({ request_id: id.current, expected_revision: resource.revision, acknowledge_failed: confirmed, clean_model: cleanModel && preview.model_files.can_clean }) }); acceptDeleted(deleted.result); }
    catch (e) { const known = e instanceof ApiError && !e.outcomeUnknown; setUncertain(!known); setError((e as Error).message); if (known) { setConfirmed(false); await state.refresh(); } }
    finally { setBusy(false); }
  };
  return <DialogFrame alert title={`移除“${resource.name}”？`} description="移除后，历史成片和剪辑记录仍会保留。" onClose={close} closeDisabled={busy || uncertain} footer={<><Button isDisabled={busy || uncertain} onClick={close} label={cleanup ? "关闭" : "取消"} /><Button isDisabled={busy || uncertain} onClick={() => void state.refresh()} label={"重新检查"} />{uncertain && <Button isLoading={busy} isDisabled={busy} onClick={() => void recover()} label={busy ? "正在查询…" : "查询移除结果"} />}{preview?.can_delete && !cleanup && !uncertain && <Button isLoading={busy} isDisabled={busy || Boolean(state.error) || (!!preview.failed_runs.length && !confirmed)} onClick={() => void remove()} label={(busy ? '正在移除…' : '移除模型')} variant="destructive" />}</>}>
    {cleanup && <section role="alert"><p>模型已移除，相关文件尚未清理完。历史成片和剪辑记录仍会保留。原因：{cleanup.cleanup_errors.map(code => ({ model_cleanup_in_use: '模型文件仍在使用', model_cleanup_failed: '模型文件清理失败', credential_cleanup_failed: '密钥文件清理失败' } as Record<string,string>)[code] || '文件清理未完成').join('、')}</p><Button isDisabled={busy} onClick={() => { setBusy(true); post<{ result: typeof cleanup }>(`/api/resources/cleanup/${id.current}`, {}).then(value => { if (value.result?.cleanup_state === 'completed') navigate('/resources', { replace: true }); else setCleanup(value.result); }).catch(reason => setError(reason.message)).finally(() => setBusy(false)); }} label={"重试清理"} /></section>}{(state.error || error) && <p role="alert">{error || state.error}</p>}{!preview ? <LoadingState /> : <>{preview.projects.length > 0 && <p>仍有项目使用这个模型，请先在项目设置中更换或取消选择。</p>}{preview.projects.map(p => <p key={p.project_id}>{p.name} · {p.purposes.map(v => purposes[v]).join('、')} <Link aria-disabled={busy || uncertain || undefined} onClick={event => { if (busy || uncertain) event.preventDefault(); }} to={`/projects/${p.project_id}?dialog=project-settings&resourceReturn=${resource.resource_id}`}>项目设置</Link></p>)}{preview.active_runs.map(r => <p key={r.run_id}>以下剪辑记录仍在使用这个模型，暂时无法移除：<Link aria-disabled={busy || uncertain || undefined} onClick={event => { if (busy || uncertain) event.preventDefault(); }} to={`/projects/${r.project_id}/runs/${r.run_id}`}>{r.run_id}</Link></p>)}{preview.tasks.length > 0 && <p>模型正在准备或检查，完成后才能移除。</p>}{resource.kind === 'local_asr' && <CheckboxInput label={"同时删除本机模型文件（" + " " + ((preview.model_files.bytes / 1024 ** 3).toFixed(2)) + " " + "GiB）" + " " + (preview.model_files.shared ? '这些文件还被其他模型配置使用，不能删除。' : '')} isDisabled={busy || uncertain || !preview.model_files.can_clean} value={cleanModel && preview.model_files.can_clean} onChange={value => setCleanModel(value)}  />}{preview.failed_runs.length > 0 && <><p>移除后，以下失败记录将无法使用这个模型继续处理。已有结果会保留，你可以使用当前项目设置重新处理。</p>{preview.failed_runs.map(r => <p key={r.run_id}><Link aria-disabled={busy || uncertain || undefined} onClick={event => { if (busy || uncertain) event.preventDefault(); }} to={`/projects/${r.project_id}/runs/${r.run_id}`}>{r.run_id}</Link></p>)}<CheckboxInput label={"我知道这些失败记录将无法使用此模型继续处理"} isDisabled={busy || uncertain} value={confirmed} onChange={value => setConfirmed(value)}  /></>}<p>{resource.kind === 'local_asr' ? cleanModel && preview.model_files.can_clean ? '移除时会一并删除上述本机模型文件，再次使用需要重新下载。' : '本机模型文件会保留，再次添加时可复用。' : resource.kind === 'local_agent' ? '只移除 Venus 中的接入配置，不会卸载 Claude Code。' : '只移除 Venus 中的模型配置，不会删除供应商账号或云端模型。'}</p></>}
  </DialogFrame>;
}

function ResourceEditor({ resourceId }: { resourceId?: string }) {
  const navigate = useNavigate(); const location = useLocation(); const draftId = useRef(new URLSearchParams(location.search).get('draft') || requestId('draft')); const draftKey = `venus.resource-draft.${resourceId || draftId.current}`;
  const [proposal, setProposal] = useState<Proposal>(() => readDraft(draftKey)?.proposal || (new URLSearchParams(location.search).get('purpose') === 'asr' ? { name: '', kind: 'local_asr', config: { purposes: ['asr'], model_source: 'modelscope' } } : empty())); const [current, setCurrent] = useState<Resource | null>(null); const credentialRef = useRef<HTMLInputElement>(null); const credentialID = useId(); const [credentialVersion, setCredentialVersion] = useState(0); const credential = credentialRef.current?.value || ''; const clearCredential = () => { if (credentialRef.current) credentialRef.current.value = ''; setCredentialVersion(v => v + 1); }; const [visible, setVisible] = useState(false); const [loaded, setLoaded] = useState(!resourceId);
  const [providers, setProviders] = useState<Array<{ id: string; name: string; endpoint?: string; help: string }>>([]); const [regions, setRegions] = useState<Record<string, string>>({}); const [models, setModels] = useState<Array<{ id: string; display_name: string; size_note: string }>>([]);
  const [validation, setValidation] = useState<{ validation_id: string; results: Resource['validation']; content: string } | null>(null); const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [leaving, setLeaving] = useState(false); const submitId = useRef(readDraft(draftKey)?.operation || requestId('resource-save')); const baseRevision = useRef(readDraft(draftKey)?.revision); const allowLeave = useRef(false); const requestContent = useRef(''); const savePending = useRef(Boolean(readDraft(draftKey)?.pendingSave)); const savedContent = useRef(''); const targetId = useRef(resourceId || readDraft(draftKey)?.resourceId); const proposalRef = useRef(proposal); proposalRef.current = proposal; const validationOperation = useRef(readDraft(draftKey)?.validationOperation || ''); const testedContent = useRef(''); const validationRequest = useRef<object | null>(null);
  const persistDraft = () => localStorage.setItem(draftKey, JSON.stringify({ proposal: proposalRef.current, revision: baseRevision.current, operation: submitId.current, validationOperation: validationOperation.current, pendingSave: savePending.current, resourceId: targetId.current }));
  useEffect(() => { const controller = new AbortController(); api<{ providers: typeof providers; regions: typeof regions }>('/api/resources/providers', {}, controller.signal).then(v => { setProviders(v.providers); setRegions(v.regions); }).catch(e => setError(e.message)); api<{ models: typeof models }>('/api/asr/models', {}, controller.signal).then(v => setModels(v.models)).catch(e => setError(e.message)); if (targetId.current) api<{ resource: Resource }>(`/api/resources/${targetId.current}`, {}, controller.signal).then(v => { setCurrent(v.resource); if (baseRevision.current === undefined) baseRevision.current = v.resource.revision; if (!readDraft(draftKey)) setProposal({ name: v.resource.name, kind: v.resource.kind, config: v.resource.config }); setLoaded(true); }).catch(e => setError(e.message)); return () => controller.abort(); }, [resourceId, draftKey]);
  useEffect(() => { if (loaded) persistDraft(); }, [proposal, loaded, draftKey]);
  const [previewConfig, setPreviewConfig] = useState<Config | null>(null); const [availableModels, setAvailableModels] = useState<string[]>([]); const [existingResources, setExistingResources] = useState<Resource[]>([]);
  useEffect(() => { if (!resourceId) api<{ resources: Resource[] }>('/api/resources').then(value => setExistingResources(value.resources)).catch(() => undefined); }, [resourceId]);
  useEffect(() => { setPreviewConfig(null); if (!proposal.name || proposal.kind !== 'ai') return; const controller = new AbortController(); api<{ proposal: Proposal }>('/api/resources/preview', { method: 'POST', body: JSON.stringify({ proposal }) }, controller.signal).then(result => setPreviewConfig(result.proposal.config)).catch(() => undefined); return () => controller.abort(); }, [proposal]);
  const discover = async () => { if (busy) return; const tested = content; setBusy('models'); setError(''); try { const result = await post<{ models: string[] }>('/api/resources/models', { proposal, credential: credentialRef.current?.value || undefined, resource_id: targetId.current, revision: baseRevision.current }); if (contentRef.current !== tested) return; setAvailableModels(result.models); if (!result.models.length) setError('没有获取到模型列表，可手动填写模型 ID。'); } catch { setError('获取模型列表失败，可手动填写模型 ID 后检查是否可用。'); } finally { setBusy(''); } };
  const content = JSON.stringify([proposal.kind, proposal.config, credentialVersion]); const contentRef = useRef(content); contentRef.current = content; const draftContent = JSON.stringify([proposal, credentialVersion]); const draftContentRef = useRef(draftContent); draftContentRef.current = draftContent;
  const change = (next: Proposal) => { setProposal(next); setAvailableModels([]); setError(''); };
  const field = (key: string, value: unknown) => change({ ...proposal, config: { ...proposal.config, [key]: value } });
  const selectKind = (kind: Kind) => { clearCredential(); setValidation(null); change({ name: proposal.name, kind, config: { purposes: kind === 'ai' ? ['analysis', 'review'] : kind === 'local_agent' ? ['review'] : ['asr'], ...(kind === 'local_asr' ? { model_source: 'modelscope', language: 'zh' } : {}) } }); };
  const selectProvider = (provider: string) => { clearCredential(); setValidation(null); change({ ...proposal, config: { purposes: proposal.config.purposes, provider, model: '', endpoint: providers.find(p => p.id === provider)?.endpoint || '' } }); };
  const numericInvalid = Object.values(proposal.config).some(v => v === null);
  const test = async () => { if (busy || !validationOperation.current && numericInvalid) return;
    setBusy('test'); setError(''); let submitted = false;
    try {
      if (validationOperation.current) {
        const prior = await api<{ result: { validation_id?: string; results?: Resource['validation']; state?: string } | null }>(`/api/resources/operations/${validationOperation.current}`);
        if (prior.result && !prior.result.validation_id) { setError('暂时无法确认上次检查结果，请稍后点击“查询检查结果”。'); return; }
        if (prior.result?.validation_id) {
        if (testedContent.current === contentRef.current) setValidation({ validation_id: prior.result.validation_id, results: prior.result.results!, content: testedContent.current });
        else setError('上次检查已完成，但配置或密钥有变化，请重新检查。');
        validationOperation.current = ''; return;
        }
      }
      if (validationOperation.current && !validationRequest.current) { setError('暂时无法确认上次检查结果，请稍后点击“查询检查结果”。'); return; }
      if (!validationOperation.current) {
        validationOperation.current = requestId('resource-validate'); testedContent.current = content;
        validationRequest.current = { request_id: validationOperation.current, proposal, credential: credentialRef.current?.value || undefined, resource_id: targetId.current, revision: baseRevision.current, purposes: proposal.config.purposes };
      }
      persistDraft();
      submitted = true;
      const result = await post<{ validation_id: string; results: Resource['validation'] }>('/api/resources/validate', validationRequest.current);
      validationOperation.current = '';
      if (contentRef.current === testedContent.current) setValidation({ ...result, content: testedContent.current });
    } catch (e) {
      if (submitted && e instanceof ApiError && !e.outcomeUnknown && e.code !== 'request_conflict') validationOperation.current = '';
      setError((e as Error).message);
    } finally {
      persistDraft(); setBusy('');
    }
  };

  const back = () => { clearCredential(); allowLeave.current = true; if (blocker.state === 'blocked') blocker.proceed(); else navigate(resourceReturn(location.search) || (resourceId ? `/resources/${resourceId}` : '/resources')); };
  const saved = (r: Resource) => {
    const changed = savedContent.current !== draftContentRef.current;
    savePending.current = false; requestContent.current = ''; targetId.current = r.resource_id;
    baseRevision.current = r.revision; submitId.current = requestId('resource-save');
    if (changed) {
      setCurrent(r); setError('上次提交已保存。当前表单中尚未提交的修改已保留，请核对后再保存。'); persistDraft();
      return;
    }
    allowLeave.current = true; localStorage.removeItem(draftKey); clearCredential();
    navigate(resourceReturn(location.search, r) || `/resources/${r.resource_id}`, { replace: true });
  };
  const save = async () => {
    if (busy || (!savePending.current && (numericInvalid || !proposal.name.trim()))) return;
    setBusy(savePending.current ? 'save-query' : 'save'); setError(''); let submitted = false;
    try {
      if (savePending.current) {
        const previous = await api<{ result: Resource | null }>(`/api/resources/operations/${submitId.current}`);
        if (previous.result) { saved(previous.result); return; }
        if (!requestContent.current) { setError('暂时无法确认上次保存结果，请稍后重试。'); return; }
      } else {
        requestContent.current = JSON.stringify({ proposal, credential: credentialRef.current?.value || undefined, expected_revision: baseRevision.current, validation_id: validation?.content === content ? validation.validation_id : undefined, request_id: submitId.current });
        savedContent.current = draftContentRef.current; savePending.current = true; persistDraft();
      }
      setBusy('save'); submitted = true;
      const result = await api<{ resource: Resource }>(targetId.current ? `/api/resources/${targetId.current}` : '/api/resources', { method: targetId.current ? 'PATCH' : 'POST', body: requestContent.current });
      saved(result.resource);
    } catch (e) {
      try {
        const operation = await api<{ result: Resource | null }>(`/api/resources/operations/${submitId.current}`);
        if (operation.result) { saved(operation.result); return; }
      } catch { /* An unreadable query cannot authorize a new write. */ }
      if (submitted && e instanceof ApiError && !e.outcomeUnknown && e.code !== 'request_conflict') {
        savePending.current = false; requestContent.current = ''; submitId.current = requestId('resource-save'); persistDraft();
      }
      if (targetId.current && e instanceof ApiError && e.code === 'revision_conflict') {
        try { const latest = await api<{ resource: Resource }>(`/api/resources/${targetId.current}`); setCurrent(latest.resource); }
        catch { /* Keep the draft until comparison is possible. */ }
      }
      setError(savePending.current ? '暂时无法确认上次保存结果，请稍后重试。' : (e as Error).message);
    } finally { setBusy(''); }
  };
  const dirty = Boolean(credential || (current ? proposal.name !== current.name || !sameVisibleConfig(proposal.config, current.config) : proposal.name || proposal.config.model));
  const unresolved = savePending.current;
  const blocker = useBlocker(({ nextLocation }) => !allowLeave.current && (dirty || !!busy || unresolved) && nextLocation.pathname !== location.pathname);
  useBeforeUnload(event => { if ((dirty || unresolved) && !allowLeave.current) event.preventDefault(); });
  useEffect(() => { if (!resourceId && !new URLSearchParams(location.search).get('draft')) { const query = new URLSearchParams(location.search); query.set('draft', draftId.current); navigate({ pathname: location.pathname, search: query.toString() }, { replace: true }); } }, [resourceId, location.pathname, location.search, navigate]);
  useEffect(() => { const pending = readDraft(draftKey)?.pendingSave ? readDraft(draftKey)?.operation : undefined; if (pending) api<{ result: Resource | null }>(`/api/resources/operations/${pending}`).then(value => { if (value.result) saved(value.result); }).catch(() => setError('暂时无法确认上次保存结果，请稍后重试。')); }, [draftKey]);
  if (!loaded) return error ? <ErrorState message={error} retry={() => window.location.reload()} /> : <LoadingState />;
  const similar = existingResources.filter(r => r.kind === proposal.kind && r.config.model && r.config.model === proposal.config.model && (r.config.endpoint || '') === (previewConfig?.endpoint || proposal.config.endpoint || ''));
  const unchanged = current && proposal.name === current.name && sameVisibleConfig(proposal.config, current.config) && !credential && validation?.content !== content;
  const renamed = current && proposal.name !== current.name && sameVisibleConfig(proposal.config, current.config) && !credential && validation?.content !== content;
  const verified = validation?.content === content; const anyReady = verified && Object.values(validation.results).some(v => v.state === 'ready');
  return <section className="page resource-editor"><PageHeading title={current ? `编辑“${current.name}”` : '添加模型'} description={current ? '修改后，使用这个模型的项目会在新建剪辑记录时采用新配置。' : '添加后，请在项目设置中选择使用。'} />
    {current?.deleted && <p role="alert">这个模型已移除，无法保存修改。</p>}{error && <p className="form-error" role="alert">{error}</p>}{current && baseRevision.current !== current.revision && <section role="alert"><p>这个模型的配置已被其他操作修改，请核对最新设置后继续。</p><ResourceChanges current={current} proposal={proposal} providers={providers} regions={regions} />{credential && <p>密钥：本次已填写新密钥，内容不回显。</p>}<Button onClick={() => { baseRevision.current = current.revision; setValidation(null); setError('已保留你的修改，请重新检查。保存后将以当前表单内容为准。'); }} label={"保留我的修改，继续编辑"} /></section>}
    {!!similar.length && <section role="status"><p>已添加过相同模型和服务地址。可以直接使用已添加的模型，也可以为不同账号分别添加。</p>{similar.map(r => <Button key={r.resource_id} label={`${r.name} · ${resourceReturn(location.search, r) ? '使用此模型' : '查看模型'}`} href={resourceReturn(location.search, r) || `/resources/${r.resource_id}`} as={ResourceLink} />)}</section>}
    <form className="resource-form" onSubmit={e => { e.preventDefault(); void save(); }}><FormLayout className="resource-fields">
      <TextInput label={"模型名称"} isRequired  value={proposal.name} onChange={value => change({ ...proposal, name: value.slice(0, 80) })}  />
      <Selector label={"模型类型"} isDisabled={!!current} value={proposal.kind} onChange={value => selectKind(value as Kind)} options={[...Object.entries(kinds).filter(([kind]) => { const purpose = new URLSearchParams(location.search).get('purpose'); return !purpose || (purpose === 'asr' ? kind.includes('asr') : purpose === 'analysis' ? kind === 'ai' : kind === 'ai' || kind === 'local_agent'); }).map(([id, name]) => ({value:id,label:(name)}))]} width="100%"  />
      {proposal.kind === 'ai' && <><Selector label={"供应商"} value={proposal.config.provider || ''} onChange={value => selectProvider(value)} options={[{value:"",label:"请选择供应商"},...providers.map(p => ({value:p.id,label:(p.name)}))]} width="100%"  />{providers.find(p => p.id === proposal.config.provider)?.help && <p>{providers.find(p => p.id === proposal.config.provider)?.help}</p>}{proposal.config.provider === 'qwen' && <><Selector label={"地域"} value={proposal.config.region || ''} onChange={value => { clearCredential(); change({ ...proposal, config: { ...proposal.config, region: value, workspace: '', endpoint: '', model: '' } }); }} options={[{value:"",label:"请选择地域"},...Object.entries(regions).map(([id, name]) => ({value:id,label:(name)}))]} width="100%"  /><TextInput label={"业务空间 ID"} value={proposal.config.workspace || ''} onChange={value => { clearCredential(); change({ ...proposal, config: { ...proposal.config, workspace: value, endpoint: '', model: '' } }); }}  /></>}</>}
      {proposal.kind === 'ai' && proposal.config.provider !== 'custom' && <p>服务地址：{previewConfig?.endpoint || (!proposal.config.provider ? '请先选择供应商' : proposal.config.provider === 'qwen' && !proposal.config.region ? '请选择地域' : proposal.config.provider === 'qwen' && !proposal.config.workspace ? '请填写业务空间 ID' : '服务地址暂未获取')}</p>}
      {(proposal.kind === 'cloud_asr' || proposal.kind === 'ai' && proposal.config.provider === 'custom') && <TextInput label="服务地址" description="更改服务地址后，请重新输入 API Key。" value={proposal.config.endpoint || ''} onChange={value => { clearCredential(); field('endpoint', value); }}  />}
      {proposal.kind === 'local_asr' ? <><Selector label={"模型"} value={proposal.config.model || ''} onChange={value => field('model', value)} options={[{value:"",label:"请选择模型"},...models.map(m => ({value:m.id,label:(m.display_name) + " " + "·" + " " + (m.size_note)}))]} width="100%"  /><Selector label={"下载来源"} value={proposal.config.model_source || 'modelscope'} onChange={value => field('model_source', value)} options={[{value:"modelscope",label:"ModelScope"},{value:"huggingface",label:"Hugging Face"}]} width="100%"  /></> : proposal.kind !== 'local_agent' && <TextInput label={"模型 ID"} value={proposal.config.model || ''} onChange={value => field('model', value)}  />}
      {(proposal.kind === 'ai' || proposal.kind === 'cloud_asr') && <>{!!availableModels.length && <Selector label="查询到的模型" placeholder="请选择模型，也可在“模型 ID”中手动填写" value={proposal.config.model || ''} options={availableModels} onChange={value => field('model', value)} width="100%" />}<Button type="button" isLoading={busy === 'models'} isDisabled={!!busy || !proposal.name} onClick={() => void discover()} label={(busy === 'models' ? '正在获取模型列表…' : '获取模型列表')} /><small>可以从列表中选择，也可手动填写模型 ID。使用前请检查是否可用。</small></>}
      {(proposal.kind === 'ai' || proposal.kind === 'cloud_asr' || proposal.kind === 'local_asr' && proposal.config.model_source === 'huggingface') && <Field inputID={credentialID} descriptionID={`${credentialID}-description`} label={proposal.kind === 'local_asr' ? 'Hugging Face Token（选填）' : 'API Key'} description={current?.has_credential && current.kind === proposal.kind && ['endpoint','provider','region','workspace'].every(k => current.config[k] === proposal.config[k]) ? '已保存。服务配置未变时，留空可继续使用。新输入的密钥不会保存在草稿中。' : '密钥不会保存在草稿中，离开后需重新输入。'} width="100%"><div className="resource-credential"><input ref={credentialRef} id={credentialID} className="form-control" aria-describedby={`${credentialID}-description`} autoComplete="off" type={visible ? 'text' : 'password'} onChange={() => { setAvailableModels([]); setCredentialVersion(v => v + 1); }} /><Button type="button" onClick={() => setVisible(!visible)} label={visible ? '隐藏本次输入' : '显示本次输入'} /></div></Field>}
      {(proposal.kind === 'local_asr' || proposal.kind === 'cloud_asr') && <Selector label={"识别语言"} value={proposal.config.language || 'zh'} onChange={value => field('language', value)} options={[{value:"zh",label:"中文"},{value:"en",label:"英语"},{value:"auto",label:"自动识别"}]} width="100%"  />}
      {proposal.kind === 'ai' && <details><summary>高级设置</summary>{([['timeout_seconds', '请求超时（秒）', 300, 30, 3600], ['request_attempts', '连接尝试次数（含首次）', 1, 1, 10], ['retry_delay_seconds', '连接重试间隔（秒）', 3, 0, 60], ['max_tokens', '片段筛选输出上限（Token）', 4096, 512, 32000]] as const).map(([key, label, fallback, min, max]) => <NumberInput label={(label)} min={min} max={max} value={proposal.config[key] === null ? null : Number(proposal.config[key] ?? fallback)} onChange={value => field(key, value)} status={proposal.config[key] === null ? { type: "error", message: "请填写数值" } : undefined} isIntegerOnly hasClear key={key} />)}{previewConfig?.request_profile === 'kimi-k2.6-default' ? <p>该模型使用默认温度，无需设置。</p> : <NumberInput label={"片段筛选温度"} min={0} max={2} step={0.1} value={proposal.config.temperature === null ? null : Number(proposal.config.temperature ?? 0.2)} status={proposal.config.temperature === null ? { type: 'error', message: '请填写数值' } : undefined} onChange={value => field('temperature', value)} hasClear  />}<p>仅在连接尚未建立时重试。请求结果不明或返回内容有误时，不会自动重试。</p></details>}
      {proposal.kind === 'local_agent' && <details><summary>Claude Code 设置</summary><NumberInput label={"执行超时（分钟）"} min={1} max={240} value={proposal.config.command_timeout_minutes === null ? null : Number(proposal.config.command_timeout_minutes ?? 60)} status={proposal.config.command_timeout_minutes === null ? { type: 'error', message: '请填写数值' } : undefined} onChange={value => field('command_timeout_minutes', value)} isIntegerOnly hasClear  /><p>Claude Code 不会获得直接修改项目文件的权限。</p></details>}
      {proposal.kind === 'ai' && <fieldset><legend>用途</legend>{(['analysis', 'review'] as Purpose[]).map(p => <CheckboxInput label={(purposes[p])} value={proposal.config.purposes.includes(p)} onChange={value => field('purposes', value ? [...proposal.config.purposes, p] : proposal.config.purposes.filter(v => v !== p))} key={p} />)}</fieldset>}
      <section><h2>检查是否可用</h2><p>{proposal.kind === 'local_asr' ? '使用内置音频检查本机模型，不会发送你的录像。' : '检查会使用内置测试内容，不会发送你的录像。调用云端模型或 Claude Code 可能产生费用。'}</p><Button type="button" isLoading={busy === 'test'} isDisabled={!!busy || !validationOperation.current && (numericInvalid || !proposal.config.purposes.length)} onClick={() => void test()} label={(busy === 'test' ? '检查中…' : validationOperation.current ? '查询检查结果' : proposal.kind === 'local_asr' ? '检查模型' : '检查是否可用')} /><div role="status">{validation && !verified ? <p>配置已更改，请重新检查。</p> : validation && Object.entries(validation.results).map(([p, v]) => <p key={p}>{purposes[p as Purpose]}：{states[v.state]}{v.code && `（${validationMessage(v.code, proposal.kind)}）`}</p>)}</div></section>
      {current && <section><h2>使用这个模型的项目</h2><ResourceChanges current={current} proposal={proposal} providers={providers} regions={regions} /><p>正在处理、排队中和历史剪辑记录继续使用原配置。</p>{current.projects?.map(p => <p key={p.project_id}>{p.name} · {p.purposes.map(v => purposes[v]).join('、')}</p>)}</section>}
      {!anyReady && !current?.ready && <p>{verified && Object.values(validation.results).some(v => v.state === 'needs_repair') ? '检查未通过，保存后仍不可用。' : '保存后仍需准备或检查，通过后才能使用。'}</p>}
      <div className="resource-actions"><Button type="button" isDisabled={!!busy || unresolved} onClick={() => dirty ? setLeaving(true) : back()} label={"返回"} /><Button type="submit" isLoading={busy === 'save' || busy === 'save-query'} isDisabled={!!busy || !unresolved && (numericInvalid || !!current?.deleted || !!unchanged || !proposal.name.trim() || !proposal.config.purposes.length || !!current && baseRevision.current !== current.revision)} label={(busy === 'save-query' ? '正在查询保存结果…' : busy === 'save' ? '正在保存…' : unresolved ? '查询保存结果' : renamed ? '保存名称' : anyReady ? current ? '保存修改' : '添加模型' : current?.ready ? '保存修改' : '保存模型')} variant="primary" /></div>
    </FormLayout></form>
    {(leaving || blocker.state === 'blocked') && <DialogFrame closeDisabled={!!busy || unresolved} title="离开此页？" description="可以保留已填写的内容，下次继续编辑。密钥不会保存在草稿中，离开后需重新输入。" onClose={() => { setLeaving(false); if (blocker.state === 'blocked') blocker.reset(); }} footer={<><Button onClick={() => { setLeaving(false); if (blocker.state === 'blocked') blocker.reset(); }} label={"继续编辑"} /><Button onClick={() => { localStorage.removeItem(draftKey); clearCredential(); back(); }} isDisabled={!!busy || unresolved} label={"放弃修改"} /><Button onClick={() => { clearCredential(); back(); }} isDisabled={!!busy || unresolved} label={"保留草稿并离开"} variant="primary" /></>}><p>{current ? '已保存的配置不会改变。' : '尚未添加模型。'}</p></DialogFrame>}
  </section>;
}
