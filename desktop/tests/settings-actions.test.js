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
    await assert.rejects(actions.open('work'), /目录不可用/);
    assert.equal(fs.existsSync(work), false);
    runtime.beginQuit(); assert.throws(() => actions.open('app'), /退出/);
  } finally { fs.rmSync(home, { recursive: true }); }
});

// Exercise the actual registered IPC handlers and shared updater, without a network or installer.
function desktopHarness() {
  const handlers = {}; const events = {}; const messages = []; const order = [];
  let response = 1; let checks = 0; let nextCheck = async () => ({ updateInfo: { version: '1.0.2' } });
  const updater = { on: (name, fn) => { events[name] = fn; }, checkForUpdates: () => { checks++; return nextCheck(); }, quitAndInstall: () => order.push('install') };
  const electron = {
    app: { getPath: () => '/isolated', setPath() {}, getVersion: () => '1.0.2', isPackaged: true, requestSingleInstanceLock: () => true, whenReady: () => new Promise(() => {}), on() {} },
    ipcMain: { handle: (name, fn) => { handlers[name] = fn; } },
    dialog: { showMessageBox: async options => { messages.push(options); return { response }; }, showErrorBox: (...args) => messages.push(args) },
  };
  const fakeFs = { ...fs, existsSync: () => true, realpathSync: value => value, mkdirSync() {} };
  const context = vm.createContext({ require: name => name === 'electron' ? electron : name === 'electron-updater' ? { autoUpdater: updater } : name === 'fs' ? fakeFs : name.startsWith('./') ? require(`../${name.slice(2)}`) : require(name), process: { env: { LIVE_CLIPPER_HOME: '/isolated/home' }, platform: 'darwin', arch: 'arm64' }, URL, console, setTimeout, clearTimeout, __dirname: path.resolve(__dirname, '..') });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8'), context);
  vm.runInContext('backendPort = 12345; mainWindow = { webContents: {} }; runtime.setStartup({ entry: { mode: "workbench" } }); backendClient = { stopService: async () => order.push("stop") };', Object.assign(context, { order }));
  const trusted = vm.runInContext('({ sender: mainWindow.webContents, senderFrame: { url: "http://127.0.0.1:12345/settings" } })', context);
  return { handlers, trusted, events, messages, order, context, checks: () => checks, setResponse: value => { response = value; }, setCheck: fn => { nextCheck = fn; } };
}

test('desktop info IPC uses app version and rejects untrusted renderers', () => {
  const h = desktopHarness();
  assert.equal(h.handlers['lc:application-info'](h.trusted).version, '1.0.2');
  assert.equal(h.handlers['lc:application-info'](h.trusted).app_home, '/isolated/home');
  for (const name of ['lc:application-info', 'lc:open-data-directory', 'lc:check-for-updates']) {
    assert.throws(() => h.handlers[name]({ ...h.trusted, senderFrame: { url: 'https://outside.test' } }, 'app'), /来源无效/);
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
  assert.equal(h.messages[0].message, '已是最新版本');
  h.setCheck(async () => { throw new Error('private error'); });
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
  h.setCheck(async () => ({ updateInfo: {} }));
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
  assert.equal(h.messages.filter(m => m.message === '已是最新版本').length, 1);
  vm.runInContext('updateDownloaded = true', h.context);
  await h.handlers['lc:check-for-updates'](h.trusted);
  assert.equal(JSON.stringify(h.messages.at(-1).buttons), JSON.stringify(['重启并更新', '稍后']));
  assert.deepEqual(h.order, []);
  h.setResponse(0); await h.handlers['lc:check-for-updates'](h.trusted);
  assert.deepEqual(h.order, ['stop', 'install']);
  assert.equal((await h.handlers['lc:check-for-updates'](h.trusted)).ok, false);
});
