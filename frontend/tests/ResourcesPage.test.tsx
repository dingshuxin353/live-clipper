import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { ResourcesPage, type Resource } from '../src/ResourcesPage';
import { installFetchMock, jsonResponse } from './helpers';

const resource: Resource = { resource_id: 'original', name: '课程分析', kind: 'ai', revision: 1,
  config: { provider: 'custom', model: 'course-model', endpoint: 'https://model.test/v1', purposes: ['analysis', 'review'] },
  ready: false, deleted: false, has_credential: true, state: 'pending', validation: {}, projects: [], active_runs: [], failed_runs: [] };
const evidence = { ok: true, validation_id: 'verified', results: { analysis: { state: 'ready' }, review: { state: 'ready' } } };
function show(path: string) { return render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <ResourcesPage /> }], { initialEntries: [path] })} />); }
function mocks(extra = {}) { return installFetchMock({ '/api/resources/original': { ok: true, resource }, '/api/resources/validate': evidence, ...extra }); }
beforeEach(() => localStorage.clear());

it('restores non-secret draft fields without probing or remembering the password', async () => {
  const calls = mocks(); const view = show('/resources/new?draft=draft-one');
  fireEvent.change(await screen.findByLabelText(/模型名称/), { target: { value: '草稿资源' } });
  fireEvent.click(screen.getByRole('combobox', { name: '供应商' }));
  fireEvent.click(await screen.findByRole('option', { name: '自定义兼容服务' }));
  fireEvent.change(screen.getByLabelText(/API Key/), { target: { value: 'private-marker' } });
  await waitFor(() => expect(localStorage.getItem('venus.resource-draft.draft-one')).toContain('草稿资源'));
  expect(JSON.stringify(localStorage)).not.toContain('private-marker');
  expect(calls.some(([path]) => path === '/api/resources/models' || path === '/api/resources/validate')).toBe(false);
  view.unmount(); show('/resources/new?draft=draft-one');
  expect(await screen.findByLabelText(/模型名称/)).toHaveValue('草稿资源');
  expect(screen.getByLabelText(/API Key/)).toHaveValue('');
});

it('promotes unchanged pending content using the explicit purpose evidence', async () => {
  const calls = mocks({ '/api/resources/original': (options?: RequestInit) => jsonResponse({ ok: true, resource: options?.method === 'PATCH' ? { ...resource, revision: 2, ready: true } : resource }) });
  show('/resources/original/edit');
  await screen.findByRole('button', { name: '检查是否可用' });
  expect(screen.getByRole('button', { name: '保存模型' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '检查是否可用' }));
  const save = await screen.findByRole('button', { name: '保存修改' }); await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
  await waitFor(() => expect(calls.some(([, opts]) => opts?.method === 'PATCH')).toBe(true));
  expect(JSON.parse(String(calls.find(([, opts]) => opts?.method === 'PATCH')![1]?.body))).toMatchObject({ expected_revision: 1, validation_id: 'verified' });
});

it('invalidates successful evidence when the model changes', async () => {
  mocks(); show('/resources/original/edit'); fireEvent.click(await screen.findByRole('button', { name: '检查是否可用' }));
  await screen.findByText('内容分析：可用');
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'different-model' } });
  expect(screen.getByText('配置已更改，请重新检查。')).toBeVisible();
});

it('queries the original operation after a lost save response without creating again', async () => {
  let savedId = ''; let writes = 0;
  mocks({ '/api/resources': (options?: RequestInit) => { if (options?.method !== 'POST') return jsonResponse({ ok: true, resources: [] }); writes++; savedId = JSON.parse(String(options?.body)).request_id; return Promise.reject(new Error('lost response')); } });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => savedId && String(input) === `/api/resources/operations/${savedId}` ? jsonResponse({ ok: true, result: resource }) : originalFetch(input, options)));
  show('/resources/new?draft=once');
  fireEvent.change(await screen.findByLabelText(/模型名称/), { target: { value: '只建一次' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型' }));
  await screen.findByRole('heading', { name: resource.name });
  expect(writes).toBe(1); expect(localStorage.getItem('venus.resource-draft.once')).toBeNull();
});


it('queries an uncertain validation before retrying and never sends a second paid request', async () => {
  let operation = ''; let validations = 0;
  mocks({ '/api/resources/validate': (options?: RequestInit) => { operation = JSON.parse(String(options?.body)).request_id; validations++; return Promise.reject(new Error('response lost')); } });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => operation && String(input) === `/api/resources/operations/${operation}` ? jsonResponse({ ok: true, result: { state: 'running' } }) : originalFetch(input, options)));
  show('/resources/original/edit'); fireEvent.click(await screen.findByRole('button', { name: '检查是否可用' }));
  await screen.findByText('无法连接后台服务。'); fireEvent.click(screen.getByRole('button', { name: '查询检查结果' }));
  await screen.findByText(/暂时无法确认上次检查结果/); expect(validations).toBe(1);
  expect(JSON.stringify(localStorage)).toContain(operation);
});


it('reads only the current password, clears it on target change, and invalidates evidence', async () => {
  const calls = mocks(); show('/resources/original/edit');
  const key = await screen.findByLabelText('API Key');
  fireEvent.change(key, { target: { value: 'synthetic-only-one' } });
  expect(key).not.toHaveAttribute('value');
  fireEvent.click(screen.getByRole('button', { name: '显示本次输入' }));
  expect(key).toHaveAttribute('type', 'text');
  fireEvent.change(key, { target: { value: 'synthetic-only-two' } });
  fireEvent.click(screen.getByRole('button', { name: '检查是否可用' }));
  await screen.findByText('内容分析：可用');
  const body = JSON.parse(String(calls.find(([path]) => path === '/api/resources/validate')![1]?.body));
  expect(body.credential).toBe('synthetic-only-two');
  fireEvent.change(key, { target: { value: '' } });
  expect(screen.getByText('配置已更改，请重新检查。')).toBeVisible();
  fireEvent.change(key, { target: { value: 'synthetic-only-three' } });
  fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://different.test/v1' } });
  expect(key).toHaveValue(''); expect(JSON.stringify(localStorage)).not.toContain('synthetic-only');
});

it('keeps a cleared numeric field empty and prevents saving until refilled', async () => {
  const calls = mocks(); show('/resources/original/edit');
  await screen.findByLabelText('模型 ID');
  fireEvent.click(screen.getByText('高级设置'));
  const timeout = screen.getByRole('spinbutton', { name: '请求超时（秒）' });
  fireEvent.change(timeout, { target: { value: '' } }); fireEvent.blur(timeout);
  expect(timeout).toHaveValue(null);
  expect(screen.getByRole('button', { name: '保存模型' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '检查是否可用' })).toBeDisabled();
  expect(JSON.parse(localStorage.getItem('venus.resource-draft.original')!).proposal.config.timeout_seconds).toBeNull();
  fireEvent.change(timeout, { target: { value: '60' } }); fireEvent.blur(timeout);
  expect(screen.getByRole('button', { name: '保存模型' })).toBeEnabled();
  expect(calls.some(([, opts]) => opts?.method === 'PATCH')).toBe(false);
});

it('blocks closing and repeat deletion while an outcome is unknown, then queries the original operation', async () => {
  let operation = ''; let deletes = 0;
  mocks({
    '/api/resources/original': (options?: RequestInit) => {
      if (options?.method !== 'DELETE') return jsonResponse({ resource });
      operation = JSON.parse(String(options.body)).request_id; deletes++; return Promise.reject(new Error('lost response'));
    },
    '/api/resources/original/delete-preview': { preview: { can_delete: true, resource, projects: [], active_runs: [], failed_runs: [{ run_id: 'failed', project_id: 'p' }], tasks: [], model_files: { can_clean: false, shared: false, bytes: 0 } } },
  });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => operation && String(input) === `/api/resources/operations/${operation}` ? jsonResponse({ result: { cleanup_state: 'completed', cleanup_errors: [] } }) : originalFetch(input, options)));
  show('/resources/original'); fireEvent.click(await screen.findByRole('button', { name: '移除模型' }));
  const dialog = await screen.findByRole('alertdialog');
  const confirm = await screen.findByRole('checkbox', { name: '我知道这些失败记录将无法使用此模型继续处理' });
  expect(confirm).not.toBeChecked();
  const remove = () => Array.from(dialog.querySelectorAll('button')).find(button => button.textContent === '移除模型')!;
  expect(remove()).toBeDisabled();
  fireEvent.keyDown(document.activeElement!, { key: 'Enter' }); expect(deletes).toBe(0);
  fireEvent.click(confirm); fireEvent.click(remove());
  await screen.findByRole('button', { name: '查询移除结果' });
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
  fireEvent.keyDown(dialog, { key: 'Escape' }); expect(dialog).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查询移除结果' }));
  await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()); expect(deletes).toBe(1);
});

it('recovers the original saved model while retaining newer edits and never persists keys', async () => {
  const writes: Array<Record<string, any>> = []; let queryCount = 0;
  mocks({ '/api/resources/original': (options?: RequestInit) => {
    if (options?.method !== 'PATCH') return jsonResponse({ resource });
    writes.push(JSON.parse(String(options.body)));
    if (writes.length === 1) return new Response('response lost after commit', { status: 200 });
    return jsonResponse({ resource: { ...resource, name: writes[1].proposal.name, revision: 3 } });
  } });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes('/resources/operations/')) {
      queryCount++;
      return queryCount === 1 ? Promise.reject(new Error('query unavailable')) : jsonResponse({ result: { ...resource, name: '原提交', revision: 2 } });
    }
    return originalFetch(input, options);
  }));
  show('/resources/original/edit');
  fireEvent.change(await screen.findByLabelText(/模型名称/), { target: { value: '原提交' } });
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'memory-only-secret' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型' }));
  await screen.findByRole('button', { name: '查询保存结果' });
  fireEvent.change(screen.getByLabelText(/模型名称/), { target: { value: '新的修改' } });
  fireEvent.click(screen.getByRole('button', { name: '查询保存结果' }));
  await screen.findByText(/上次提交已保存/);
  expect(writes).toHaveLength(1); expect(screen.getByLabelText(/模型名称/)).toHaveValue('新的修改');
  expect(JSON.stringify(localStorage)).not.toContain('memory-only-secret');
  fireEvent.click(screen.getByRole('button', { name: '保存模型' }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1]).toMatchObject({ proposal: { name: '新的修改' }, expected_revision: 2 });
  expect(writes[1].request_id).not.toBe(writes[0].request_id);
});


it.each([true, false])('keeps historical hidden configuration %s unchanged while editing', async hidden => {
  const historical = { ...resource, kind: 'local_agent', config: { purposes: ['review'], include_review_package_inline: hidden, command_timeout_minutes: 60 } };
  const calls = mocks({ '/api/resources/original': (options?: RequestInit) => jsonResponse({ resource: options?.method === 'PATCH' ? { ...historical, name: '新名称', revision: 2 } : historical }) });
  show('/resources/original/edit');
  fireEvent.change(await screen.findByLabelText(/模型名称/), { target: { value: '新名称' } });
  expect(screen.queryByText('内联审阅材料')).not.toBeInTheDocument();
  expect(screen.queryByText('include_review_package_inline')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保存名称' }));
  await waitFor(() => expect(calls.some(([, options]) => options?.method === 'PATCH')).toBe(true));
  const body = JSON.parse(String(calls.find(([, options]) => options?.method === 'PATCH')![1]?.body));
  expect(body.proposal.config.include_review_package_inline).toBe(hidden);
});

it('does not apply an older preparation result to a newer configuration', async () => {
  const local = { ...resource, kind: 'local_asr', revision: 2, config: { model: 'local-model', purposes: ['asr'] }, preparation: { id: 'old-job', status: 'running' }, model_files: { state: 'installed', installed_bytes: 100, partial_bytes: 0, bytes_downloaded: 100, bytes_total: 100 } };
  mocks({ '/api/resources/original': { resource: local }, '/api/jobs/old-job': { job: { status: 'succeeded', result: { resource_id: 'original', revision: 1 } } } });
  show('/resources/original');
  expect(await screen.findByText('模型文件准备任务已结束，当前配置仍需检查。')).toBeVisible();
  expect(screen.queryByText('模型可以使用了')).not.toBeInTheDocument();
  expect(screen.queryByText('本次准备已完成')).not.toBeInTheDocument();
});
