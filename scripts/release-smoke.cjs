// Release-only probes. Never stages a native update or installs an application.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");

async function main() {
  const [mode, app, source, scope, expectedVersion, feed] = process.argv.slice(2);
  const fromSource = createRequire(path.join(source, "desktop/package.json"));
  if (mode === "blockmap") {
    const { buildBlockMap } = fromSource("app-builder-lib/out/targets/blockmap/blockmap");
    await buildBlockMap(app, "gzip", `${app}.blockmap`);
    return;
  }
  if (mode !== "provider" || !path.isAbsolute(app) || !path.isAbsolute(scope)) {
    throw new Error("Expected provider and absolute isolated paths");
  }
  const extracted = path.join(scope, "previous-app");
  if (fs.existsSync(extracted)) throw new Error("Probe directory already used");
  fromSource("@electron/asar").extractAll(path.join(app, "Contents/Resources/app.asar"), extracted);
  const fromPrevious = createRequire(path.join(extracted, "package.json"));
  const pkg = fromPrevious("./package.json");
  const { AppUpdater } = fromPrevious("electron-updater/out/AppUpdater");
  const { ElectronHttpExecutor } = fromPrevious("electron-updater/out/electronHttpExecutor");
  const { NodeHttpExecutor } = fromSource("builder-util/out/nodeHttpExecutor");
  const updaterVersion = fromPrevious("electron-updater/package.json").version;
  const destination = path.join(scope, "download.zip");

  // Use the previous app's provider, version comparison, digest checking and downloader;
  // Node transport substitutes for Electron net. No Squirrel/native installer is started.
  class Transport extends NodeHttpExecutor {
    download(...args) { return ElectronHttpExecutor.prototype.download.apply(this, args); }
  }
  class DownloadProbe extends AppUpdater {
    async doDownloadUpdate(options) {
      const { provider, info } = options.updateInfoAndProvider;
      const files = provider.resolveFiles(info).filter(file => file.url.pathname.endsWith("-arm64-mac.zip"));
      if (files.length !== 1) throw new Error("Expected one arm64 ZIP");
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
  const check = await updater.checkForUpdates();
  if (!check?.isUpdateAvailable || check.updateInfo.version !== expectedVersion) throw new Error("Expected update was not discovered");
  await updater.downloadUpdate();
  const hash = crypto.createHash("sha256");
  for await (const chunk of fs.createReadStream(destination)) hash.update(chunk);
  process.stdout.write(JSON.stringify({
    previous_version: pkg.version, updater_version: updaterVersion, version: check.updateInfo.version,
    sha256: hash.digest("hex"), size: fs.statSync(destination).size,
    provider: feed === "github" ? "github" : "generic-loopback", transport: "node-http",
    native_installer_executed: false,
  }) + "\n");
}

main().catch(() => {
  // Remote errors can contain signed URLs. Do not print credentials in probe logs.
  process.stderr.write("Release probe failed; inspect input identities and connectivity.\n");
  process.exitCode = 1;
});
