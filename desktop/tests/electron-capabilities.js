const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");
const { EventEmitter } = require("node:events");
const { createUpdateFlow } = require("../update-flow");
const { registerDesktopCapabilities } = require("../desktop-capabilities");
const { createRuntimeState } = require("../runtime-state");

app.on("window-all-closed", () => {});

const root = path.resolve(process.argv[2]);
fs.mkdirSync(root, { recursive: true });
app.setPath("userData", path.join(root, "electron-profile"));
const windows = [];
const servers = [];
const effects = [];
const cases = [];
let currentWindow = null;
let dispose;

async function serve() {
  const server = http.createServer((_, response) => response.end("<!doctype html><title>Isolated desktop capability check</title>"));
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  servers.push(server);
  return server.address().port;
}
async function windowAt(port) {
  const window = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, preload: path.resolve(__dirname, "../preload.js") } });
  windows.push(window);
  await window.loadURL(`http://127.0.0.1:${port}/settings`);
  return window;
}
const call = (window, expression) => window.webContents.executeJavaScript(`window.liveClipperShell.${expression}`);

app.whenReady().then(async () => {
  const runtime = createRuntimeState();
  const port = await serve();
  const outsidePort = await serve();
  let response = 1; let finishCheck; let queryStarted;
  const checkStarted = new Promise(resolve => { queryStarted = resolve; });
  const messages = [];
  const updater = new EventEmitter();
  updater.checkForUpdates = () => { effects.push("query"); queryStarted(); return new Promise(resolve => { finishCheck = resolve; }); };
  updater.quitAndInstall = () => effects.push("install");
  const appBoundary = { getVersion: () => app.getVersion(), isPackaged: true, quit: () => effects.push("quit") };
  const dialog = {
    showOpenDialog: async parent => { assert.equal(parent, currentWindow); effects.push("dialog"); return { canceled: true, filePaths: [] }; },
    showMessageBox: async (parent, options) => { assert.equal(parent, currentWindow); messages.push(options); return { response }; },
    showErrorBox: (_, message) => assert.fail(message),
  };
  const flow = createUpdateFlow({ app: appBoundary, dialog, shell: {}, runtime, getWindow: () => currentWindow,
    getUpdater: () => updater, prepareForQuit: async () => { effects.push("prepare"); return runtime.beginQuit(); }, markExiting: () => effects.push("mark") });
  dispose = registerDesktopCapabilities({
    ipcMain, runtime, appHome: root, getWindow: () => currentWindow, getPort: () => port,
    app: appBoundary, dialog,
    clipboard: { readText: () => { effects.push("read"); return "isolated clipboard"; }, writeText: text => effects.push(["write", text]) },
    shell: {}, client: {}, checkForUpdates: flow.checkForUpdates,
  });
  currentWindow = await windowAt(port);
  const methods = await currentWindow.webContents.executeJavaScript("Object.keys(window.liveClipperShell).sort()");
  assert.deepEqual(methods, ["getApplicationInfo", "openDataDirectory", "checkForUpdates", "selectFolder", "readClipboardText", "openOutput", "revealOutput", "selectIssueSource", "selectRecoveryOutput", "showBackup", "quitApp", "writeClipboardText"].sort());
  assert.equal((await call(currentWindow, "getApplicationInfo()")).app_home, root);
  assert.equal(await call(currentWindow, "selectFolder('选择目录')"), null);
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  assert.deepEqual(await call(currentWindow, "writeClipboardText('isolated text')"), { ok: true });
  assert.equal((await call(currentWindow, "checkForUpdates()")).code, "restricted");
  assert.equal(updater.listenerCount("error"), 0);
  cases.push("production preload and trusted IPC; restricted folder, clipboard and updates");

  const effectsBefore = effects.length;
  const otherWindow = await windowAt(port);
  for (const expression of ["readClipboardText()", "selectFolder('目录')", "checkForUpdates()", "quitApp()"])
    await assert.rejects(call(otherWindow, expression), /无法执行此操作/);
  assert.equal(effects.length, effectsBefore);
  cases.push("wrong actual webContents rejected without effects");

  await currentWindow.loadURL(`http://127.0.0.1:${outsidePort}/settings`);
  await assert.rejects(call(currentWindow, "readClipboardText()"), /无法执行此操作/);
  assert.equal(effects.length, effectsBefore);
  await currentWindow.loadURL(`http://127.0.0.1:${port}/settings`);
  cases.push("current renderer with wrong actual origin rejected");

  const oldWindow = currentWindow;
  currentWindow = otherWindow;
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  await assert.rejects(call(oldWindow, "readClipboardText()"), /无法执行此操作/);
  oldWindow.destroy();
  currentWindow.destroy();
  currentWindow = null;
  const replacement = await windowAt(port);
  await assert.rejects(call(replacement, "readClipboardText()"), /无法执行此操作/);
  currentWindow = replacement;
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  cases.push("window replacement, no-window rejection, and recreation without registration");

  runtime.setStartup({ entry: { mode: "workbench" } });
  const firstCheck = call(currentWindow, "checkForUpdates()");
  await checkStarted;
  const duplicateCheck = call(currentWindow, "checkForUpdates()");
  await call(currentWindow, "getApplicationInfo()");
  finishCheck({ updateInfo: { version: app.getVersion() } });
  assert.deepEqual(await firstCheck, { ok: true });
  assert.deepEqual(await duplicateCheck, { ok: true });
  assert.equal(effects.filter(value => value === "query").length, 1);
  assert.equal(messages.length, 1);
  updater.emit("update-downloaded");
  assert.deepEqual(await call(currentWindow, "checkForUpdates()"), { ok: true, deferred: true });
  assert.equal(effects.includes("prepare"), false);
  response = 0;
  assert.deepEqual(await call(currentWindow, "checkForUpdates()"), { ok: true });
  assert.deepEqual(effects.slice(-3), ["prepare", "mark", "install"]);
  cases.push("renderer update query sharing, event-driven defer and prepare-before-install");

  await assert.rejects(call(currentWindow, "getApplicationInfo()"), /退出/);
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  assert.deepEqual(await call(currentWindow, "quitApp()"), { ok: true });
  cases.push("original per-capability quit rules");
  fs.writeFileSync(path.join(root, "result.json"), JSON.stringify({ passed: true, versions: process.versions, pid: process.pid, ports: [port, outsidePort], cases, controlled: ["clipboard", "system dialogs", "update network and installer", "quit preparation"] }, null, 2) + "\n");
}).catch(error => {
  fs.writeFileSync(path.join(root, "result.json"), JSON.stringify({ passed: false, message: error.message, cases }, null, 2) + "\n");
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  for (const window of windows) if (!window.isDestroyed()) window.destroy();
  dispose?.();
  for (const server of servers) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
  app.exit(process.exitCode || 0);
});
