import { Selector } from '@astryxdesign/core/Selector';
import { Button } from '@astryxdesign/core/Button';
import { useLocation, useNavigate } from 'react-router-dom';
import type { FormOptionsPayload } from './project-dto';
import type { ProjectDraft } from './workbench-shared';

export function ResourceAssignment({ draft, options, change, projectId, disabled = false }: { draft: ProjectDraft; options: FormOptionsPayload; change(next: ProjectDraft): void; projectId?: string; disabled?: boolean }) {
  const navigate = useNavigate(); const location = useLocation();
  const open = (purpose: string, resource?: string) => {
    if (disabled) return;
    const query = new URLSearchParams({ origin: projectId ? 'project' : 'new-project', purpose });
    if (projectId) query.set('project', projectId);
    const draftId = new URLSearchParams(location.search).get('draft'); if (!projectId && draftId) query.set('sourceDraft', draftId);
    navigate(`/resources/${resource || 'new'}?${query}`);
  };
  return <section className="resource-assignment"><h2>处理资源</h2>{(['asr', 'analysis', 'review'] as const).map(purpose => {
    const key = `${purpose}Ref` as 'asrRef' | 'analysisRef' | 'reviewRef'; const value = draft[key]; const label = { asr: '语音识别', analysis: '内容分析', review: 'AI 审阅' }[purpose];
    const choices = options.resources.filter(r => r.purposes.includes(purpose));
    const resolved = purpose === 'review' && value === 'reuse_analysis' ? draft.analysisRef : value;
    const current = options.resources.find(r => r.resource_id === resolved);
    return <div className="resource-assignment-row" key={purpose}><Selector isDisabled={disabled} label={(label)} value={value} onChange={value => change({ ...draft, [key]: value })} options={[
      { value: '', label: '不分配' },
      ...(purpose === 'review' ? [{ value: 'reuse_analysis', label: '复用本项目的内容分析资源' }] : []),
      ...(value && value !== 'reuse_analysis' && !choices.some(r => r.resource_id === value) ? [{ value, label: '原资源不可用，请重新选择' }] : []),
      ...choices.map(r => ({ value: r.resource_id, label: `${r.display_name} · ${r.version || ''} · ${r.ready_purposes.includes(purpose) ? '可用' : '待准备'}` })),
    ]} width="100%"  /><p role="status">{current ? `${current.display_name} · ${current.version || ''} · ${current.ready_purposes.includes(purpose) ? '可用' : '尚未通过此用途验证'}` : `缺少${label}资源`}</p><div className="resource-actions">{current && <Button isDisabled={disabled} type="button" onClick={() => open(purpose, current.resource_id)} label={"查看资源"} />}<Button isDisabled={disabled} type="button" onClick={() => open(purpose)} label={"添加资源"} /></div>{!choices.length && <p>还没有可用于{label}的资源。</p>}</div>;
  })}<p>分配只影响这个项目的新记录。保存项目设置后生效，不会额外扫描或重跑；已启用项目就绪后按原计划继续，暂停项目保持暂停。</p></section>;
}
