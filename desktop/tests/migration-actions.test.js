const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { readFileSync } = fs;
const test = require("node:test");

const { createMigrationActions, createRuntimeState } = require("../runtime-state");

function fixture() {
  const appHome = fs.mkdtempSync(path.join(os.tmpdir(), "venus-q-grant-"));
  const migrationId = "migration-1";
  const backupRoot = path.join(appHome, "work", "migration-backups");
  const backupPath = path.join(backupRoot, migrationId);
  fs.mkdirSync(backupPath, { recursive: true });
  return { appHome, backupPath, backupRoot, migrationId };
}

function grant(migrationId, backupPath) {
  return {
    ok: true,
    grant: {
      grant_version: 1,
      kind: "migration_backup_reveal",
      migration_id: migrationId,
      backup_path: backupPath,
    },
  };
}

test("migration backup grant is validated in main and concurrent calls reveal once", async (t) => {
  const { appHome, backupPath, migrationId } = fixture();
  t.after(() => fs.rmSync(appHome, { recursive: true, force: true }));
  const requested = [];
  const revealed = [];
  let release;
  const response = new Promise((resolve) => { release = resolve; });
  const actions = createMigrationActions({
    client: { getMigrationBackupGrant: async (id) => { requested.push(id); return response; } },
    shell: { showItemInFolder: (target) => revealed.push(target) },
    runtime: createRuntimeState(),
    appHome,
  });

  const first = actions.showBackup(migrationId);
  const replay = actions.showBackup(migrationId);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requested, [migrationId]);
  release(grant(migrationId, backupPath));
  assert.deepEqual(await first, { ok: true });
  assert.deepEqual(await replay, { ok: true });
  assert.deepEqual(revealed, [fs.realpathSync(backupPath)]);
});

test("migration backup action rejects unsafe ids, invalid grants, backend failures, and quitting state", async (t) => {
  const { appHome, backupPath } = fixture();
  t.after(() => fs.rmSync(appHome, { recursive: true, force: true }));
  const runtime = createRuntimeState();
  const actions = createMigrationActions({
    client: { getMigrationBackupGrant: async () => grant("other", backupPath) },
    shell: { showItemInFolder: () => assert.fail("invalid grant must not reach shell") },
    runtime,
    appHome,
  });

  for (const invalid of ["", "../migration", "migration/child", " migration-1"]) {
    assert.throws(() => actions.showBackup(invalid), /升级记录无效/);
  }
  await assert.rejects(actions.showBackup("migration-1"), /无法确认备份位置/);

  const failed = createMigrationActions({
    client: { getMigrationBackupGrant: async () => { throw new Error("secret backend detail"); } },
    shell: { showItemInFolder: () => assert.fail("failed grant must not reach shell") },
    runtime,
    appHome,
  });
  await assert.rejects(failed.showBackup("migration-2"), (error) => {
    assert.match(error.message, /Finder/);
    assert.equal(error.message.includes("secret"), false);
    assert.equal(error.message.includes(appHome), false);
    return true;
  });

  runtime.beginQuit();
  assert.throws(() => actions.showBackup("migration-3"), /正在退出/);
});

test("migration backup grant rejects outside paths and target or root symlinks without calling shell", async (t) => {
  const { appHome, backupPath, backupRoot, migrationId } = fixture();
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "venus-q-outside-"));
  t.after(() => fs.rmSync(appHome, { recursive: true, force: true }));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  let shellCalls = 0;
  const makeActions = (payload) => createMigrationActions({
    client: { getMigrationBackupGrant: async () => payload },
    shell: { showItemInFolder: () => { shellCalls += 1; } },
    runtime: createRuntimeState(),
    appHome,
  });

  await assert.rejects(makeActions(grant(migrationId, outside)).showBackup(migrationId), /无法确认备份位置/);
  fs.rmSync(backupPath, { recursive: true });
  fs.symlinkSync(outside, backupPath, "dir");
  await assert.rejects(makeActions(grant(migrationId, backupPath)).showBackup(migrationId), /无法确认备份位置/);
  fs.rmSync(backupRoot, { recursive: true });
  fs.mkdirSync(path.join(outside, migrationId));
  fs.symlinkSync(outside, backupRoot, "dir");
  await assert.rejects(makeActions(grant(migrationId, backupPath)).showBackup(migrationId), /无法确认备份位置/);
  assert.equal(shellCalls, 0);
});

test("migration backup shell failures stay path-free", async (t) => {
  const { appHome, backupPath, migrationId } = fixture();
  t.after(() => fs.rmSync(appHome, { recursive: true, force: true }));
  const actions = createMigrationActions({
    client: { getMigrationBackupGrant: async () => grant(migrationId, backupPath) },
    shell: { showItemInFolder: () => { throw new Error(`failed ${backupPath}`); } },
    runtime: createRuntimeState(),
    appHome,
  });
  await assert.rejects(actions.showBackup(migrationId), (error) => {
    assert.match(error.message, /Finder/);
    assert.equal(error.message.includes(appHome), false);
    return true;
  });
});

test("migration backup grant detects target replacement during validation", async (t) => {
  const { appHome, backupPath, migrationId } = fixture();
  t.after(() => fs.rmSync(appHome, { recursive: true, force: true }));
  let targetReads = 0;
  const guardedFs = {
    lstatSync(target) {
      const value = fs.lstatSync(target);
      if (target === backupPath) {
        targetReads += 1;
        if (targetReads === 1) {
          fs.renameSync(backupPath, `${backupPath}-old`);
          fs.mkdirSync(backupPath);
        }
      }
      return value;
    },
    realpathSync: fs.realpathSync,
  };
  const actions = createMigrationActions({
    client: { getMigrationBackupGrant: async () => grant(migrationId, backupPath) },
    shell: { showItemInFolder: () => assert.fail("replaced target must not reach shell") },
    runtime: createRuntimeState(),
    appHome,
    fs: guardedFs,
  });
  await assert.rejects(actions.showBackup(migrationId), /无法确认备份位置/);
});

test("preload and main expose only the migration-id backup capability", () => {
  const preload = readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
  const main = readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(preload, /showBackup: \(migrationId\) => ipcRenderer\.invoke\("lc:show-migration-backup", migrationId\)/);
  assert.match(main, /migrationActions\.showBackup\(migrationId\)/);
  assert.equal(preload.includes("backupPath"), false);
  assert.equal(preload.includes("backendToken"), false);
  assert.equal(preload.includes("showMigrationBackup"), false);
});

test("preload exposes a parameter-free quit capability through the trusted main process", () => {
  const preload = readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
  const main = readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(preload, /quitApp: \(\) => ipcRenderer\.invoke\("lc:quit-app"\)/);
  assert.match(main, /ipcMain\.handle\("lc:quit-app", \(event\) => \{\s*assertTrustedRenderer\(event\);\s*app\.quit\(\)/);
  assert.equal(preload.includes("quitCommand"), false);
});
