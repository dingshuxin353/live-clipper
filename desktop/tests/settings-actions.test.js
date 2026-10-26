const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { registerDesktopCapabilities } = require('../desktop-capabilities');
const { createUpdateFlow } = require('../update-flow');
const { createDataDirectoryActions, createRuntimeState } = require('../runtime-state');

test('known data directories resolve in their owning process and never create missing targets', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'venus-settings-'));
  const work = path.join(home, 'custom-work'); fs.mkdirSync(work);
  const opened = []; const runtime = createRuntimeState();
  const actions = createDataDirectoryActions({ appHome: home, runtime, shell: { openPath: async p => { opened.push(p); return ''; } }, client: { request: async endpoint => { assert.equal(endpoint, '/api/config'); return { storage: { work_dir: work } }; } } });
  try {
    await actions.open('app'); await actions.open('work');
    assert.deepEqual(opened, [home, work]);
    for (const id of [work, '../', 'https://example.test', null, {}]) assert.throws(() => actions.open(id), /标识/);
    fs.rmdirSync(work);
    await assert.rejects(actions.open('work'), /无法打开文件夹/);
    assert.equal(fs.existsSync(work), false);
    runtime.beginQuit(); assert.throws(() => actions.open('app'), /退出/);
  } finally { fs.rmSync(home, { recursive: true }); }
});

// Native dialogs, update transport and installer are controlled external boundaries.
function desktopHarness() {
  const handlers = {}; const messages = []; const order = [];
  const runtime = createRuntimeState(); runtime.setStartup({ entry: { mode: 'workbench' } });
  let window = { webContents: {} };
  let nextDialog; let response = 1; let checks = 0;
  let nextCheck = async () => ({ updateInfo: { version: '1.0.2' } });
  let prepare = async () => { order.push('stop'); return runtime.beginQuit(); };
  let install = () => order.push('install');
  let latest = '2.0.0';
  const updater = new EventEmitter();
  updater.checkForUpdates = () => { checks++; return nextCheck(); };
  updater.quitAndInstall = () => install();
  const app = { getVersion: () => '1.0.2', isPackaged: true, quit() {} };
  const dialog = { showMessageBox: async (parent, options) => { messages.push(options); return nextDialog ? nextDialog(parent, options) : { response }; }, showErrorBox: (...args) => messages.push(args) };
  const flow = createUpdateFlow({ app, dialog, runtime, getWindow: () => window,
    shell: { openExternal: async () => { throw new Error('private-browser-error'); } },
    getUpdater: () => updater, prepareForQuit: () => prepare(), markExiting: () => order.push('mark'),
    https: { get: (url, options, callback) => {
      assert.equal(url, 'https://api.github.com/repos/dingshuxin353/live-clipper/releases/latest');
      assert.equal(options.timeout, 8000);
      const request = new EventEmitter(); request.destroy = () => {};
      queueMicrotask(() => { const result = new EventEmitter(); result.statusCode = 200; callback(result); result.emit('data', JSON.stringify({ tag_name: latest })); result.emit('end'); });
      return request;
    } },
  });
  registerDesktopCapabilities({ ipcMain: { handle: (name, fn) => { handlers[name] = fn; } }, app, dialog,
    runtime, getWindow: () => window, getPort: () => 12345, appHome: '/isolated/home', client: {}, clipboard: {}, shell: {}, checkForUpdates: flow.checkForUpdates });
  const trusted = { sender: window.webContents, senderFrame: { url: 'http://127.0.0.1:12345/settings' } };
  return { handlers, trusted, updater, runtime, messages, order, flow, app, checks: () => checks,
    window: () => window, setWindow: value => { window = value; },
    setDialog: fn => { nextDialog = fn; }, setResponse: value => { response = value; }, setCheck: fn => { nextCheck = fn; },
    setPrepare: fn => { prepare = fn; }, setInstall: fn => { install = fn; }, setLatest: value => { latest = value; },
    downloaded: () => { runtime.setStartup({ entry: { mode: 'onboarding' } }); updater.emit('update-downloaded'); runtime.setStartup({ entry: { mode: 'workbench' } }); },
  };
}

test('desktop info IPC uses app version and rejects untrusted renderers', () => {
  const h = desktopHarness();
  assert.equal(h.handlers['lc:application-info'](h.trusted).version, '1.0.2');
  assert.equal(h.handlers['lc:application-info'](h.trusted).app_home, '/isolated/home');
  for (const name of ['lc:application-info', 'lc:open-data-directory', 'lc:check-for-updates']) {
    assert.throws(() => h.handlers[name]({ ...h.trusted, senderFrame: { url: 'https://outside.test' } }, 'app'), /无法执行此操作/);
  }
});

test('IPC and other update callers share in-flight work, failures retry, and confirmed installation prepares quit', async () => {
  const h = desktopHarness(); let finish;
  h.setCheck(() => new Promise(resolve => { finish = resolve; }));
  const silent = h.flow.checkForUpdates(false);
  const first = h.handlers['lc:check-for-updates'](h.trusted);
  const second = h.flow.checkForUpdates(true);
  assert.equal(first, second); assert.equal(h.checks(), 1);
  finish({ updateInfo: { version: '1.0.2' } }); await silent; await first;
  assert.equal(h.messages[0].message, '未发现新版本');
  h.setCheck(async () => { throw new Error('private error'); });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'check');
  h.setCheck(async () => ({ updateInfo: {} }));
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'check');
  assert.equal(h.messages.filter(m => m.message === '未发现新版本').length, 1);
  h.downloaded();
  await h.handlers['lc:check-for-updates'](h.trusted);
  assert.deepEqual(h.messages.at(-1).buttons, ['重启并安装', '稍后']);
  assert.deepEqual(h.order, []);
  h.setResponse(0); await h.handlers['lc:check-for-updates'](h.trusted);
  assert.deepEqual(h.order, ['stop', 'mark', 'install']);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'restricted');
  for (const name of ['update-available', 'update-downloaded', 'error']) assert.equal(h.updater.listenerCount(name), 1);
  assert.equal(h.updater.autoDownload, true);
  assert.equal(h.updater.autoInstallOnAppQuit, false);
});

for (const latest of ['1.0.2', '1.0.1']) test(`no-update ${latest} owns its native dialog and releases IPC after dismissal`, async () => {
  const h = desktopHarness(); let dismiss;
  h.setCheck(async () => ({ updateInfo: { version: latest } }));
  h.setDialog((parent, options) => {
    assert.equal(parent, h.window());
    assert.equal(options.message, '未发现新版本');
    return new Promise(resolve => { dismiss = () => resolve({ response: 0 }); });
  });
  let returned = false;
  const first = h.handlers['lc:check-for-updates'](h.trusted).then(result => { returned = true; return result; });
  await new Promise(setImmediate);
  assert.equal(returned, false);
  assert.equal(h.messages.length, 1);
  const duplicate = h.handlers['lc:check-for-updates'](h.trusted);
  assert.equal(h.checks(), 1);
  dismiss(); assert.equal((await first).ok, true); await duplicate;
  const second = h.handlers['lc:check-for-updates'](h.trusted);
  await new Promise(setImmediate);
  assert.equal(h.checks(), 2); assert.equal(h.messages.length, 2);
  dismiss(); assert.equal((await second).ok, true);
});

test('update events preserve download errors and prepare/install failures without exposing external details', async () => {
  const h = desktopHarness();
  await h.handlers['lc:check-for-updates'](h.trusted);
  h.updater.emit('update-available'); h.updater.emit('error', new Error('private-url/key'));
  assert.match(h.messages.at(-1)[1], /下载失败/);
  assert.doesNotMatch(JSON.stringify(h.messages), /private-url/);
  h.downloaded(); h.setPrepare(async () => false); h.setResponse(0);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'prepare');
  assert.deepEqual(h.order, []);
  h.setPrepare(async () => { throw new Error('private prepare'); });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'prepare');
  h.setPrepare(async () => true); h.setInstall(() => { throw new Error('secret'); });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'install');
  assert.deepEqual(h.order, ['mark']);
  assert.match(h.messages.at(-1)[1], /后台处理已停止/);
  assert.doesNotMatch(JSON.stringify(h.messages), /secret|private prepare/);
  h.runtime.setStartup({ entry: { mode: 'onboarding' } });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'restricted');
});

test('development download-page errors keep their own phase through the production IPC', async () => {
  const h = desktopHarness(); h.setResponse(0); h.app.isPackaged = false;
  const result = await h.handlers['lc:check-for-updates'](h.trusted);
  assert.equal(result.code, 'page');
  assert.match(h.messages.at(-1)[1], /无法打开下载页/);
  assert.deepEqual(h.order, []);
});

test('download events share confirmation, respect current restrictions, and use the current window', async () => {
  const h = desktopHarness();
  assert.equal(h.checks(), 0);
  assert.equal(h.updater.listenerCount('error'), 0);
  await h.flow.checkForUpdates(false);
  h.runtime.setStartup({ entry: { mode: 'migration_required' } });
  h.updater.emit('update-downloaded');
  assert.equal(h.messages.length, 0);
  const replacement = { webContents: {} };
  h.setWindow(replacement);
  h.runtime.setStartup({ entry: { mode: 'workbench' } });
  let dismiss;
  h.setDialog((owner) => { assert.equal(owner, replacement); return new Promise(resolve => { dismiss = resolve; }); });
  const event = { ...h.trusted, sender: replacement.webContents };
  const first = h.handlers['lc:check-for-updates'](event);
  await new Promise(setImmediate);
  h.updater.emit('update-downloaded');
  const duplicate = h.flow.checkForUpdates(true);
  assert.equal(h.messages.length, 1);
  h.runtime.setStartup({ entry: { mode: 'onboarding' } });
  dismiss({ response: 0 });
  assert.equal((await first).code, 'restricted');
  assert.equal((await duplicate).code, 'restricted');
  assert.deepEqual(h.order, []);
  assert.throws(() => h.handlers['lc:check-for-updates'](h.trusted), /无法执行此操作/);
});

test('installation waits for quit preparation and handles installer error events safely', async () => {
  const h = desktopHarness();
  await h.flow.checkForUpdates(false); h.downloaded(); h.setResponse(0);
  let prepared;
  h.setPrepare(() => new Promise(resolve => { prepared = resolve; }));
  h.setInstall(() => { h.order.push('install'); h.updater.emit('error', new Error('secret installer path')); });
  const installing = h.handlers['lc:check-for-updates'](h.trusted);
  await new Promise(setImmediate);
  assert.deepEqual(h.order, []);
  prepared(true);
  assert.equal((await installing).code, 'install');
  assert.deepEqual(h.order, ['mark', 'install']);
  assert.doesNotMatch(JSON.stringify(h.messages), /secret installer path/);
});

test('development invalid versions fail safely and a later valid query can retry', async () => {
  const h = desktopHarness(); h.app.isPackaged = false;
  h.setLatest('not-a-version');
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'check');
  h.setLatest('v1.0.2');
  assert.deepEqual(await h.handlers['lc:check-for-updates'](h.trusted), { ok: true });
  assert.equal(h.messages.at(-1).message, '未发现新版本');
});
