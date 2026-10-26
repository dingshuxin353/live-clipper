const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

test("production capability modules load using only the desktop distribution file list", t => {
  const desktop = path.resolve(__dirname, "..");
  const builder = fs.readFileSync(path.join(desktop, "electron-builder.yml"), "utf8");
  const files = builder.split("files:\n")[1].split("extraResources:")[0].trim().split("\n").map(line => line.trim().replace(/^- /, ""));
  assert.ok(files.includes("desktop-capabilities.js"));
  assert.ok(files.includes("update-flow.js"));
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), "venus-desktop-stage-"));
  t.after(() => fs.rmSync(stage, { recursive: true }));
  for (const name of files.filter(name => !name.includes("*"))) fs.copyFileSync(path.join(desktop, name), path.join(stage, name));
  assert.equal(typeof require(path.join(stage, "desktop-capabilities")).registerDesktopCapabilities, "function");
  assert.equal(typeof require(path.join(stage, "update-flow")).createUpdateFlow({}).checkForUpdates, "function");
});
