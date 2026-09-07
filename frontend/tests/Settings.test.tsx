import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Settings, type ApplicationSettings } from '../src/Settings';

const initial: ApplicationSettings = {
  ok: true, revision: 'revision-1', storage: { work_dir: '/isolated/work', workspace_root: '/isolated' }, connection: { host: '127.0.0.1', port: 8765 },
  config: { paths: { glossary_path: '/isolated/glossary.txt' }, scheduler: { timezone: 'Asia/Shanghai', tick_seconds: 30 }, service: { stuck_after_minutes: 30 }, review_automation: { timeout_minutes: 60 }, review_automation_model: { max_candidates: 40 } },
};
it('exposes only consumed application controls and links to resource and project ownership', () => {
  render(<MemoryRouter><Settings initial={initial} notify={vi.fn()} /></MemoryRouter>);
  expect(screen.getByLabelText('术语表路径')).toHaveValue('/isolated/glossary.txt');
  expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText('模型')).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '管理资源' })).toHaveAttribute('href', '/resources');
  expect(screen.getByRole('button', { name: '保存应用设置' })).toBeDisabled();
});
it('submits the original revision and preserves entered values after a conflict', async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ ok: false, message: '应用设置已更新，请重新读取后比较' }), { status: 409 }));
  vi.stubGlobal('fetch', fetch);
  render(<MemoryRouter><Settings initial={initial} notify={vi.fn()} /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText('每次审阅候选上限'), { target: { value: '45' } });
  fireEvent.click(screen.getByRole('button', { name: '保存应用设置' }));
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  expect(JSON.parse((fetch.mock.calls as unknown as Array<[string, RequestInit]>)[0][1].body as string).expected_revision).toBe('revision-1');
  expect(await screen.findByRole('alert')).toHaveTextContent('应用设置已更新');
  expect(screen.getByLabelText('每次审阅候选上限')).toHaveValue(45);
});
