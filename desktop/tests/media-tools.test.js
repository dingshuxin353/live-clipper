const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const childProcess = require("node:child_process");
const { createHash } = require("node:crypto");
const vm = require("node:vm");
const { createRequire } = require("node:module");
const { checkMediaTools } = require("../media-runtime");
const { default: beforePack, prepareTools, verifyCache } = require("../scripts/prepare-media-tools");
const { default: afterPack } = require("../scripts/after-pack");

test.beforeEach(() => {
  delete process.env.VENUS_RELEASE_MEDIA_CACHE;
  delete process.env.VENUS_RELEASE_ARCHIVE_CACHE;
});

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

test("beforePack rejects unsupported targets before touching tools", async () => {
  for (const [electronPlatformName, arch] of [["linux", 3], ["darwin", 1], ["darwin", "arm64"]]) {
    await assert.rejects(beforePack({ electronPlatformName, arch }), /macOS arm64 build target/);
  }
});

function cacheFixture(t) {
  const desktop = temporary(t);
  const root = path.join(desktop, "vendor/media-tools/darwin-arm64");
  fs.mkdirSync(path.join(root, "licenses"), { recursive: true });
  const manifest = { identity: { version: "1.0.1", inputs: {} }, outputs: {}, tools: {} };
  for (const name of ["ffmpeg", "ffprobe"]) {
    const version = name + " version 9.0.1 test\n";
    fs.writeFileSync(path.join(root, name), "#!/bin/sh\nif [ \"$1\" = \"-L\" ]; then echo 'GNU General Public License version 2'; else echo '" + version.trim() + "'; fi\n", { mode: 0o755 });
    manifest.tools[name] = { version };
  }
  for (const name of ["media-sources.tar.gz", "licenses/SOURCE.md", "licenses/COMPONENTS.md", "licenses/GPL-2.0.txt", "licenses/CORRESPONDING-SOURCE.md"]) {
    fs.writeFileSync(path.join(root, name), "unit test fixture");
  }
  for (const name of ["ffmpeg", "ffprobe", "media-sources.tar.gz", "licenses/SOURCE.md", "licenses/COMPONENTS.md", "licenses/GPL-2.0.txt", "licenses/CORRESPONDING-SOURCE.md"]) {
    const bytes = fs.readFileSync(path.join(root, name));
    manifest.outputs[name] = { size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  }
  fs.writeFileSync(path.join(root, "build-manifest.json"), JSON.stringify(manifest));
  return { desktop, root, manifest };
}

test("valid cache is rehashed and reused without running the builder", (t) => {
  const { desktop, manifest } = cacheFixture(t);
  const calls = [];
  t.mock.method(childProcess, "execFileSync", (file, args) => {
    calls.push(args);
    assert.ok(args.includes("--identity"));
    return JSON.stringify(manifest.identity);
  });
  assert.deepEqual(prepareTools(desktop), manifest);
  assert.equal(calls.length, 1);
});

test("stale identity, missing artifacts, wrong hashes and escaped files fail closed", (t) => {
  const { desktop, root, manifest } = cacheFixture(t);
  assert.throws(() => verifyCache(desktop, { ...manifest.identity, version: "1.0.2" }), /stale/);
  const extra = path.join(root, "licenses/private.log");
  fs.writeFileSync(extra, "not a license");
  assert.throws(() => verifyCache(desktop, manifest.identity), /Unexpected media license/);
  fs.unlinkSync(extra);
  for (const name of ["ffprobe", "media-sources.tar.gz", "licenses/COMPONENTS.md"]) {
    const file = path.join(root, name);
    const bytes = fs.readFileSync(file);
    fs.writeFileSync(file, "tampered");
    assert.throws(() => verifyCache(desktop, manifest.identity), /hash mismatch/);
    fs.unlinkSync(file);
    assert.throws(() => verifyCache(desktop, manifest.identity), /missing/);
    fs.symlinkSync("/bin/echo", file);
    assert.throws(() => verifyCache(desktop, manifest.identity), /unsafe/);
    fs.unlinkSync(file);
    fs.writeFileSync(file, bytes, { mode: 0o755 });
  }
  const calls = [];
  t.mock.method(childProcess, "execFileSync", (_, args) => {
    calls.push(args);
    return JSON.stringify({ version: "changed" });
  });
  assert.throws(() => prepareTools(desktop), /stale/);
  assert.equal(calls.length, 1);
});

test("a missing cache invokes the source builder and propagates failure without installing", (t) => {
  const desktop = temporary(t);
  let calls = 0;
  t.mock.method(childProcess, "execFileSync", (_, args) => {
    calls += 1;
    if (args.includes("--identity")) return "{}";
    throw new Error("source build failed");
  });
  assert.throws(() => prepareTools(desktop), /source build failed/);
  assert.equal(calls, 2);
  assert.equal(fs.existsSync(path.join(desktop, "vendor/media-tools/darwin-arm64")), false);
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
      process: { env: { LIVE_CLIPPER_HOME: path.join(root, "app-home") }, resourcesPath: root },
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

test("shared media cache copies verified bytes and refuses corrupt reuse", (t) => {
  const { desktop, manifest } = cacheFixture(t);
  const sharedRoot = fs.realpathSync(temporary(t));
  const previous = process.env.VENUS_RELEASE_MEDIA_CACHE;
  process.env.VENUS_RELEASE_MEDIA_CACHE = sharedRoot;
  t.after(() => {
    if (previous === undefined) delete process.env.VENUS_RELEASE_MEDIA_CACHE;
    else process.env.VENUS_RELEASE_MEDIA_CACHE = previous;
  });
  t.mock.method(childProcess, "execFileSync", (_, args) => {
    assert.ok(args.includes("--identity"), "Cache reuse must not rebuild");
    return JSON.stringify(manifest.identity);
  });
  assert.deepEqual(prepareTools(desktop), manifest);
  const other = temporary(t);
  assert.deepEqual(prepareTools(other), manifest);
  const key = createHash("sha256").update(JSON.stringify(manifest.identity)).digest("hex");
  const cached = path.join(sharedRoot, key, "vendor/media-tools/darwin-arm64/ffprobe");
  const local = path.join(other, "vendor/media-tools/darwin-arm64/ffprobe");
  assert.notEqual(fs.statSync(cached).ino, fs.statSync(local).ino);
  fs.writeFileSync(cached, "corrupt");
  assert.throws(() => prepareTools(temporary(t)), /hash mismatch/);
});

for (const populated of [false, true]) {
  test(`media tests preserve inherited ${populated ? "populated" : "empty"} release caches`, (t) => {
    const root = fs.realpathSync(temporary(t));
    const media = path.join(root, "media");
    const archives = path.join(root, "archives");
    for (const directory of [media, archives]) {
      fs.mkdirSync(directory);
      if (populated) {
        fs.mkdirSync(path.join(directory, "sentinel"));
        fs.writeFileSync(path.join(directory, "sentinel", "keep"), "external cache bytes");
      }
    }
    if (populated) fs.writeFileSync(path.join(media, "entries.json"), "{}\n");
    function snapshot(directory) {
      return ["", ...fs.readdirSync(directory, { recursive: true })].sort().map((name) => {
        const file = path.join(directory, name);
        const stat = fs.lstatSync(file);
        return [name, {
          device: stat.dev, inode: stat.ino, mode: stat.mode,
          ...(stat.isDirectory() ? { directory: true } : {
            size: stat.size, sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"),
          }),
        }];
      });
    }
    const before = [snapshot(media), snapshot(archives)];
    const names = [
      "valid cache is rehashed and reused without running the builder",
      "stale identity, missing artifacts, wrong hashes and escaped files fail closed",
      "a missing cache invokes the source builder and propagates failure without installing",
      "shared media cache copies verified bytes and refuses corrupt reuse",
    ];
    const env = { ...process.env, TMPDIR: root, VENUS_RELEASE_MEDIA_CACHE: media, VENUS_RELEASE_ARCHIVE_CACHE: archives };
    // Start an independent runner rather than inheriting the parent's test worker context.
    delete env.NODE_TEST_CONTEXT;
    const result = childProcess.spawnSync(process.execPath, [
      "--test", "--test-reporter=tap", `--test-name-pattern=^(${names.join("|")})$`, __filename,
    ], {
      env, encoding: "utf8", timeout: 30000,
    });
    assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
    assert.match(result.stdout, /^# pass 4$/m);
    assert.deepEqual([snapshot(media), snapshot(archives)], before);
  });
}

test("registration rejects invalid or linked indexes before creating shared entries", (t) => {
  const { desktop, manifest } = cacheFixture(t);
  t.mock.method(childProcess, "execFileSync", (_, args) => {
    assert.ok(args.includes("--identity"));
    return JSON.stringify(manifest.identity);
  });
  for (const kind of ["broken", "array", "symlink"]) {
    const root = fs.realpathSync(temporary(t));
    const index = path.join(root, "entries.json");
    const external = path.join(fs.realpathSync(temporary(t)), "sentinel");
    fs.writeFileSync(external, "{}\n");
    if (kind === "symlink") fs.symlinkSync(external, index);
    else fs.writeFileSync(index, kind === "array" ? "[]" : "{");
    const before = fs.readdirSync(root);
    process.env.VENUS_RELEASE_MEDIA_CACHE = root;
    assert.throws(() => prepareTools(desktop));
    assert.deepEqual(fs.readdirSync(root), before);
    assert.equal(fs.readFileSync(external, "utf8"), "{}\n");
  }
});

test("incomplete shared copies remain unregistered and cannot be adopted", (t) => {
  const { desktop, manifest } = cacheFixture(t);
  const root = fs.realpathSync(temporary(t));
  process.env.VENUS_RELEASE_MEDIA_CACHE = root;
  t.mock.method(childProcess, "execFileSync", (_, args) => {
    assert.ok(args.includes("--identity"));
    return JSON.stringify(manifest.identity);
  });
  const copy = fs.cpSync;
  const interrupted = t.mock.method(fs, "cpSync", (from, to, options) => {
    copy(from, to, options);
    throw new Error("test copy interruption");
  });
  assert.throws(() => prepareTools(desktop), /copy interruption/);
  interrupted.mock.restore();
  assert.equal(fs.existsSync(path.join(root, "entries.json")), false);
  const names = fs.readdirSync(root);
  assert.throws(() => prepareTools(desktop), /ownership mismatch/);
  assert.deepEqual(fs.readdirSync(root), names);
  assert.equal(fs.existsSync(path.join(root, "entries.json")), false);
});
