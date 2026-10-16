// Release-only probes. Never stages a native update or installs an application.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");

function digest(file, algorithm = "sha256", encoding = "hex") {
  const hash = crypto.createHash(algorithm);
  const fd = fs.openSync(file, "r");
  try {
    const buffer = Buffer.alloc(1024 * 1024);
    let size;
    while ((size = fs.readSync(fd, buffer))) hash.update(buffer.subarray(0, size));
  } finally { fs.closeSync(fd); }
  return hash.digest(encoding);
}

function requireFact(condition, message) {
  if (!condition) throw Object.assign(new Error(message), { code: "INPUT_OR_CONTENT_MISMATCH" });
}

function atomicJson(file, data) {
  requireFact(!fs.existsSync(file), "Evidence already exists");
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  const fd = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(data, null, 2) + "\n");
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  fs.renameSync(temporary, file);
  const directory = fs.openSync(path.dirname(file), "r");
  try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
}

function tree(directory) {
  const entries = {};
  function visit(parent) {
    for (const item of fs.readdirSync(parent).sort()) {
      const file = path.join(parent, item);
      const stat = fs.lstatSync(file);
      const key = path.relative(directory, file);
      requireFact(!stat.isSymbolicLink(), "Unexpected extracted link");
      if (stat.isDirectory()) { entries[key] = "directory"; visit(file); }
      else { requireFact(stat.isFile(), "Unexpected extracted entry"); entries[key] = digest(file); }
    }
  }
  visit(directory);
  return entries;
}

async function main() {
  const [mode, app, source, scope, expectedVersion, feed, inputFile, resultFile] = process.argv.slice(2);
  let phase = "input";
  try {
  const fromSource = createRequire(path.join(source, "desktop/package.json"));
  if (mode === "blockmap") {
    const { buildBlockMap } = fromSource("app-builder-lib/out/targets/blockmap/blockmap");
    await buildBlockMap(app, "gzip", `${app}.blockmap`);
    return;
  }
  if (mode !== "provider" || !path.isAbsolute(app) || !path.isAbsolute(scope)) {
    throw new Error("Expected provider and absolute isolated paths");
  }
  requireFact(path.isAbsolute(inputFile) && path.isAbsolute(resultFile), "Absolute evidence paths required");
  const inputs = JSON.parse(fs.readFileSync(inputFile));
  const config = path.join(app, "Contents/Resources/app-update.yml");
  const asar = path.join(app, "Contents/Resources/app.asar");
  requireFact(digest(config) === inputs.config_sha256 && digest(asar) === inputs.asar_sha256, "Previous App changed");
  const extracted = path.join(scope, "previous-app");
  const extractionFile = `${resultFile}.extracted.json`;
  const discoveryFile = `${resultFile}.discovery.json`;
  if (fs.existsSync(extracted)) {
    requireFact(fs.existsSync(extractionFile), "Unverified existing extraction");
    const saved = JSON.parse(fs.readFileSync(extractionFile));
    requireFact(saved.asar_sha256 === inputs.asar_sha256 && JSON.stringify(saved.files) === JSON.stringify(tree(extracted)), "Extracted App changed");
  } else {
    requireFact(!fs.existsSync(extractionFile), "Recorded extraction disappeared");
    fromSource("@electron/asar").extractAll(asar, extracted);
    atomicJson(extractionFile, { asar_sha256: inputs.asar_sha256, files: tree(extracted) });
  }
  const fromPrevious = createRequire(path.join(extracted, "package.json"));
  const pkg = fromPrevious("./package.json");
  const { AppUpdater } = fromPrevious("electron-updater/out/AppUpdater");
  const { ElectronHttpExecutor } = fromPrevious("electron-updater/out/electronHttpExecutor");
  const { NodeHttpExecutor } = fromSource("builder-util/out/nodeHttpExecutor");
  const updaterVersion = fromPrevious("electron-updater/package.json").version;
  const destination = path.join(scope, "download.zip");

  let discovery;
  let recovered = false;
  function validDownload() {
    if (!fs.existsSync(destination)) return false;
    requireFact(!fs.lstatSync(destination).isSymbolicLink(), "Download is a link");
    return fs.statSync(destination).size === inputs.expected.size &&
      digest(destination) === inputs.expected.sha256 && digest(destination, "sha512", "base64") === inputs.expected.sha512;
  }
  function validateDiscovery(saved) {
    requireFact(JSON.stringify(saved.inputs) === JSON.stringify(inputs) && saved.version === expectedVersion &&
      saved.metadata_sha256 === inputs.metadata_sha256 && saved.updater_version === updaterVersion && saved.previous_version === pkg.version &&
      saved.transport === "node-http" && saved.provider === (feed === "github" ? "github" : "generic-loopback") &&
      saved.file.name === inputs.expected.name && saved.file.size === inputs.expected.size &&
      saved.file.sha512 === inputs.expected.sha512, "Discovery identity changed");
  }
  if (fs.existsSync(discoveryFile)) {
    discovery = JSON.parse(fs.readFileSync(discoveryFile));
    validateDiscovery(discovery);
    recovered = validDownload();
  }

  // Use the previous app's provider, version comparison, digest checking and downloader;
  // Node transport substitutes for Electron net. No Squirrel/native installer is started.
  class Transport extends NodeHttpExecutor {
    async request(options, ...args) {
      const result = await super.request(options, ...args);
      if (options.path?.split("?")[0].endsWith("/latest-mac.yml")) {
        requireFact(crypto.createHash("sha256").update(result).digest("hex") === inputs.metadata_sha256, "Update metadata changed");
      }
      return result;
    }
    download(...args) { return ElectronHttpExecutor.prototype.download.apply(this, args); }
  }
  class DownloadProbe extends AppUpdater {
    async doDownloadUpdate(options) {
      const { provider, info } = options.updateInfoAndProvider;
      const files = provider.resolveFiles(info).filter(file => file.url.pathname.endsWith("-arm64-mac.zip"));
      if (files.length !== 1) throw new Error("Expected one arm64 ZIP");
      const file = files[0];
      requireFact(path.basename(file.url.pathname) === inputs.expected.name && file.info.size === inputs.expected.size &&
        file.info.sha512 === inputs.expected.sha512 && info.version === expectedVersion, "Discovered update differs from frozen ZIP");
      const discovered = { inputs, metadata_sha256: inputs.metadata_sha256, version: info.version, previous_version: pkg.version, updater_version: updaterVersion,
        provider: feed === "github" ? "github" : "generic-loopback", transport: "node-http",
        file: { name: inputs.expected.name, size: file.info.size, sha512: file.info.sha512,
          origin: file.url.origin, pathname: file.url.pathname }, discovered_at: new Date().toISOString() };
      if (discovery) {
        requireFact(JSON.stringify(discovery.file) === JSON.stringify(discovered.file), "Update source changed");
      } else {
        discovery = discovered;
        atomicJson(discoveryFile, discovery);
      }
      if (fs.existsSync(destination)) {
        requireFact(!fs.lstatSync(destination).isSymbolicLink(), "Download is a link");
        fs.unlinkSync(destination);
      }
      phase = "download";
      await this.httpExecutor.download(files[0].url, destination, {
        sha512: files[0].info.sha512,
        cancellationToken: options.cancellationToken,
        headers: options.requestHeaders,
      });
      return [destination];
    }
  }
  const adapter = {
    version: pkg.version, name: pkg.name, isPackaged: true,
    appUpdateConfigPath: path.join(app, "Contents/Resources/app-update.yml"),
    userDataPath: scope, baseCachePath: scope,
    whenReady: async () => {},
  };
  const updater = new DownloadProbe(null, adapter);
  updater.httpExecutor = new Transport();
  updater.logger = null;
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  if (feed !== "github") {
    const url = new URL(feed);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") throw new Error("Local feed must be loopback");
    updater.setFeedURL({ provider: "generic", url: feed, useMultipleRangeRequest: false });
  }
  if (!recovered) {
    phase = "discovery";
    const check = await updater.checkForUpdates();
    requireFact(check?.isUpdateAvailable && check.updateInfo.version === expectedVersion, "Expected update was not discovered");
    await updater.downloadUpdate();
  }
  phase = "content-check";
  requireFact(discovery && validDownload(), "Downloaded ZIP content differs");
  const report = {
    previous_version: pkg.version, updater_version: updaterVersion, version: expectedVersion,
    sha256: inputs.expected.sha256, sha512: inputs.expected.sha512, size: fs.statSync(destination).size,
    provider: feed === "github" ? "github" : "generic-loopback", transport: "node-http",
    native_installer_executed: false,
    discovery_sha256: digest(discoveryFile), extraction_sha256: digest(extractionFile),
  };
  phase = "save-completion";
  atomicJson(resultFile, { inputs, result: report, completed_at: new Date().toISOString(), exit_code: null,
    recovery: recovered ? "local-content-check" : "download" });
  process.stdout.write(JSON.stringify(report) + "\n");
  } catch (error) {
    const category = error.code === "INPUT_OR_CONTENT_MISMATCH" ? "input-or-content-mismatch" :
      (phase === "discovery" || phase === "download") ? "network-or-provider" : "unknown";
    if (resultFile && path.isAbsolute(resultFile) && fs.existsSync(path.dirname(resultFile))) {
      atomicJson(`${resultFile}.${crypto.randomUUID()}.error.json`, {
        phase, category, exit_code: null, failed_at: new Date().toISOString(),
        code: /^[A-Z0-9_]+$/.test(error.code || "") ? error.code : null,
      });
    }
    throw Object.assign(new Error(`Provider ${phase}: ${category}`), { safe: true });
  }
}

main().catch(error => {
  // Messages from remote libraries can include credentials and signed query strings.
  process.stderr.write((error.safe ? error.message : "Provider unknown error") + "; preserve completion evidence.\n");
  process.exitCode = 1;
});
