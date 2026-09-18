import { useEffect, useState } from 'react';
import { Button } from '@astryxdesign/core/Button';
import iconLicense from './ui/remix-license.txt?url';
import { copyText } from './copy';
import { PageHeading } from './workbench-shared';

export interface ApplicationSettings {
  ok: boolean;
  storage: { work_dir: string };
}
type ApplicationInfo = Awaited<ReturnType<NonNullable<NonNullable<Window['liveClipperShell']>['getApplicationInfo']>>>;

function DataDirectory({ title, description, path, id, notify }: { title: string; description: string; path: string; id: 'app' | 'work'; notify(message: string): void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const act = async (open: boolean) => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (open) await window.liveClipperShell!.openDataDirectory!(id);
      else { await copyText(path); notify('路径已复制'); }
    } catch { setError(open ? '无法打开文件夹，请稍后重试。' : '复制失败，请手动选择路径复制。'); }
    finally { setBusy(false); }
  };
  return <div className="settings-directory"><h3>{title}</h3><p>{description}</p><p className="settings-path">{path}</p>
    <div className="resource-actions"><Button label="复制路径" isDisabled={busy} onClick={() => void act(false)} />{window.liveClipperShell?.openDataDirectory && <Button label="在 Finder 中打开" isDisabled={busy} onClick={() => void act(true)} />}</div>
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}

export function Settings({ initial, loading, error, retry, notify }: { initial?: ApplicationSettings; loading?: boolean; error?: string; retry?(): void; notify(message: string): void }) {
  const shell = window.liveClipperShell;
  const [info, setInfo] = useState<ApplicationInfo>();
  const [infoError, setInfoError] = useState('');
  const [infoBusy, setInfoBusy] = useState(Boolean(shell?.getApplicationInfo));
  const [checking, setChecking] = useState(false);
  const [updateError, setUpdateError] = useState('');
  const [copyError, setCopyError] = useState('');
  const readInfo = async () => {
    if (!shell?.getApplicationInfo) return;
    setInfoBusy(true); setInfoError('');
    try { setInfo(await shell.getApplicationInfo()); }
    catch { setInfoError('无法读取应用信息，请重试。'); }
    finally { setInfoBusy(false); }
  };
  useEffect(() => { void readInfo(); }, []);
  const check = async () => {
    if (checking) return;
    setChecking(true); setUpdateError('');
    try {
      const result = await shell!.checkForUpdates!();
      if (!result.ok) setUpdateError(result.message || '暂时无法检查更新，请稍后重试。');
    }
    catch { setUpdateError('暂时无法检查更新，请稍后重试。'); }
    finally { setChecking(false); }
  };
  const copyEnvironment = async () => {
    setCopyError('');
    try {
      await copyText(`Venus 版本：${info?.version || '未读取'}\n运行方式：${shell ? '桌面应用' : '浏览器'}\n系统：${info?.platform || '未读取'}\n应用架构：${info?.arch || '未读取'}`);
      notify('应用信息已复制');
    } catch { setCopyError('复制失败，请重试。'); }
  };
  const workDir = initial?.storage.work_dir;
  const sameDirectory = Boolean(workDir && workDir === info?.app_home);
  const unread = shell ? infoBusy ? "正在读取…" : "未读取" : "仅桌面应用可读取";
  const platform = info?.platform ? ({ darwin: "macOS", win32: "Windows", linux: "Linux" } as Record<string, string>)[info.platform] ?? info.platform : unread;
  return <section className="page settings-page"><PageHeading title="设置" />
    <section className="settings-section" aria-labelledby="settings-storage"><h2 id="settings-storage">数据位置</h2>
      {!info?.app_home && <div className="settings-directory"><h3>应用数据</h3>{shell ? <>{infoBusy && <p role="status">正在读取数据位置…</p>}{infoError && <p role="alert" className="form-error">{infoError}</p>}{!infoBusy && <Button label="重新读取" onClick={() => void readInfo()} />}</> : <p>请在 Venus 桌面应用中查看应用数据位置。</p>}</div>}
      {info?.app_home && <DataDirectory title={sameDirectory ? '应用数据与处理文件' : '应用数据'} description={sameDirectory ? '保存 Venus 设置、记录和处理过程中生成的文件。录像和成片的位置可在项目设置中查看。' : '保存 Venus 设置和记录。'} path={info.app_home} id="app" notify={notify} />}
      {workDir && !sameDirectory && <DataDirectory title="处理文件" description="保存处理过程中生成的文件。录像和成片的位置可在项目设置中查看。" path={workDir} id="work" notify={notify} />}
      {!workDir && <div className="settings-directory"><h3>处理文件</h3>{loading && <p role="status">正在读取数据位置…</p>}{error && <p className="form-error" role="alert">无法读取数据位置，请重试。</p>}{!loading && retry && <Button label="重新读取" onClick={retry} />}</div>}
      {workDir && error && <div><p role="alert" className="form-error">无法刷新处理文件位置，当前显示的是上次读取的位置。</p><Button label="重新读取" onClick={retry} isDisabled={loading} /></div>}
      {info?.app_home && infoError && <div><p role="alert" className="form-error">无法刷新应用数据位置，当前显示的是上次读取的位置。</p><Button label="重新读取" onClick={() => void readInfo()} isDisabled={infoBusy} /></div>}
    </section>
    <section className="settings-section" aria-labelledby="settings-about"><h2 id="settings-about">关于 Venus</h2>
      {shell ? <><p>当前版本：{info?.version || (infoBusy ? '正在读取…' : '未读取')}</p>{infoError && <p className="form-error" role="alert">{infoError}</p>}{(!info || infoError) && <Button label={infoBusy ? '正在读取…' : '重新读取'} isDisabled={infoBusy} onClick={() => void readInfo()} />}
        {shell.checkForUpdates && <Button label={checking ? '检查中…' : '检查更新'} isDisabled={checking} onClick={() => void check()} />}{updateError && <p className="form-error" role="alert">{updateError}</p>}
      </> : <p>请在 Venus 桌面应用中查看版本和检查更新。</p>}
      <div><h3>开源许可</h3><a href={iconLicense} target="_blank" rel="noreferrer">Remix Icon 许可</a></div>
    </section>
    <details className="settings-section"><summary>问题排查</summary><dl className="summary-list">
      <div><dt>运行方式</dt><dd>{shell ? '桌面应用' : '浏览器'}</dd></div><div><dt>系统 / 应用架构</dt><dd>{platform} / {info?.arch || unread}</dd></div><div><dt>访问地址</dt><dd>{window.location.host}</dd></div>
    </dl><Button label="复制应用信息" onClick={() => void copyEnvironment()} />{copyError && <p className="form-error" role="alert">{copyError}</p>}</details>
  </section>;
}
