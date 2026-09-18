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
  return <section className="resource-assignment"><h2>模型与工具</h2>{(['asr', 'analysis', 'review'] as const).map(purpose => {
    const key = `${purpose}Ref` as 'asrRef' | 'analysisRef' | 'reviewRef'; const value = draft[key]; const label = { asr: '语音识别', analysis: '内容分析', review: '片段筛选' }[purpose];
    const choices = options.resources.filter(r => r.purposes.includes(purpose));
    const resolved = purpose === 'review' && value === 'reuse_analysis' ? draft.analysisRef : value;
    const current = options.resources.find(r => r.resource_id === resolved);
    return <div className="resource-assignment-row" key={purpose}><Selector isDisabled={disabled} label={(label)} value={value} onChange={value => change({ ...draft, [key]: value })} options={[
      { value: '', label: '暂不选择' },
      ...(purpose === 'review' ? [{ value: 'reuse_analysis', label: '使用内容分析的模型' }] : []),
      ...(value && value !== 'reuse_analysis' && !choices.some(r => r.resource_id === value) ? [{ value, label: '原配置不可用，请重新选择' }] : []),
      ...choices.map(r => ({ value: r.resource_id, label: [r.display_name, r.version, r.ready_purposes.includes(purpose) ? '可用' : '不可用'].filter(Boolean).join(' · ') })),
    ]} width="100%"  /><small>{{ asr: '把语音转成文字', analysis: '找出候选片段', review: '选出最终要剪辑的片段' }[purpose]}</small><p role="status">{current ? [current.display_name, current.version, current.ready_purposes.includes(purpose) ? '可用' : `尚未通过“${label}”检查`].filter(Boolean).join(' · ') : value === 'reuse_analysis' && !draft.analysisRef ? '请先选择内容分析模型。' : value && value !== 'reuse_analysis' ? '原配置不可用，请重新选择' : purpose === 'review' ? '尚未选择片段筛选的模型或工具。' : `尚未选择${label}模型。`}</p><div className="resource-actions">{current && <Button isDisabled={disabled} type="button" onClick={() => open(purpose, current.resource_id)} label={"查看模型"} />}<Button isDisabled={disabled} type="button" onClick={() => open(purpose)} label={"添加模型"} /></div>{!choices.length && <p>{purpose === 'review' ? '还没有用于片段筛选的模型或工具，请先添加。' : `还没有${label}模型，请先添加。`}</p>}</div>;
  })}<p>{projectId ? '保存后，这些选择只用于新建的剪辑记录，不会改变已有记录，也不会额外发起扫描或重新处理。已启用项目准备好后按原计划继续，暂停项目保持暂停。' : '这些选择将在创建项目时生效。是否开始扫描，取决于是否启用项目及前一步的扫描设置。'}</p></section>;
}
