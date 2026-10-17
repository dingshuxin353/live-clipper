const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const childProcess = require("node:child_process");
const { bundledFile, runTool } = require("../media-runtime");

function python(desktop, args, inherit = false) {
  return childProcess.execFileSync("python3.11", ["-I", path.join(desktop, "scripts/build-media-tools.py"), ...args], {
    encoding: "utf8", timeout: inherit ? 0 : 30000, killSignal: "SIGKILL",
    maxBuffer: 1024 * 1024, stdio: inherit ? "inherit" : ["ignore", "pipe", "pipe"],
  });
}

function fileIdentity(file) {
  return { size: fs.statSync(file).size, sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex") };
}

function same(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

function verifyCache(desktop, expected) {
  const relative = "vendor/media-tools/darwin-arm64";
  const root = path.join(desktop, relative);
  const record = bundledFile(desktop, `${relative}/build-manifest.json`, false);
  const manifest = JSON.parse(fs.readFileSync(record, "utf8"));
  if (!same(manifest.identity, expected)) throw new Error("Media build identity is stale");
  const names = ["ffmpeg", "ffprobe", "media-sources.tar.gz", "licenses/SOURCE.md",
    "licenses/COMPONENTS.md", "licenses/GPL-2.0.txt", "licenses/CORRESPONDING-SOURCE.md"];
  if (!same(Object.keys(manifest.outputs).sort(), [...names].sort())) throw new Error("Incomplete media manifest");
  for (const name of names) {
    const file = bundledFile(desktop, `${relative}/${name}`, name === "ffmpeg" || name === "ffprobe");
    if (!same(fileIdentity(file), manifest.outputs[name])) throw new Error(`Media cache hash mismatch: ${name}`);
  }
  const licenses = names.filter((name) => name.startsWith("licenses/")).map((name) => path.basename(name));
  if (!same(fs.readdirSync(path.join(root, "licenses")).sort(), licenses.sort())) {
    throw new Error("Unexpected media license files");
  }
  for (const name of ["ffmpeg", "ffprobe"]) {
    const version = runTool(path.join(root, name), "-version");
    const license = runTool(path.join(root, name), "-L");
    if (!version.startsWith(`${name} version 9.0.1 `)
      || /nonfree|not legally redistributable|--enable-version3/i.test(version + license)
      || !/GNU General Public License/.test(license) || !/version 2/.test(license)
      || /\/(?:Users|home|Volumes)\/[A-Za-z0-9]/.test(version + license)) {
      throw new Error(`${name}: unexpected version/license or private path`);
    }
    if (manifest.tools[name].version !== version) throw new Error(`${name}: version record mismatch`);
  }
  return manifest;
}

function canonical(file) {
  if (!path.isAbsolute(file) || path.normalize(file) !== file) throw new Error("Unsafe media path");
  let current = path.parse(file).root;
  for (const part of file.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    const facts = fs.lstatSync(current, { throwIfNoEntry: false });
    if (facts?.isSymbolicLink()) throw new Error("Unsafe media path alias");
    if (facts && fs.realpathSync(current) !== current) throw new Error("Unsafe media path alias");
  }
  return file;
}

function readEntries(root) {
  canonical(root);
  const facts = fs.lstatSync(root, { throwIfNoEntry: false });
  if (facts && !facts.isDirectory()) throw new Error("Unsafe media root");
  const index = canonical(path.join(root, "entries.json"));
  const indexFacts = fs.lstatSync(index, { throwIfNoEntry: false });
  if (!indexFacts) return {};
  if (!indexFacts.isFile()) throw new Error("Unsafe media index");
  const fd = fs.openSync(index, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  let entries;
  try {
    if (!fs.fstatSync(fd).isFile()) throw new Error("Unsafe media index");
    entries = JSON.parse(fs.readFileSync(fd, "utf8"));
  } finally { fs.closeSync(fd); }
  if (!entries || Array.isArray(entries) || typeof entries !== "object") throw new Error("Invalid media index");
  for (const [key, value] of Object.entries(entries)) {
    if (!/^[0-9a-f]{64}$/.test(key) || !value || typeof value.version !== "string" || !value.version
      || !Number.isSafeInteger(value.device) || value.device < 0
      || !Number.isSafeInteger(value.inode) || value.inode <= 0) throw new Error("Invalid media index entry");
  }
  return entries;
}

function ownedEntry(root, key, entry) {
  const directory = canonical(path.join(root, key));
  const facts = fs.lstatSync(directory, { throwIfNoEntry: false });
  if (facts && (!facts.isDirectory() || facts.dev !== entry.device || facts.ino !== entry.inode)) {
    throw new Error("Media cache identity changed");
  }
  return facts;
}

function checkRoot(root, expected) {
  canonical(root);
  const current = fs.statSync(root);
  if (!current.isDirectory() || current.dev !== expected.dev || current.ino !== expected.ino) throw new Error("Media root identity changed");
}

function saveEntries(root, entries, rootFacts) {
  checkRoot(root, rootFacts);
  readEntries(root); // Recheck the index boundary before replacing it.
  const index = path.join(root, "entries.json");
  const temporary = path.join(root, `.entries-${randomUUID()}.tmp`);
  const fd = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(entries, null, 2) + "\n");
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  try {
    checkRoot(root, rootFacts);
    canonical(index);
    fs.renameSync(temporary, index);
    const directory = fs.openSync(root, "r");
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
  } finally { fs.rmSync(temporary, { force: true }); }
}

function treeBytes(root, checkSafety = false) {
  const base = fs.lstatSync(root, { throwIfNoEntry: false });
  if (!base) return 0;
  function visit(file) {
    const facts = fs.lstatSync(file);
    if (checkSafety) {
      const resolved = fs.realpathSync(file);
      if (resolved !== root && !resolved.startsWith(root + path.sep)) throw new Error("Escaping media symlink");
      const parent = fs.statSync(path.dirname(file));
      if (!facts.isSymbolicLink() && (facts.dev !== base.dev || facts.dev !== parent.dev
        || (facts.isDirectory() && facts.ino === parent.ino))) throw new Error("Mounted media path");
    }
    if (!facts.isDirectory()) return facts.size;
    return fs.readdirSync(file).reduce((total, name) => total + visit(path.join(file, name)), 0);
  }
  return visit(root);
}

function inspectEntry(root, key, entry, keepVersions) {
  const directory = path.join(root, key);
  const result = { key, path: directory, version: entry.version, bytes_before: null, action: "refused", reason: "" };
  try {
    result.bytes_before = treeBytes(directory);
    if (keepVersions.includes(entry.version)) return { ...result, action: "keep", reason: "retained_version" };
    if (!ownedEntry(root, key, entry)) return { ...result, action: "absent", reason: "already_absent" };
    treeBytes(directory, true);
    const occupied = childProcess.spawnSync("lsof", ["-t", "+D", directory], { encoding: "utf8" });
    if (occupied.error || occupied.signal || occupied.status !== 1 || occupied.stdout.trim() || occupied.stderr.trim()) {
      throw new Error("Media cache occupied or occupancy check failed");
    }
    ownedEntry(root, key, entry);
    return { ...result, action: "delete", reason: "version_not_retained" };
  } catch (error) { return { ...result, reason: error.message }; }
}

function cleanupMedia({ root, keep_versions: keepVersions, operation }) {
  if (!Array.isArray(keepVersions) || keepVersions.some((v) => typeof v !== "string")
    || !["preview", "apply"].includes(operation)) throw new Error("Invalid media cleanup request");
  const result = { root, operation, complete: true, items: [] };
  let entries;
  try { entries = readEntries(root); }
  catch (error) { return { ...result, complete: false, reason: error.message }; }
  const rootFacts = fs.lstatSync(root, { throwIfNoEntry: false });
  if (!rootFacts) return result;
  const names = Object.keys(entries);
  for (const key of names) {
    const item = inspectEntry(root, key, entries[key], keepVersions);
    result.items.push(item);
    if (item.action === "refused") result.complete = false;
    if (operation !== "apply" || !["delete", "absent"].includes(item.action)) continue;
    try {
      checkRoot(root, rootFacts);
      ownedEntry(root, key, entries[key]);
      if (item.action === "delete") {
        fs.rmSync(item.path, { recursive: true });
        if (fs.lstatSync(item.path, { throwIfNoEntry: false })) throw new Error("Media directory remains");
        item.action = "deleted";
      }
    } catch (error) {
      item.action = "refused";
      item.reason = error.message;
      result.complete = false;
      continue;
    }
    delete entries[key];
    try { saveEntries(root, entries, rootFacts); item.index_saved = true; }
    catch (error) {
      item.index_saved = false;
      result.complete = false;
      result.reason = `Media index save failed: ${error.message}`;
      for (const pending of names.slice(result.items.length)) {
        result.items.push({ key: pending, path: path.join(root, pending), version: entries[pending].version,
          bytes_before: null, action: "pending", reason: "index_save_failed" });
      }
      break;
    }
  }
  for (const name of fs.readdirSync(root)) {
    if (name === "entries.json" || names.includes(name)) continue;
    const file = path.join(root, name);
    let bytes = null;
    try { bytes = treeBytes(file); } catch { /* Unknown contents remain manual-only. */ }
    result.items.push({ key: null, path: file, version: null, bytes_before: bytes, action: "unknown", reason: "not_registered" });
  }
  return result;
}

function prepareTools(desktop) {
  if (process.platform !== "darwin" || process.arch !== "arm64") throw new Error("Requires macOS arm64 host");
  const expected = JSON.parse(python(desktop, ["--identity"]));
  const cache = path.join(desktop, "vendor/media-tools/darwin-arm64");
  // Existing invalid caches are evidence, not permission to rebuild or overwrite.
  const sharedRoot = process.env.VENUS_RELEASE_MEDIA_CACHE;
  let sharedDesktop, sharedEntries, sharedFacts, key;
  if (sharedRoot) {
    sharedEntries = readEntries(sharedRoot);
    sharedFacts = fs.statSync(sharedRoot);
    key = createHash("sha256").update(JSON.stringify(expected)).digest("hex");
    sharedDesktop = path.join(sharedRoot, key);
    const exists = fs.lstatSync(sharedDesktop, { throwIfNoEntry: false });
    if (exists || sharedEntries[key]) {
      if (!sharedEntries[key] || sharedEntries[key].version !== expected.version
        || !ownedEntry(sharedRoot, key, sharedEntries[key])) throw new Error("Shared media entry ownership mismatch");
      verifyCache(sharedDesktop, expected);
      if (!fs.lstatSync(cache, { throwIfNoEntry: false })) {
        fs.mkdirSync(path.dirname(cache), { recursive: true });
        fs.cpSync(path.join(sharedDesktop, "vendor/media-tools/darwin-arm64"), cache, { recursive: true, errorOnExist: true, force: false });
      }
    }
  }
  if (!fs.lstatSync(cache, { throwIfNoEntry: false })) python(desktop, [], true);
  const manifest = verifyCache(desktop, expected);
  if (sharedDesktop && !sharedEntries[key]) {
    checkRoot(sharedRoot, sharedFacts);
    fs.mkdirSync(sharedDesktop);
    const facts = fs.statSync(sharedDesktop);
    fs.mkdirSync(path.join(sharedDesktop, "vendor/media-tools"), { recursive: true });
    fs.cpSync(cache, path.join(sharedDesktop, "vendor/media-tools/darwin-arm64"), { recursive: true, errorOnExist: true, force: false });
    verifyCache(sharedDesktop, expected);
    const entry = { version: expected.version, device: facts.dev, inode: facts.ino };
    ownedEntry(sharedRoot, key, entry);
    sharedEntries[key] = entry;
    saveEntries(sharedRoot, sharedEntries, sharedFacts);
  }
  return manifest;
}

exports.default = async function beforePack(context) {
  // builder-util Arch.arm64 = 3; never infer the target from the build host.
  if (context.electronPlatformName !== "darwin" || context.arch !== 3) {
    throw new Error("Bundled media tools support only the macOS arm64 build target");
  }
  const manifest = prepareTools(context.packager.projectDir);
  console.log(`[media-tools] verified ${manifest.identity.version}: ${manifest.outputs["media-sources.tar.gz"].sha256}`);
};
exports.prepareTools = prepareTools;
exports.verifyCache = verifyCache;

if (require.main === module) {
  try {
    const request = JSON.parse(fs.readFileSync(0, "utf8"));
    if (!same(Object.keys(request).sort(), ["keep_versions", "operation", "root"])) throw new Error("Invalid media cleanup request");
    console.log(JSON.stringify(cleanupMedia(request)));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
