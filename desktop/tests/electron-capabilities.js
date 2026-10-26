const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");
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
  dispose = registerDesktopCapabilities({
    ipcMain, runtime, appHome: root, getWindow: () => currentWindow, getPort: () => port,
    app: { getVersion: () => app.getVersion(), quit: () => effects.push("quit") },
    clipboard: { readText: () => { effects.push("read"); return "isolated clipboard"; }, writeText: text => effects.push(["write", text]) },
    dialog: { showOpenDialog: async (parent) => { assert.equal(parent, currentWindow); effects.push("dialog"); return { canceled: true, filePaths: [] }; } },
    shell: {}, client: {},
    checkForUpdates: () => assert.fail("Untrusted updates must not execute"),
  });
  currentWindow = await windowAt(port);
  const methods = await currentWindow.webContents.executeJavaScript("Object.keys(window.liveClipperShell).sort()");
  assert.deepEqual(methods, ["getApplicationInfo", "openDataDirectory", "checkForUpdates", "selectFolder", "readClipboardText", "openOutput", "revealOutput", "selectIssueSource", "selectRecoveryOutput", "showBackup", "quitApp", "writeClipboardText"].sort());
  assert.equal((await call(currentWindow, "getApplicationInfo()")).app_home, root);
  assert.equal(await call(currentWindow, "selectFolder('选择目录')"), null);
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  assert.deepEqual(await call(currentWindow, "writeClipboardText('isolated text')"), { ok: true });
  cases.push("production preload and trusted IPC; restricted folder and clipboard");

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

  runtime.beginQuit();
  await assert.rejects(call(currentWindow, "getApplicationInfo()"), /退出/);
  assert.equal(await call(currentWindow, "readClipboardText()"), "isolated clipboard");
  assert.deepEqual(await call(currentWindow, "quitApp()"), { ok: true });
  cases.push("original per-capability quit rules");
  fs.writeFileSync(path.join(root, "result.json"), JSON.stringify({ passed: true, versions: process.versions, pid: process.pid, ports: [port, outsidePort], cases, controlled: ["clipboard", "system dialogs", "quit"] }, null, 2) + "\n");
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
