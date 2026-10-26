const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter, once } = require("node:events");
const { spawn } = require("node:child_process");
const { createRequire } = require("node:module");
const Module = require("node:module");
const test = require("node:test");

test("production startup shares page, tray and automatic updates and prepares the actual backend before installation", { timeout: 15000 }, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "venus-startup-"));
  const trace = path.join(root, "native-order.jsonl");
  let backend;
  t.after(async () => {
    if (backend && backend.exitCode === null) {
      backend.kill("SIGTERM");
      await once(backend, "exit", { signal: AbortSignal.timeout(5000) });
    }
    fs.rmSync(root, { recursive: true });
  });
  const source = path.resolve(__dirname, "../main.js");
  const sourceRequire = createRequire(source);
  const originalLoad = Module._load;
  const updater = new EventEmitter();
  let checks = 0; let finishCheck;
  updater.checkForUpdates = () => { checks++; return new Promise(resolve => { finishCheck = resolve; }); };
  let installed;
  const installation = new Promise(resolve => { installed = resolve; });
  updater.quitAndInstall = () => {
    fs.appendFileSync(trace, '"install"\n');
    let prevented = false;
    app.emit("before-quit", { preventDefault: () => { prevented = true; } });
    assert.equal(prevented, false, "installation exit must have been marked after preparation");
    installed();
  };
  t.mock.method(Module, "_load", function(name, ...args) {
    return name === "electron-updater" ? { autoUpdater: updater } : Reflect.apply(originalLoad, this, [name, ...args]);
  });
  const handlers = new Map(); const timers = new Set(); const windows = []; const trays = []; const prompts = [];
  let ready; let response = 1;
  const app = Object.assign(new EventEmitter(), {
    name: "Venus", isPackaged: false, getPath: () => root, setPath() {}, getVersion: () => "1.0.6",
    requestSingleInstanceLock: () => true, whenReady: () => ({ then: fn => { ready = fn; } }),
    dock: { setBadge() {} }, exit: code => assert.fail(`Unexpected exit ${code}`),
  });
  class Window extends EventEmitter {
    constructor() {
      super(); windows.push(this);
      this.webContents = Object.assign(new EventEmitter(), { getURL: () => this.url, setWindowOpenHandler() {} });
    }
    loadURL(url) { this.url = url; return Promise.resolve(); }
    show() {} focus() {} hide() {}
  }
  class Tray {
    constructor() { trays.push(this); }
    setToolTip() {} setContextMenu(items) { this.items = items; }
    destroy() { this.destroyed = true; }
  }
  const electron = {
    app, BrowserWindow: Window, Tray, Menu: { buildFromTemplate: value => value, setApplicationMenu() {} },
    clipboard: {}, shell: {}, nativeImage: { createFromPath: () => ({ setTemplateImage() {} }) },
    ipcMain: { handle: (name, fn) => { assert.equal(handlers.has(name), false); handlers.set(name, fn); } },
    session: { defaultSession: { cookies: { set: async () => {} } } },
    dialog: { showMessageBox: async (_, options) => { prompts.push(options); return { response }; }, showErrorBox: (_, message) => assert.fail(message) },
  };
  const backendProgram = `
    const http = require('node:http'); const fs = require('node:fs');
    const server = http.createServer((req, res) => {
      if (req.headers.authorization !== 'Bearer ' + process.env.LIVE_CLIPPER_WEB_TOKEN) { res.writeHead(403); res.end('{}'); return; }
      let result = { ok: true, unseen_result_count: 0 };
      if (req.url === '/api/onboarding') result = { ok: true, entry: { mode: 'workbench', onboarding: null } };
      if (req.url === '/api/service/stop') fs.appendFileSync(process.argv[2], '"stop"\\n');
      res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(result));
    });
    process.on('SIGTERM', () => { fs.appendFileSync(process.argv[2], '"term"\\n'); server.closeAllConnections(); server.close(() => process.exit(0)); });
    server.listen(Number(process.argv[1]), '127.0.0.1');
  `;
  vm.runInNewContext(fs.readFileSync(source, "utf8"), {
    __dirname: path.dirname(source), URL, console,
    process: { env: { LIVE_CLIPPER_HOME: root }, platform: process.platform, arch: process.arch, stdout: { write() {} }, stderr: { write() {} } },
    setTimeout: (fn, delay) => { const timer = { fn, delay }; timers.add(timer); return timer; }, clearTimeout: timer => timers.delete(timer),
    require: name => {
      if (name === "electron") return electron;
      if (name === "child_process") return { spawn: (_, args, options) => {
        backend = spawn(process.execPath, ["-e", backendProgram, args.at(-1), trace], options);
        return backend;
      } };
      return sourceRequire(name);
    },
  });
  await ready();
  assert.equal(handlers.size, 12);
  assert.equal(windows.length, 1);
  assert.equal(trays.length, 1);
  app.isPackaged = true;
  const event = { sender: windows[0].webContents, senderFrame: { url: windows[0].url } };
  const automatic = [...timers].find(timer => timer.delay === 5000);
  assert.ok(automatic);
  timers.delete(automatic);
  automatic.fn();
  const page = handlers.get("lc:check-for-updates")(event);
  const tray = trays[0].items.find(item => item.label === "检查更新").click();
  assert.equal(page, tray);
  assert.equal(checks, 1);
  finishCheck({ updateInfo: { version: "1.0.6" } });
  assert.deepEqual(await page, { ok: true });
  assert.equal(prompts.length, 1);
  response = 0;
  updater.emit("update-downloaded");
  await installation;
  assert.deepEqual(fs.readFileSync(trace, "utf8").trim().split("\n").map(line => JSON.parse(line)), ["stop", "term", "install"]);
  assert.equal(trays[0].destroyed, true);
  assert.equal(timers.size, 0);
  assert.equal(backend.exitCode, 0);
});
