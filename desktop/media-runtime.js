const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

function toolError(name, reason) {
  return new Error(`视频处理组件缺失或无法运行，请重新下载安装最新版 Venus。无需删除已有配置和项目。 (${name}: ${reason})`);
}

function bundledFile(resourcesPath, relative, executable = true) {
  const name = path.basename(relative);
  try {
    const root = fs.realpathSync(resourcesPath);
    const file = path.join(root, relative);
    const resolved = fs.realpathSync(file);
    if (!resolved.startsWith(`${root}${path.sep}`) || !fs.lstatSync(file).isFile()) {
      throw toolError(name, "unsafe file");
    }
    if (executable) fs.accessSync(file, fs.constants.X_OK);
    return file;
  } catch (error) {
    if (error.code === "ENOENT") throw toolError(name, "missing");
    if (error.code === "EACCES") throw toolError(name, "not executable");
    throw toolError(name, "unsafe file");
  }
}

function runTool(file, argument) {
  try {
    return execFileSync(file, [argument], {
      encoding: "utf8", timeout: 5000, killSignal: "SIGKILL", maxBuffer: 64 * 1024,
      stdio: ["ignore", "pipe", "pipe"], env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin" },
    });
  } catch {
    throw toolError(path.basename(file), "execution failed");
  }
}

function checkMediaTools(resourcesPath) {
  const versions = {};
  for (const name of ["ffmpeg", "ffprobe"]) {
    const file = bundledFile(resourcesPath, `bin/${name}`);
    const output = runTool(file, "-version");
    if (!new RegExp(`^${name} version \\S+`).test(output)) throw toolError(name, "invalid version");
    versions[name] = output.split("\n", 1)[0];
  }
  return versions;
}

module.exports = { bundledFile, checkMediaTools, runTool };

if (require.main === module) {
  try {
    if (process.argv.length !== 3) throw new Error("Usage: node media-runtime.js <Resources>");
    console.log(JSON.stringify(checkMediaTools(process.argv[2])));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
