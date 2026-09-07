import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import iconLicense from './ui/remix-license.txt?url';
import { post } from './api';
import { PageHeading } from './workbench-shared';

export interface ApplicationSettings {
  ok: boolean; revision: string;
  config: Record<string, Record<string, string | number>>;
  storage: { workspace_root: string; work_dir: string };
  connection: { host: string; port: number };
}
const fields = [
  ['paths', 'glossary_path', '术语表路径', 'text'],
  ['scheduler', 'timezone', '新项目默认时区', 'text'],
  ['scheduler', 'tick_seconds', '调度检查间隔（秒）', 'number', 5, 300],
  ['service', 'stuck_after_minutes', '处理无响应检查（分钟）', 'number', 1, 1440],
  ['review_automation', 'timeout_minutes', '审阅请求超时（分钟）', 'number', 1, 240],
  ['review_automation_model', 'max_candidates', '每次审阅候选上限', 'number', 1, 200],
] as const;

export function Settings({ initial, notify }: { initial: ApplicationSettings; notify(message: string): void }) {
  const [base, setBase] = useState(initial); const [draft, setDraft] = useState(initial.config);
  const observedRevision = useRef(initial.revision); const [remoteChanged, setRemoteChanged] = useState(false);
  useEffect(() => { if (initial.revision !== observedRevision.current) { observedRevision.current = initial.revision; setRemoteChanged(initial.revision !== base.revision); } }, [initial, base.revision]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const save = async () => {
    setBusy(true); setError('');
    try { const result = await post<ApplicationSettings>('/api/config', { config: draft, expected_revision: base.revision }); setBase(result); setDraft(result.config); setRemoteChanged(false); notify('应用设置已保存'); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="page"><PageHeading title="应用设置" description="资源配置归资源页，项目的来源、定时与输出归项目设置。" />
    {error && <p role="alert">{error}</p>}
    {remoteChanged && <p role="status">设置已有新修订。当前输入保留，请比较后再保存。<button className="button" onClick={() => { setBase(initial); setDraft(initial.config); setRemoteChanged(false); setError(''); }}>重新读取，放弃当前输入</button></p>}
    <form className="form-layout resource-form" onSubmit={event => { event.preventDefault(); void save(); }}>
      {fields.map(([section, key, label, type, min, max]) => <label key={key}>{label}<input className="form-control" type={type} min={min} max={max} required value={draft[section][key]} onChange={e => setDraft({ ...draft, [section]: { ...draft[section], [key]: type === 'number' ? Number(e.target.value) : e.target.value } })} /></label>)}
      <p>时区用于新项目。审阅策略在新建处理记录时固定，已有记录保留原值。</p><button className="button primary" disabled={busy || JSON.stringify(base.config) === JSON.stringify(draft)}>{busy ? '正在保存…' : '保存应用设置'}</button>
    </form>
    <p><a href={iconLicense} target="_blank" rel="noreferrer">Remix Icon 许可</a></p><section><h2>存储与连接</h2><dl className="summary-list"><div><dt>工作目录</dt><dd>{base.storage.work_dir}</dd></div><div><dt>工作区</dt><dd>{base.storage.workspace_root || '应用默认位置'}</dd></div><div><dt>当前连接</dt><dd>{window.location.host}</dd></div></dl></section>
    <div className="resource-actions"><Link className="button" to="/resources">管理资源</Link><Link className="button" to="/projects">项目设置</Link></div>
  </section>;
}
