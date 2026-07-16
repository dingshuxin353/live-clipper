// electron-builder can drop the executable bit on extraResources binaries.
const fs = require("fs");
const path = require("path");

exports.default = async function afterPack(context) {
  const resources = path.join(context.appOutDir, "Venus.app", "Contents", "Resources");
  for (const target of [path.join(resources, "bin", "ffmpeg"), path.join(resources, "backend", "live-clipper-backend")]) {
    if (fs.existsSync(target)) fs.chmodSync(target, 0o755);
  }
};
