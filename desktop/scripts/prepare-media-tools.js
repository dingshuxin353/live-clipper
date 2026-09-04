const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
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

function prepareTools(desktop) {
  if (process.platform !== "darwin" || process.arch !== "arm64") throw new Error("Requires macOS arm64 host");
  const expected = JSON.parse(python(desktop, ["--identity"]));
  const cache = path.join(desktop, "vendor/media-tools/darwin-arm64");
  // Existing invalid caches are evidence, not permission to rebuild or overwrite.
  if (!fs.lstatSync(cache, { throwIfNoEntry: false })) python(desktop, [], true);
  return verifyCache(desktop, expected);
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
