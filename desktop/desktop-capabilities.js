const {
  createDataDirectoryActions,
  createFileSelections,
  createFolderSelection,
  createMigrationActions,
  createOutputActions,
  isInternalAppUrl,
  writeClipboardText,
} = require("./runtime-state");

function registerDesktopCapabilities({ ipcMain, app, clipboard, dialog, shell, client, runtime, appHome, getWindow, getPort, checkForUpdates }) {
  const dataDirectories = createDataDirectoryActions({ client, shell, runtime, appHome });
  const outputs = createOutputActions({ client, shell, runtime });
  const files = createFileSelections({ client, dialog, runtime, getWindow });
  const folders = createFolderSelection({ dialog, runtime, getWindow });
  const migrations = createMigrationActions({ client, shell, runtime, appHome });
  const trusted = operation => (event, ...args) => {
    const window = getWindow();
    if (!window || event.sender !== window.webContents || !isInternalAppUrl(event.senderFrame?.url, getPort())) {
      throw new Error("无法执行此操作，请重新打开 Venus 后重试。");
    }
    return operation(...args);
  };
  const selectFolder = trusted(title => folders.select(title));
  const handlers = {
    "lc:application-info": trusted(() => {
      if (!runtime.canStart()) throw new Error("Venus 正在退出");
      return { version: app.getVersion(), app_home: appHome, platform: process.platform, arch: process.arch };
    }),
    "lc:open-data-directory": trusted(id => dataDirectories.open(id)),
    "lc:check-for-updates": trusted(() => checkForUpdates(true)),
    // Folder selection has always rejected asynchronously, including source checks.
    "lc:select-folder": async (...args) => selectFolder(...args),
    "lc:read-clipboard-text": trusted(() => clipboard.readText()),
    "lc:write-clipboard-text": trusted(value => writeClipboardText(clipboard, runtime, value)),
    "lc:open-output": trusted(id => outputs.openOutput(id)),
    "lc:reveal-output": trusted(id => outputs.revealOutput(id)),
    "lc:select-issue-source": trusted(id => files.selectIssueSource(id)),
    "lc:select-recovery-output": trusted(id => files.selectRecoveryOutput(id)),
    "lc:show-migration-backup": trusted(id => migrations.showBackup(id).catch(error => ({ ok: false, message: error.message, code: error.code || "backup_display_failed" }))),
    "lc:quit-app": trusted(() => { app.quit(); return { ok: true }; }),
  };
  for (const [channel, handler] of Object.entries(handlers)) ipcMain.handle(channel, handler);
  return () => { for (const channel of Object.keys(handlers)) ipcMain.removeHandler(channel); };
}

module.exports = { registerDesktopCapabilities };
