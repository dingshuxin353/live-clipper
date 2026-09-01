const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const { createMigrationActions, createRuntimeState } = require("../runtime-state");

test("migration backup action sends only the migration id and reuses concurrent calls", async () => {
  const seen = [];
  let release;
  const response = new Promise((resolve) => { release = resolve; });
  const actions = createMigrationActions({
    client: {
      showMigrationBackup: async (migrationId) => {
        seen.push(migrationId);
        return response;
      },
    },
    runtime: createRuntimeState(),
  });

  const first = actions.showBackup("migration-1");
  const replay = actions.showBackup("migration-1");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(seen, ["migration-1"]);
  release({ ok: true, action: "reveal_backup", migration_id: "migration-1" });
  assert.deepEqual(await first, { ok: true });
  assert.deepEqual(await replay, { ok: true });
});

test("migration backup action rejects unsafe ids, mismatched replies, failures, and quitting state", async () => {
  const runtime = createRuntimeState();
  const actions = createMigrationActions({
    client: { showMigrationBackup: async () => ({ ok: true, action: "reveal_backup", migration_id: "other" }) },
    runtime,
  });

  for (const invalid of ["", "../migration", "migration/child", " migration-1"]) {
    assert.throws(() => actions.showBackup(invalid), /migration_id/);
  }
  await assert.rejects(actions.showBackup("migration-1"), /结果无效/);

  const failed = createMigrationActions({
    client: { showMigrationBackup: async () => { throw new Error("secret backend detail"); } },
    runtime,
  });
  await assert.rejects(failed.showBackup("migration-2"), (error) => {
    assert.match(error.message, /Finder/);
    assert.equal(error.message.includes("secret"), false);
    return true;
  });

  runtime.beginQuit();
  assert.throws(() => actions.showBackup("migration-3"), /正在退出/);
});

test("preload and main expose only the migration-id backup capability", () => {
  const preload = readFileSync(join(__dirname, "..", "preload.js"), "utf8");
  const main = readFileSync(join(__dirname, "..", "main.js"), "utf8");
  assert.match(preload, /showBackup: \(migrationId\) => ipcRenderer\.invoke\("lc:show-migration-backup", migrationId\)/);
  assert.match(main, /migrationActions\.showBackup\(migrationId\)/);
  assert.equal(preload.includes("backupPath"), false);
  assert.equal(preload.includes("backendToken"), false);
});
