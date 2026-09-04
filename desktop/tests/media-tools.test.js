const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const https = require("node:https");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const { createHash } = require("node:crypto");
const vm = require("node:vm");
const { createRequire } = require("node:module");
const { checkMediaTools } = require("../media-runtime");
const { default: beforePack, prepareTools, download, verify, TOOLS } = require("../scripts/prepare-media-tools");
const { default: afterPack } = require("../scripts/after-pack");

function temporary(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "venus-media-tools-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "bin"));
  return root;
}

function executable(root, name, body = `echo '${name} version 9.0.1-test'`) {
  fs.writeFileSync(path.join(root, "bin", name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
}

test("both bundled tools run by absolute path", (t) => {
  const root = temporary(t);
  for (const name of ["ffmpeg", "ffprobe"]) executable(root, name);
  assert.deepEqual(Object.keys(checkMediaTools(root)), ["ffmpeg", "ffprobe"]);
});

test("missing ffprobe fails even when another executable is on PATH", (t) => {
  const root = temporary(t);
  executable(root, "ffmpeg");
  const external = temporary(t);
  executable(external, "ffprobe");
  const previousPath = process.env.PATH;
  t.after(() => { process.env.PATH = previousPath; });
  process.env.PATH = `${external}/bin:${previousPath}`;
  assert.throws(() => checkMediaTools(root), /ffprobe.*missing/);
});

test("permissions, non-regular files and escaping symlinks are rejected", (t) => {
  const root = temporary(t);
  executable(root, "ffmpeg");
  const probe = path.join(root, "bin", "ffprobe");
  executable(root, "ffprobe");
  fs.chmodSync(probe, 0o644);
  assert.throws(() => checkMediaTools(root), /not executable/);
  fs.unlinkSync(probe);
  fs.mkdirSync(probe);
  assert.throws(() => checkMediaTools(root), /unsafe file/);
  fs.rmdirSync(probe);
  fs.symlinkSync("/bin/echo", probe);
  assert.throws(() => checkMediaTools(root), /unsafe file/);
  const outside = temporary(t);
  for (const tool of ["ffmpeg", "ffprobe"]) executable(outside, tool);
  const escaped = temporary(t);
  fs.rmdirSync(path.join(escaped, "bin"));
  fs.symlinkSync(path.join(outside, "bin"), path.join(escaped, "bin"));
  assert.throws(() => checkMediaTools(escaped), /unsafe file/);
});

test("wrong identity, nonzero exit, signal and large output fail without leaking output", (t) => {
  const root = temporary(t);
  executable(root, "ffmpeg");
  for (const body of [
    "echo 'wrong version secret'",
    "echo 'private-key' >&2; exit 7",
    "kill -TERM $$",
    "i=0; while [ $i -lt 7000 ]; do printf 'private-key'; i=$((i+1)); done",
  ]) {
    executable(root, "ffprobe", body);
    assert.throws(() => checkMediaTools(root), (error) => {
      assert.doesNotMatch(error.message, /private-key|secret|venus-media-tools-/);
      return /ffprobe/.test(error.message);
    });
  }
});

test("a hung tool is killed within the five second runtime budget", (t) => {
  const root = temporary(t);
  executable(root, "ffmpeg");
  executable(root, "ffprobe", "while :; do :; done");
  const start = Date.now();
  assert.throws(() => checkMediaTools(root), /execution failed/);
  assert.ok(Date.now() - start < 8000);
});

test("afterPack requires backend and both tools, restores mode, and runs checks", async (t) => {
  const root = temporary(t);
  const resources = path.join(root, "Venus.app", "Contents", "Resources");
  fs.mkdirSync(path.join(resources, "bin"), { recursive: true });
  fs.mkdirSync(path.join(resources, "backend"));
  for (const name of ["ffmpeg", "ffprobe"]) executable(resources, name);
  await assert.rejects(afterPack({ appOutDir: root }), /live-clipper-backend.*missing/);
  fs.writeFileSync(path.join(resources, "backend", "live-clipper-backend"), "backend");
  fs.chmodSync(path.join(resources, "bin", "ffprobe"), 0o644);
  await afterPack({ appOutDir: root });
  assert.ok(fs.statSync(path.join(resources, "bin", "ffprobe")).mode & 0o111);
  executable(resources, "ffprobe", "exit 1");
  await assert.rejects(afterPack({ appOutDir: root }), /execution failed/);
  fs.unlinkSync(path.join(resources, "bin", "ffprobe"));
  await assert.rejects(afterPack({ appOutDir: root }), /ffprobe.*missing/);
});

test("beforePack checks the target architecture and refuses missing license material", async (t) => {
  const root = temporary(t);
  for (const [electronPlatformName, arch] of [["linux", 3], ["darwin", 1], ["darwin", "arm64"]]) {
    await assert.rejects(beforePack({ electronPlatformName, arch }), /macOS arm64 build target/);
  }
  await assert.rejects(beforePack({ electronPlatformName: "darwin", arch: 3,
    packager: { projectDir: root } }), /SOURCE.md.*missing/);
  assert.equal(fs.existsSync(path.join(root, "vendor")), false);
});

test("identity checks reject changed bytes, truncated files and oversized files", () => {
  const bytes = Buffer.from("verified bytes");
  const hash = createHash("sha256").update(bytes).digest("hex");
  verify(bytes, bytes.length, hash);
  for (const candidate of [Buffer.from("tampered bytes"), bytes.subarray(1), Buffer.concat([bytes, bytes])]) {
    assert.throws(() => verify(candidate, bytes.length, hash), /SHA-256 mismatch/);
  }
});

function responseFixture(t, statusCode, send) {
  const requests = [];
  t.mock.method(https, "get", (url, callback) => {
    requests.push(url);
    const request = new EventEmitter();
    const response = new PassThrough();
    response.statusCode = statusCode;
    request.destroy = (error) => {
      response.destroy();
      if (error) request.emit("error", error);
      request.emit("close");
    };
    queueMicrotask(() => {
      callback(response);
      if (!response.destroyed) send(response, request);
    });
    response.on("end", () => request.emit("close"));
    return request;
  });
  return requests;
}

test("download uses the fixed HTTPS URL and refuses redirects and HTTP failures", async (t) => {
  for (const status of [302, 404, 503]) {
    const requests = responseFixture(t, status, () => assert.fail("must not consume body"));
    await assert.rejects(download("ffmpeg"), /download failed/);
    assert.equal(requests[0], "https://ffmpeg.martin-riedl.de/download/macos/arm64/1787073674_9.0.1/ffmpeg.zip");
    t.mock.restoreAll();
  }
  assert.throws(() => download("../ffplay"), /Unknown media tool/);
});

test("network error, truncated response and wrong hash leave no install or staging files", async (t) => {
  for (const send of [
    (_, request) => request.destroy(new Error("network secret")),
    (response) => response.destroy(new Error("connection reset")),
    (response) => response.end("truncated zip"),
    (response) => response.end(Buffer.alloc(TOOLS.ffmpeg.zipSize)),
    (response) => response.end(Buffer.alloc(TOOLS.ffmpeg.zipSize + 1)),
  ]) {
    const root = temporary(t);
    responseFixture(t, 200, send);
    await assert.rejects(prepareTools(root), /download|SHA-256 mismatch/);
    assert.deepEqual(fs.readdirSync(path.join(root, "vendor", "media-tools")), []);
    t.mock.restoreAll();
  }
});

test("download has a total deadline even if no response arrives", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(https, "get", () => {
    const request = new EventEmitter();
    request.destroy = (error) => { request.emit("error", error); request.emit("close"); };
    return request;
  });
  const pending = assert.rejects(download("ffprobe"), /download failed/);
  t.mock.timers.tick(120000);
  await pending;
});

test("partial and escaped caches are rejected without network or writes outside desktop", async (t) => {
  t.mock.method(https, "get", () => assert.fail("corrupt cache must not redownload"));
  const root = temporary(t);
  const parent = path.join(root, "vendor", "media-tools");
  fs.mkdirSync(path.join(parent, "darwin-arm64"), { recursive: true });
  await assert.rejects(prepareTools(root), /missing/);
  const outside = temporary(t);
  const escaped = temporary(t);
  fs.mkdirSync(path.join(escaped, "vendor"));
  fs.symlinkSync(outside, path.join(escaped, "vendor", "media-tools"));
  await assert.rejects(prepareTools(escaped), /escapes desktop/);
  assert.deepEqual(fs.readdirSync(outside), ["bin"]);
  const vendorEscape = temporary(t);
  fs.symlinkSync(outside, path.join(vendorEscape, "vendor"));
  await assert.rejects(prepareTools(vendorEscape), /escapes desktop/);
  assert.deepEqual(fs.readdirSync(outside), ["bin"]);
});

test("size-correct corrupt cache fails SHA verification without redownloading", async (t) => {
  const root = temporary(t);
  const cache = path.join(root, "vendor", "media-tools", "darwin-arm64");
  fs.mkdirSync(cache, { recursive: true });
  for (const [name, size] of [["ffmpeg", TOOLS.ffmpeg.size], ["ffmpeg.zip", TOOLS.ffmpeg.zipSize]]) {
    fs.writeFileSync(path.join(cache, name), "", { mode: 0o755 });
    fs.truncateSync(path.join(cache, name), size);
  }
  t.mock.method(https, "get", () => assert.fail("must not redownload"));
  await assert.rejects(prepareTools(root), /SHA-256 mismatch/);
});

test("real main startup gates packaged mode before any backend setup; development skips it", async (t) => {
  const mainPath = path.resolve(__dirname, "../main.js");
  const sourceRequire = createRequire(mainPath);
  for (const isPackaged of [true, false]) {
    const root = temporary(t);
    const stages = [];
    let ready;
    const electron = {
      app: { isPackaged, getPath: () => root, setPath() {}, on() {},
        requestSingleInstanceLock: () => true,
        whenReady: () => ({ then: (fn) => { ready = fn; } }),
        exit: (code) => stages.push(`exit:${code}`) },
      ipcMain: { handle() {} },
      dialog: { showErrorBox: (_, message) => stages.push(message) },
    };
    vm.runInNewContext(fs.readFileSync(mainPath, "utf8"), {
      __dirname: path.dirname(mainPath),
      process: { env: { LIVE_CLIPPER_HOME: root }, resourcesPath: root },
      require: (name) => {
        if (name === "electron") return electron;
        if (name === "net") return { createServer: () => { stages.push("backend setup"); throw new Error("test stop"); } };
        return sourceRequire(name);
      },
    });
    await ready();
    assert.equal(stages.includes("backend setup"), !isPackaged);
    assert.equal(stages.at(-1), "exit:1");
    if (isPackaged) assert.match(stages[0], /ffmpeg.*missing/);
  }
});
