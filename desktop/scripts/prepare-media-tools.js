const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { bundledFile, runTool } = require("../media-runtime");

const BASE = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1787073674_9.0.1/";
const TOOLS = Object.freeze({
  ffmpeg: Object.freeze({ zipSize: 28447413, size: 66334032,
    zipHash: "8287a1b2229e05eb41859f073e18e6c52c60a778f2f5e6881070fe51b79407fe",
    hash: "393e4c395020a1cb7cbd77fbe00599ce69d1c6466fee0dbd59d13f86a81a1611" }),
  ffprobe: Object.freeze({ zipSize: 28370930, size: 66159232,
    zipHash: "102a26b8940a053298d9929bfaae71e4b6ef65ba5f19a99a88c433108560741a",
    hash: "7abc49fb2bdf2204f018e76dc6e0a8ae7643313bae09a9fa43e7eb12442271bc" }),
});

function verify(bytes, size, hash) {
  if (bytes.length !== size || createHash("sha256").update(bytes).digest("hex") !== hash) {
    throw new Error("Media tool size/SHA-256 mismatch");
  }
}

function download(name) {
  if (!Object.hasOwn(TOOLS, name)) throw new Error("Unknown media tool");
  return new Promise((resolve, reject) => {
    const request = https.get(`${BASE}${name}.zip`, (response) => {
      if (response.statusCode !== 200) {
        response.destroy();
        request.destroy(new Error("Media tool download requires HTTP 200"));
        return;
      }
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > TOOLS[name].zipSize) {
          request.destroy(new Error("Media tool download exceeded size limit"));
        } else chunks.push(chunk);
      });
      response.on("error", () => request.destroy(new Error("Media tool download interrupted")));
      response.on("end", () => resolve(Buffer.concat(chunks)));
    });
    const timer = setTimeout(() => request.destroy(new Error("Media tool download timed out")), 120000);
    request.on("close", () => clearTimeout(timer));
    request.on("error", () => reject(new Error("Media tool download failed")));
  });
}

function verifyPair(directory) {
  const reports = {};
  for (const [name, identity] of Object.entries(TOOLS)) {
    const zip = bundledFile(directory, `${name}.zip`, false);
    const file = bundledFile(directory, name);
    if (fs.statSync(zip).size !== identity.zipSize || fs.statSync(file).size !== identity.size) {
      throw new Error("Media tool cache size mismatch");
    }
    verify(fs.readFileSync(zip), identity.zipSize, identity.zipHash);
    verify(fs.readFileSync(file), identity.size, identity.hash);
    const version = runTool(file, "-version");
    const license = runTool(file, "-L");
    if (!version.startsWith(`${name} version 9.0.1-https://www.martin-riedl.de `)
      || /nonfree|not legally redistributable/i.test(version + license)
      || !/GNU General Public License/.test(license) || !/version 3/.test(license)) {
      throw new Error(`${name}: unexpected version/license`);
    }
    reports[name] = { version, license };
  }
  return reports;
}

async function prepareTools(desktopDirectory) {
  const parent = path.join(desktopDirectory, "vendor", "media-tools");
  const destination = path.join(parent, "darwin-arm64");
  for (const directory of [path.join(desktopDirectory, "vendor"), parent]) {
    try { fs.mkdirSync(directory); } catch (error) {
      if (error.code !== "EEXIST") throw error;
    }
    if (!fs.lstatSync(directory).isDirectory()) {
      throw new Error("Media tool cache escapes desktop directory");
    }
  }
  if (fs.existsSync(destination)) {
    if (fs.lstatSync(destination).isSymbolicLink()) throw new Error("Unsafe media tool cache");
    return verifyPair(destination);
  }
  // A failed preparation never installs half of the pair as a valid cache.
  const temporary = fs.mkdtempSync(path.join(parent, ".prepare-"));
  try {
    for (const [name, identity] of Object.entries(TOOLS)) {
      const bytes = await download(name);
      verify(bytes, identity.zipSize, identity.zipHash);
      const zip = path.join(temporary, `${name}.zip`);
      fs.writeFileSync(zip, bytes, { flag: "wx" });
      let executable;
      try {
        executable = execFileSync("/usr/bin/unzip", ["-p", zip, name], {
          timeout: 30000, killSignal: "SIGKILL", maxBuffer: identity.size,
          stdio: ["ignore", "pipe", "pipe"],
        });
      } catch {
        throw new Error(`${name}: ZIP extraction failed`);
      }
      verify(executable, identity.size, identity.hash);
      fs.writeFileSync(path.join(temporary, name), executable, { flag: "wx", mode: 0o755 });
    }
    const reports = verifyPair(temporary);
    fs.renameSync(temporary, destination);
    return reports;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

function checkLicenseMaterials(desktopDirectory) {
  for (const name of ["SOURCE.md", "GPL-3.0.txt", "COMPONENTS.md", "CORRESPONDING-SOURCE.md"]) {
    const file = bundledFile(desktopDirectory, `build/ffmpeg/${name}`, false);
    if (!fs.statSync(file).size) throw new Error(`Empty media license material: ${name}`);
  }
}

exports.default = async function beforePack(context) {
  // electron-builder uses builder-util's Arch enum (arm64 = 3), not process.arch.
  if (context.electronPlatformName !== "darwin" || context.arch !== 3) {
    throw new Error("Bundled media tools support only the macOS arm64 build target");
  }
  const desktopDirectory = context.packager.projectDir;
  checkLicenseMaterials(desktopDirectory);
  const reports = await prepareTools(desktopDirectory);
  for (const [name, report] of Object.entries(reports)) {
    console.log(`[media-tools] ${name}\n${report.version}\n${report.license}`);
  }
};

exports.prepareTools = prepareTools;
exports.checkLicenseMaterials = checkLicenseMaterials;
exports.TOOLS = TOOLS;
exports.download = download;
exports.verify = verify;
