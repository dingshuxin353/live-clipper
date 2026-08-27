const assert = require("node:assert/strict");
const test = require("node:test");

const { createOutputActions, createRuntimeState } = require("../runtime-state");

test("output actions resolve trusted paths by output ID and never accept renderer paths", async () => {
  const resolved = [];
  const opened = [];
  const revealed = [];
  const actions = createOutputActions({
    client: {
      resolveOutputPath: async (outputId) => {
        resolved.push(outputId);
        return { path: `/private/outputs/${outputId}.mp4` };
      },
    },
    shell: {
      openPath: async (target) => { opened.push(target); return ""; },
      showItemInFolder: (target) => { revealed.push(target); },
    },
    runtime: createRuntimeState(),
  });

  assert.deepEqual(await actions.openOutput("output-1"), { ok: true });
  assert.deepEqual(await actions.revealOutput("output-2"), { ok: true });
  assert.deepEqual(resolved, ["output-1", "output-2"]);
  assert.deepEqual(opened, ["/private/outputs/output-1.mp4"]);
  assert.deepEqual(revealed, ["/private/outputs/output-2.mp4"]);
});

test("repeated output operations share one in-flight backend lookup", async () => {
  let resolveRequest;
  let requests = 0;
  const actions = createOutputActions({
    client: {
      resolveOutputPath: () => {
        requests += 1;
        return new Promise((resolve) => { resolveRequest = resolve; });
      },
    },
    shell: { openPath: async () => "", showItemInFolder: () => undefined },
    runtime: createRuntimeState(),
  });

  const first = actions.openOutput("output-1");
  const second = actions.openOutput("output-1");
  await Promise.resolve();
  assert.equal(requests, 1);
  resolveRequest({ path: "/private/output.mp4" });
  assert.deepEqual(await first, { ok: true });
  assert.deepEqual(await second, { ok: true });
});

test("output actions reject invalid IDs, backend paths, shell failures, and quitting", async () => {
  const runtime = createRuntimeState();
  const actions = createOutputActions({
    client: { resolveOutputPath: async () => ({ path: "relative/output.mp4" }) },
    shell: { openPath: async () => "not found", showItemInFolder: () => undefined },
    runtime,
  });

  await assert.rejects(actions.openOutput("output-1"), /路径无法读取/);
  assert.throws(() => actions.openOutput(""), /output_id无效/);
  runtime.beginQuit();
  assert.throws(() => actions.revealOutput("output-1"), /正在退出/);

  const shellFailure = createOutputActions({
    client: { resolveOutputPath: async () => ({ path: "/private/output.mp4" }) },
    shell: { openPath: async () => "not found", showItemInFolder: () => undefined },
    runtime: createRuntimeState(),
  });
  await assert.rejects(shellFailure.openOutput("output-1"), /无法打开成片/);

  const revealFailure = createOutputActions({
    client: { resolveOutputPath: async () => ({ path: "/private/secret/output.mp4" }) },
    shell: {
      openPath: async () => "",
      showItemInFolder: () => { throw new Error("failed at /private/secret/output.mp4"); },
    },
    runtime: createRuntimeState(),
  });
  await assert.rejects(revealFailure.revealOutput("output-1"), (error) => {
    assert.match(error.message, /Finder/);
    assert.equal(error.message.includes("/private/secret"), false);
    return true;
  });
});
