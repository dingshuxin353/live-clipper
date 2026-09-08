import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Settings, type ApplicationSettings } from '../src/Settings';

const initial: ApplicationSettings = { ok: true, storage: { work_dir: '/isolated/work' } };
it('shows readonly browser information without desktop actions or configuration fields', async () => {
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  render(<Settings initial={initial} notify={vi.fn()} />);
  expect(screen.getByText('/isolated/work')).toBeVisible();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '检查更新' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '在访达中打开' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '保存应用设置' })).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: '管理资源' })).not.toBeInTheDocument();
  expect(screen.getByText('浏览器访问，请在 Venus 桌面应用中查看版本和更新')).toBeVisible();
  expect(screen.getByText('问题排查').parentElement).not.toHaveAttribute('open');
  fireEvent.click(screen.getByRole('button', { name: '复制路径' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('/isolated/work'));
  expect(screen.getByRole('link', { name: 'Remix Icon 许可' }).getAttribute('href')).not.toMatch(/^https?:/);
});
it('keeps desktop information on backend failure and retries locally; copies only the whitelist', async () => {
  const retry = vi.fn(); const write = vi.fn(async () => ({ ok: true as const }));
  const check = vi.fn().mockResolvedValue({ ok: false });
  window.liveClipperShell = {
    getApplicationInfo: vi.fn().mockResolvedValue({ version: '1.0.2', app_home: '/private/secret-home', platform: 'darwin', arch: 'arm64', secret: 'do-not-copy' }),
    checkForUpdates: check, writeClipboardText: write,
  };
  render(<Settings error="private service error" retry={retry} notify={vi.fn()} />);
  expect(await screen.findByText('应用版本：1.0.2')).toBeVisible();
  expect(check).not.toHaveBeenCalled();
  expect(screen.queryByText('private service error')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重新读取数据位置' })); expect(retry).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: '检查更新' }));
  expect(await screen.findByText('暂时无法检查更新，请稍后重试。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '检查更新' }));
  await waitFor(() => expect(check).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByText('问题排查'));
  fireEvent.click(screen.getByRole('button', { name: '复制环境信息' }));
  await waitFor(() => expect(write).toHaveBeenCalledWith('应用版本：1.0.2\n运行方式：桌面\n系统：darwin\n架构：arm64'));
});
it('merges identical paths and opens by directory identifier; in-flight updates cannot be repeated', async () => {
  let finish!: (value: { ok: boolean }) => void;
  const check = vi.fn(() => new Promise<{ ok: boolean }>(resolve => { finish = resolve; }));
  const open = vi.fn().mockRejectedValue(new Error('private path'));
  window.liveClipperShell = { getApplicationInfo: vi.fn().mockResolvedValue({ version: '1.0.2', app_home: '/isolated/work', platform: 'darwin', arch: 'arm64' }), openDataDirectory: open, checkForUpdates: check };
  render(<Settings initial={initial} notify={vi.fn()} />);
  await screen.findByText('应用数据与处理工作目录');
  expect(screen.getAllByText('/isolated/work')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '在访达中打开' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('目录不可用');
  expect(open).toHaveBeenCalledWith('app');
  fireEvent.click(screen.getByRole('button', { name: '检查更新' }));
  expect(screen.getByRole('button', { name: '检查中…' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '检查中…' }));
  expect(check).toHaveBeenCalledOnce(); finish({ ok: true });
  await waitFor(() => expect(screen.getByRole('button', { name: '检查更新' })).toBeEnabled());
});
