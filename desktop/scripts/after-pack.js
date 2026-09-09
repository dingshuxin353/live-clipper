// electron-builder can drop the executable bit on extraResources binaries.
const fs = require("fs");
const path = require("path");
const { bundledFile, checkMediaTools } = require("../media-runtime");

exports.default = async function afterPack(context) {
  const resources = path.join(context.appOutDir, "Venus.app", "Contents", "Resources");
  for (const relative of ["bin/ffmpeg", "bin/ffprobe", "backend/live-clipper-backend"]) {
    fs.chmodSync(bundledFile(resources, relative, false), 0o755);
  }
  checkMediaTools(resources);
};
