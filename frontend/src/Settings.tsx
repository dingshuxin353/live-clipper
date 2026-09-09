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
    } catch { setError(open ? '目录不可用或打开失败，请检查目录后重试。' : '复制失败，请重试或手动选择路径复制。'); }
    finally { setBusy(false); }
  };
  return <div className="settings-directory"><h3>{title}</h3><p>{description}</p><p className="settings-path">{path}</p>
    <div className="resource-actions"><Button label="复制路径" isDisabled={busy} onClick={() => void act(false)} />{window.liveClipperShell?.openDataDirectory && <Button label="在访达中打开" isDisabled={busy} onClick={() => void act(true)} />}</div>
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}

export function Settings({ initial, loading, error, retry, notify }: { initial?: ApplicationSettings; loading?: boolean; error?: string; retry?(): void; notify(message: string): void }) {
  const shell = window.liveClipperShell;
  const [info, setInfo] = useState<ApplicationInfo>();
  const [infoError, setInfoError] = useState('');
  const [infoBusy, setInfoBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [updateError, setUpdateError] = useState('');
  const [copyError, setCopyError] = useState('');
  const readInfo = async () => {
    if (!shell?.getApplicationInfo) return;
    setInfoBusy(true); setInfoError('');
    try { setInfo(await shell.getApplicationInfo()); }
    catch { setInfoError('桌面应用信息未获取，请重试。'); }
    finally { setInfoBusy(false); }
  };
  useEffect(() => { void readInfo(); }, []);
  const check = async () => {
    if (checking) return;
    setChecking(true); setUpdateError('');
    try { if (!(await shell!.checkForUpdates!()).ok) throw new Error(); }
    catch { setUpdateError('暂时无法检查更新，请稍后重试。'); }
    finally { setChecking(false); }
  };
  const copyEnvironment = async () => {
    setCopyError('');
    try {
      await copyText(`应用版本：${info?.version || '未获取'}\n运行方式：${shell ? '桌面' : '浏览器'}\n系统：${info?.platform || '未获取'}\n架构：${info?.arch || '未获取'}`);
      notify('环境信息已复制');
    } catch { setCopyError('复制失败，请重试。'); }
  };
  const workDir = initial?.storage.work_dir;
  const sameDirectory = Boolean(workDir && workDir === info?.app_home);
  return <section className="page settings-page"><PageHeading title="设置" description="查看数据位置、应用版本和问题排查信息" />
    <section className="settings-section" aria-labelledby="settings-storage"><h2 id="settings-storage">数据位置</h2>
      {info?.app_home && <DataDirectory title={sameDirectory ? '应用数据与处理工作目录' : '应用数据目录'} description={sameDirectory ? '保存应用配置、本机数据和处理过程文件；录像与成片位置在对应项目中查看' : '保存应用配置与本机数据'} path={info.app_home} id="app" notify={notify} />}
      {workDir && !sameDirectory && <DataDirectory title="处理工作目录" description="保存处理过程文件；录像与成片位置在对应项目中查看" path={workDir} id="work" notify={notify} />}
      {loading && !initial && <p role="status">正在读取数据位置…</p>}
      {error && <div><p className="form-error" role="alert">数据位置未获取，请重试。</p><Button label="重新读取数据位置" onClick={retry} isDisabled={loading} /></div>}
    </section>
    <section className="settings-section" aria-labelledby="settings-about"><h2 id="settings-about">关于 Venus</h2>
      {shell ? <><p>应用版本：{info?.version || '未获取'}</p>{infoError && <p className="form-error" role="alert">{infoError}</p>}{(!info || infoError) && <Button label={infoBusy ? '正在读取…' : '重新读取应用信息'} isDisabled={infoBusy} onClick={() => void readInfo()} />}
        {shell.checkForUpdates && <Button label={checking ? '检查中…' : '检查更新'} isDisabled={checking} onClick={() => void check()} />}{updateError && <p className="form-error" role="alert">{updateError}</p>}
      </> : <p>浏览器访问，请在 Venus 桌面应用中查看版本和更新</p>}
      <div><h3>开源许可</h3><a href={iconLicense} target="_blank" rel="noreferrer">Remix Icon 许可</a></div>
    </section>
    <details className="settings-section"><summary>问题排查</summary><dl className="summary-list">
      <div><dt>运行方式</dt><dd>{shell ? '桌面' : '浏览器'}</dd></div><div><dt>系统 / 架构</dt><dd>{info?.platform || '未获取'} / {info?.arch || '未获取'}</dd></div><div><dt>当前连接</dt><dd>{window.location.host}</dd></div>
    </dl><Button label="复制环境信息" onClick={() => void copyEnvironment()} />{copyError && <p className="form-error" role="alert">{copyError}</p>}</details>
  </section>;
}
