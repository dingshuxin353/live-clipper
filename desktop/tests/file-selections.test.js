const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createFileSelections,
  createRuntimeState,
  writeClipboardText,
} = require("../runtime-state");

test("source selection uses a single-video dialog and returns only a short-lived token", async () => {
  const dialogCalls = [];
  const registrations = [];
  const selections = createFileSelections({
    client: {
      registerFileSelection: async (...args) => {
        registrations.push(args);
        return { selection_token: "grant-1", expires_in_seconds: 300 };
      },
    },
    dialog: {
      showOpenDialog: async (...args) => {
        dialogCalls.push(args);
        return { canceled: false, filePaths: ["/private/source.mp4"] };
      },
    },
    runtime: createRuntimeState(),
    getWindow: () => ({ id: "window" }),
    now: () => Date.parse("2026-08-27T00:00:00.000Z"),
  });

  const result = await selections.selectIssueSource("issue-1");
  assert.deepEqual(result, {
    selectionToken: "grant-1",
    expiresAt: "2026-08-27T00:05:00.000Z",
  });
  assert.deepEqual(registrations, [["issue-1", "source", "/private/source.mp4"]]);
  assert.deepEqual(dialogCalls[0][1].properties, ["openFile"]);
  assert.deepEqual(dialogCalls[0][1].filters[0].extensions, ["m4v", "mkv", "mov", "mp4", "webm"]);
  assert.equal(JSON.stringify(result).includes("/private/source.mp4"), false);
});

test("recovery selection uses a directory dialog while cancel and backend failure expose no path", async () => {
  let options;
  const canceled = createFileSelections({
    client: { registerFileSelection: async () => { throw new Error("must not run"); } },
    dialog: { showOpenDialog: async (value) => { options = value; return { canceled: true, filePaths: [] }; } },
    runtime: createRuntimeState(),
  });
  assert.equal(await canceled.selectRecoveryOutput("issue-1"), null);
  assert.deepEqual(options.properties, ["openDirectory", "createDirectory"]);

  const failing = createFileSelections({
    client: { registerFileSelection: async () => { throw new Error("后台请求失败"); } },
    dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: ["/private/recovery"] }) },
    runtime: createRuntimeState(),
  });
  await assert.rejects(failing.selectRecoveryOutput("issue-1"), (error) => {
    assert.equal(error.message.includes("/private/recovery"), false);
    return true;
  });
});

test("selection dialogs are deduplicated and new work is rejected after quitting", async () => {
  let finishDialog;
  let dialogs = 0;
  const runtime = createRuntimeState();
  const selections = createFileSelections({
    client: { registerFileSelection: async () => ({ selection_token: "grant", expires_in_seconds: 60 }) },
    dialog: {
      showOpenDialog: () => {
        dialogs += 1;
        return new Promise((resolve) => { finishDialog = resolve; });
      },
    },
    runtime,
  });
  const first = selections.selectIssueSource("issue-1");
  const second = selections.selectIssueSource("issue-1");
  await Promise.resolve();
  assert.equal(dialogs, 1);
  finishDialog({ canceled: true, filePaths: [] });
  assert.equal(await first, null);
  assert.equal(await second, null);
  runtime.beginQuit();
  assert.throws(() => selections.selectIssueSource("issue-2"), /正在退出/);
});

test("clipboard write validates type and length without reading or persisting content", () => {
  const writes = [];
  const runtime = createRuntimeState();
  assert.deepEqual(writeClipboardText({ writeText: (value) => writes.push(value) }, runtime, "#Venus"), { ok: true });
  assert.deepEqual(writes, ["#Venus"]);
  assert.throws(() => writeClipboardText({ writeText: () => undefined }, runtime, 42), /复制内容无效/);
  assert.throws(() => writeClipboardText({ writeText: () => undefined }, runtime, "x".repeat(20001)), /过长/);
  runtime.beginQuit();
  assert.throws(() => writeClipboardText({ writeText: () => undefined }, runtime, "later"), /正在退出/);
});
