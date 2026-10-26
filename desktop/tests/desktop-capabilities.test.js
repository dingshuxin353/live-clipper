const assert = require("node:assert/strict");
const test = require("node:test");
const { registerDesktopCapabilities } = require("../desktop-capabilities");
const { createRuntimeState } = require("../runtime-state");

function harness({ client = {}, appHome = "/isolated/home", port = 12345 } = {}) {
  const handlers = new Map([["unrelated", () => "retained"]]);
  const calls = [];
  let window = { webContents: {} };
  let select = async () => ({ canceled: true, filePaths: [] });
  const runtime = createRuntimeState();
  const dispose = registerDesktopCapabilities({
    ipcMain: { handle: (name, handler) => { assert.equal(handlers.has(name), false); handlers.set(name, handler); }, removeHandler: name => handlers.delete(name) },
    app: { getVersion: () => "1.0.6", quit: () => calls.push("quit") },
    clipboard: { readText: () => { calls.push("read"); return "isolated text"; }, writeText: value => calls.push(["write", value]) },
    dialog: { showOpenDialog: async (...args) => { calls.push(["dialog", ...args]); return select(...args); } },
    shell: { openPath: async () => { calls.push("open"); return ""; }, showItemInFolder: () => calls.push("reveal") },
    client, runtime, appHome,
    getWindow: () => window, getPort: () => port,
    checkForUpdates: () => { calls.push("update"); return Promise.resolve({ ok: false, code: "restricted" }); },
  });
  const event = () => ({ sender: window?.webContents, senderFrame: { url: `http://127.0.0.1:${port}/settings` } });
  return { handlers, calls, runtime, dispose, event, setDialog: value => { select = value; }, setWindow: value => { window = value; } };
}

test("all named capabilities reject untrusted sources before effects and follow the current window", async () => {
  const h = harness();
  const channels = ["application-info", "open-data-directory", "check-for-updates", "select-folder", "read-clipboard-text", "write-clipboard-text", "open-output", "reveal-output", "select-issue-source", "select-recovery-output", "show-migration-backup", "quit-app"].map(name => `lc:${name}`);
  assert.deepEqual([...h.handlers.keys()].filter(name => name !== "unrelated").sort(), channels.sort());
  const current = h.event();
  assert.deepEqual(h.handlers.get("lc:application-info")(current), { version: "1.0.6", app_home: "/isolated/home", platform: process.platform, arch: process.arch });
  for (const channel of channels) {
    const events = [
      { ...current, sender: {} },
      ...["https://127.0.0.1:12345/", "http://localhost:12345/", "http://127.0.0.1:12346/", "file:///tmp/page", "invalid"].map(url => ({ ...current, senderFrame: { url } })),
      { ...current, senderFrame: null },
    ];
    for (const event of events) {
      if (channel === "lc:select-folder") await assert.rejects(h.handlers.get(channel)(event), /无法执行此操作/);
      else assert.throws(() => h.handlers.get(channel)(event), /无法执行此操作/);
    }
  }
  assert.deepEqual(h.calls, []);
  h.setWindow({ webContents: {} });
  assert.throws(() => h.handlers.get("lc:application-info")(current), /无法执行此操作/);
  assert.equal(h.handlers.get("lc:application-info")(h.event()).version, "1.0.6");
  h.setWindow(null);
  assert.throws(() => h.handlers.get("lc:read-clipboard-text")(current), /无法执行此操作/);
  h.dispose();
  assert.deepEqual([...h.handlers.keys()], ["unrelated"]);
});


test("registered file, directory, backup and clipboard operations keep their results and state limits", async t => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const http = require("node:http");
  const { BackendClient } = require("../backend-client");
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "venus-capabilities-")));
  const backup = path.join(home, "work", "migration-backups", "migration-1");
  fs.mkdirSync(backup, { recursive: true });
  const requests = [];
  const server = http.createServer(async (request, response) => {
    assert.equal(request.headers.authorization, "Bearer isolated-test-token");
    let text = "";
    for await (const chunk of request) text += chunk;
    const body = text ? JSON.parse(text) : null;
    requests.push([request.method, request.url, body]);
    let result;
    if (request.url === "/api/config") result = { storage: { work_dir: home } };
    else if (request.url === "/api/desktop/outputs/output-1/path") result = { path: path.join(home, "output.mp4") };
    else if (request.url === "/api/desktop/file-selections") result = { selection_token: "selection-1", expires_in_seconds: 300 };
    else if (request.url === "/api/migration/migration-1/backup-grant") result = { ok: true, grant: { grant_version: 1, kind: "migration_backup_reveal", migration_id: "migration-1", backup_path: backup } };
    else { response.statusCode = 404; result = { ok: false, error: { code: "migration_not_found", message: "升级记录不存在" } }; }
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify(result));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); fs.rmSync(home, { recursive: true }); });
  const port = server.address().port;
  const h = harness({ client: new BackendClient({ port, token: "isolated-test-token" }), appHome: home, port });
  t.after(h.dispose);
  const call = (name, ...args) => h.handlers.get(`lc:${name}`)(h.event(), ...args);
  for (const mode of ["onboarding", "migration_required"]) {
    h.runtime.setStartup({ entry: { mode } });
    assert.equal(await call("select-folder", "选择目录"), null);
    assert.deepEqual(await call("show-migration-backup", "migration-1"), { ok: true });
    assert.deepEqual(call("quit-app"), { ok: true });
  }
  h.runtime.setStartup({ entry: { mode: "workbench" } });
  let finish;
  h.setDialog((parent, options) => {
    assert.equal(parent.webContents, h.event().sender);
    assert.deepEqual(options.properties, ["openDirectory", "createDirectory"]);
    return new Promise(resolve => { finish = resolve; });
  });
  const before = h.calls.length;
  const first = call("select-folder", "选择目录");
  const duplicate = call("select-folder", "选择目录");
  await Promise.resolve();
  assert.equal(h.calls.length, before + 1);
  finish({ canceled: false, filePaths: [home] });
  assert.equal(await first, home);
  assert.equal(await duplicate, home);
  h.setDialog(async () => ({ canceled: true, filePaths: [] }));
  const beforeCancel = requests.length;
  assert.equal(await call("select-issue-source", "issue-1"), null);
  assert.equal(requests.length, beforeCancel);
  h.setDialog(async () => ({ canceled: false, filePaths: [path.join(home, "source.mp4")] }));
  for (const method of ["select-issue-source", "select-recovery-output"]) {
    const result = await call(method, "issue-1");
    assert.deepEqual(Object.keys(result).sort(), ["expiresAt", "selectionToken"]);
    assert.equal(result.selectionToken, "selection-1");
    assert.ok(Date.parse(result.expiresAt) > Date.now());
  }
  assert.deepEqual(requests.filter(([, url]) => url === "/api/desktop/file-selections").map(([, , body]) => body), [
    { issue_id: "issue-1", kind: "source", selected_path: path.join(home, "source.mp4") },
    { issue_id: "issue-1", kind: "recovery_output", selected_path: path.join(home, "source.mp4") },
  ]);
  for (const method of ["open-output", "reveal-output"]) assert.deepEqual(await call(method, "output-1"), { ok: true });
  for (const id of ["app", "work"]) assert.deepEqual(await call("open-data-directory", id), { ok: true });
  assert.throws(() => call("open-data-directory", "../"), /标识/);
  assert.equal((await call("show-migration-backup", "missing")).code, "migration_not_found");
  assert.throws(() => call("show-migration-backup", "../"), /升级记录/);
  assert.equal(call("read-clipboard-text"), "isolated text");
  assert.deepEqual(call("write-clipboard-text", "test text"), { ok: true });
  for (const value of [{}, "x".repeat(20001)]) assert.throws(() => call("write-clipboard-text", value), /无法复制/);
  h.runtime.beginQuit();
  for (const [method, arg] of [["application-info"], ["open-data-directory", "app"], ["open-output", "output-1"], ["reveal-output", "output-1"], ["select-issue-source", "issue-1"], ["select-recovery-output", "issue-1"], ["show-migration-backup", "migration-1"], ["write-clipboard-text", "text"]]) {
    assert.throws(() => call(method, arg), /退出/);
  }
  await assert.rejects(call("select-folder"), /退出/);
  assert.equal(call("read-clipboard-text"), "isolated text");
  assert.deepEqual(call("quit-app"), { ok: true });
});
