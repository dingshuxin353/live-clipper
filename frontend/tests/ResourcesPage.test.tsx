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
  fireEvent.change(await screen.findByLabelText('资源名称'), { target: { value: '草稿资源' } });
  fireEvent.change(screen.getByLabelText('供应商'), { target: { value: 'custom' } });
  fireEvent.change(screen.getByLabelText(/API Key/), { target: { value: 'private-marker' } });
  await waitFor(() => expect(localStorage.getItem('venus.resource-draft.draft-one')).toContain('草稿资源'));
  expect(JSON.stringify(localStorage)).not.toContain('private-marker');
  expect(calls.some(([path]) => path === '/api/resources/models' || path === '/api/resources/validate')).toBe(false);
  view.unmount(); show('/resources/new?draft=draft-one');
  expect(await screen.findByLabelText('资源名称')).toHaveValue('草稿资源');
  expect(screen.getByLabelText(/API Key/)).toHaveValue('');
});

it('promotes unchanged pending content using the explicit purpose evidence', async () => {
  const calls = mocks({ '/api/resources/original': (options?: RequestInit) => jsonResponse({ ok: true, resource: options?.method === 'PATCH' ? { ...resource, revision: 2, ready: true } : resource }) });
  show('/resources/original/edit');
  await screen.findByRole('button', { name: '验证用途' });
  expect(screen.getByRole('button', { name: '保存为待准备资源' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '验证用途' }));
  const save = await screen.findByRole('button', { name: '保存资源更改' }); await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
  await waitFor(() => expect(calls.some(([, opts]) => opts?.method === 'PATCH')).toBe(true));
  expect(JSON.parse(String(calls.find(([, opts]) => opts?.method === 'PATCH')![1]?.body))).toMatchObject({ expected_revision: 1, validation_id: 'verified' });
});

it('invalidates successful evidence when the model changes', async () => {
  mocks(); show('/resources/original/edit'); fireEvent.click(await screen.findByRole('button', { name: '验证用途' }));
  await screen.findByText('内容分析：可用');
  fireEvent.change(screen.getByLabelText('模型标识'), { target: { value: 'different-model' } });
  expect(screen.getByText('配置已更改，请重新验证。')).toBeVisible();
});

it('queries the original operation after a lost save response without creating again', async () => {
  let savedId = ''; let writes = 0;
  mocks({ '/api/resources': (options?: RequestInit) => { if (options?.method !== 'POST') return jsonResponse({ ok: true, resources: [] }); writes++; savedId = JSON.parse(String(options?.body)).request_id; return Promise.reject(new Error('lost response')); } });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => savedId && String(input) === `/api/resources/operations/${savedId}` ? jsonResponse({ ok: true, result: resource }) : originalFetch(input, options)));
  show('/resources/new?draft=once');
  fireEvent.change(await screen.findByLabelText('资源名称'), { target: { value: '只建一次' } });
  fireEvent.click(screen.getByRole('button', { name: '保存为待准备资源' }));
  await screen.findByRole('heading', { name: resource.name });
  expect(writes).toBe(1); expect(localStorage.getItem('venus.resource-draft.once')).toBeNull();
});


it('queries an uncertain validation before retrying and never sends a second paid request', async () => {
  let operation = ''; let validations = 0;
  mocks({ '/api/resources/validate': (options?: RequestInit) => { operation = JSON.parse(String(options?.body)).request_id; validations++; return Promise.reject(new Error('response lost')); } });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => operation && String(input) === `/api/resources/operations/${operation}` ? jsonResponse({ ok: true, result: { state: 'running' } }) : originalFetch(input, options)));
  show('/resources/original/edit'); fireEvent.click(await screen.findByRole('button', { name: '验证用途' }));
  await screen.findByText('网络连接失败'); fireEvent.click(screen.getByRole('button', { name: '验证用途' }));
  await screen.findByText(/不会重复发起验证/); expect(validations).toBe(1);
  expect(JSON.stringify(localStorage)).toContain(operation);
});
