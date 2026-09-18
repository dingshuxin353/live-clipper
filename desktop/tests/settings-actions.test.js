const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
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

// Exercise the actual registered IPC handlers and shared updater, without a network or installer.
function desktopHarness() {
  const handlers = {}; const events = {}; const messages = []; const order = [];
  let nextDialog; let response = 1; let checks = 0; let nextCheck = async () => ({ updateInfo: { version: '1.0.2' } });
  const updater = { on: (name, fn) => { events[name] = fn; }, checkForUpdates: () => { checks++; return nextCheck(); }, quitAndInstall: () => order.push('install') };
  const electron = {
    app: { getPath: () => '/isolated', setPath() {}, getVersion: () => '1.0.2', isPackaged: true, requestSingleInstanceLock: () => true, whenReady: () => new Promise(() => {}), on() {} },
    shell: { openExternal: async () => { throw new Error("private-browser-error"); } },
    ipcMain: { handle: (name, fn) => { handlers[name] = fn; } },
    dialog: { showMessageBox: async (parent, options) => { messages.push(options); return nextDialog ? nextDialog(parent, options) : { response }; }, showErrorBox: (...args) => messages.push(args) },
  };
  const fakeFs = { ...fs, existsSync: () => true, realpathSync: value => value, mkdirSync() {} };
  const context = vm.createContext({ require: name => name === 'electron' ? electron : name === 'electron-updater' ? { autoUpdater: updater } : name === 'fs' ? fakeFs : name.startsWith('./') ? require(`../${name.slice(2)}`) : require(name), process: { env: { LIVE_CLIPPER_HOME: '/isolated/home' }, platform: 'darwin', arch: 'arm64' }, URL, console, setTimeout, clearTimeout, __dirname: path.resolve(__dirname, '..') });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8'), context);
  vm.runInContext('backendPort = 12345; mainWindow = { webContents: {} }; runtime.setStartup({ entry: { mode: "workbench" } }); backendClient = { stopService: async () => order.push("stop") };', Object.assign(context, { order }));
  const trusted = vm.runInContext('({ sender: mainWindow.webContents, senderFrame: { url: "http://127.0.0.1:12345/settings" } })', context);
  return { handlers, trusted, events, messages, order, context, checks: () => checks, setDialog: fn => { nextDialog = fn; }, setResponse: value => { response = value; }, setCheck: fn => { nextCheck = fn; } };
}

test('desktop info IPC uses app version and rejects untrusted renderers', () => {
  const h = desktopHarness();
  assert.equal(h.handlers['lc:application-info'](h.trusted).version, '1.0.2');
  assert.equal(h.handlers['lc:application-info'](h.trusted).app_home, '/isolated/home');
  for (const name of ['lc:application-info', 'lc:open-data-directory', 'lc:check-for-updates']) {
    assert.throws(() => h.handlers[name]({ ...h.trusted, senderFrame: { url: 'https://outside.test' } }, 'app'), /无法执行此操作/);
  }
});

test('IPC and tray update flow share in-flight work, failures retry, downloaded update requires confirmation before safe quit', async () => {
  const h = desktopHarness(); let finish;
  h.setCheck(() => new Promise(resolve => { finish = resolve; }));
  const silent = vm.runInContext('checkForUpdates(false)', h.context);
  const first = h.handlers['lc:check-for-updates'](h.trusted);
  const second = vm.runInContext('checkForUpdates(true)', h.context);
  assert.equal(first, second); assert.equal(h.checks(), 1);
  finish({ updateInfo: { version: '1.0.2' } }); await silent; await first;
  assert.equal(h.messages[0].message, '未发现新版本');
  h.setCheck(async () => { throw new Error('private error'); });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
  h.setCheck(async () => ({ updateInfo: {} }));
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
  assert.equal(h.messages.filter(m => m.message === '未发现新版本').length, 1);
  vm.runInContext('updateDownloaded = true', h.context);
  await h.handlers['lc:check-for-updates'](h.trusted);
  assert.equal(JSON.stringify(h.messages.at(-1).buttons), JSON.stringify(['重启并安装', '稍后']));
  assert.deepEqual(h.order, []);
  h.setResponse(0); await h.handlers['lc:check-for-updates'](h.trusted);
  assert.deepEqual(h.order, ['stop', 'install']);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
});

for (const latest of ['1.0.2', '1.0.1']) test(`no-update ${latest} owns its native dialog and releases IPC for a second check after dismissal`, async () => {
  const h = desktopHarness(); let dismiss;
  h.setCheck(async () => ({ updateInfo: { version: latest } }));
  h.setDialog((parent, options) => {
    assert.equal(parent, vm.runInContext('mainWindow', h.context));
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

test('update errors retain the failed operation and never treat rejected installation as success', async () => {
  const h = desktopHarness();
  await h.handlers['lc:check-for-updates'](h.trusted);
  h.events['update-available'](); h.events.error(new Error('private-url/key'));
  assert.match(h.messages.at(-1)[1], /下载失败/);
  assert.doesNotMatch(JSON.stringify(h.messages), /private-url/);
  vm.runInContext('updateDownloaded = true; prepareForQuit = async () => false', h.context);
  h.setResponse(0);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'prepare');
  assert.deepEqual(h.order, []);
  vm.runInContext('prepareForQuit = async () => true; updater.quitAndInstall = () => { throw new Error("secret"); }', h.context);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).code, 'install');
  assert.match(h.messages.at(-1)[1], /后台处理已停止/);
  assert.doesNotMatch(JSON.stringify(h.messages), /secret/);
  vm.runInContext('runtime.setStartup({ entry: { mode: "onboarding" } })', h.context);
  assert.equal((await vm.runInContext('installDownloadedUpdate()', h.context)).code, 'restricted');
});

test('manual update page errors are not reported as update check failures', async () => {
  const h = desktopHarness(); h.setResponse(0);
  const result = await vm.runInContext('app.isPackaged = false; showUpdateResult({ ok: true, latest: "2.0.0" })', h.context);
  assert.equal(result.code, 'page');
  assert.match(h.messages.at(-1)[1], /无法打开下载页/);
  assert.deepEqual(h.order, []);
});
