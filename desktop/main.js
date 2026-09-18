const { app, BrowserWindow, Tray, Menu, clipboard, dialog, ipcMain, session, shell, nativeImage } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const https = require("https");
const net = require("net");
const path = require("path");
const { BackendClient, BackendError, redactText } = require("./backend-client");
const { checkMediaTools } = require("./media-runtime");
const {
  appUrl,
  createBadgePoller,
  createDataDirectoryActions,
  createFileSelections,
  createFolderSelection,
  createMigrationActions,
  createOutputActions,
  createRuntimeState,
  electronRuntimeHome,
  isInternalAppUrl,
  resolveTrustedAppHome,
  writeClipboardText,
} = require("./runtime-state");

let mainWindow = null;
let tray = null;
let backendProcess = null;
let backendPort = null;
let backendClient = null;
let badgePoller = null;
let outputActions = null;
let fileSelections = null;
let folderSelection = null;
let migrationActions = null;
let dataDirectoryActions = null;
let appHome = null;
let startupRefresh = null;
let updateCheckTimer = null;
let exitingNow = false;
const runtime = createRuntimeState();
const backendToken = require("crypto").randomBytes(16).toString("hex");

const configuredHome = String(process.env.LIVE_CLIPPER_HOME || "").trim();
const homeCandidate = configuredHome || path.join(app.getPath("appData"), "Venus");
appHome = resolveTrustedAppHome(homeCandidate);
const electronHome = electronRuntimeHome(appHome);
fs.mkdirSync(electronHome, { recursive: true, mode: 0o700 });
app.setPath("userData", electronHome);

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function backendCommand(port) {
  if (app.isPackaged) {
    return {
      executable: path.join(process.resourcesPath, "backend", "live-clipper-backend"),
      args: ["app", "--port", String(port)],
    };
  }
  const repoRoot = path.resolve(__dirname, "..");
  return {
    executable: path.join(repoRoot, ".venv", "bin", "live-clipper"),
    args: ["app", "--port", String(port)],
  };
}

function startBackend(port) {
  if (!runtime.canStart()) return false;
  const { executable, args } = backendCommand(port);
  const env = {
    ...process.env,
    LIVE_CLIPPER_HOME: appHome,
    LIVE_CLIPPER_WEB_TOKEN: backendToken,
  };
  if (app.isPackaged) {
    env.PATH = `${path.join(process.resourcesPath, "bin")}:${env.PATH || ""}`;
  }
  backendProcess = spawn(executable, args, { env, stdio: ["ignore", "pipe", "pipe"] });
  backendProcess.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${redactText(chunk, [backendToken])}`));
  backendProcess.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${redactText(chunk, [backendToken])}`));
  backendProcess.on("error", () => {
    backendProcess = null;
  });
  backendProcess.on("exit", (code) => {
    backendProcess = null;
    if (!runtime.isQuitting()) {
      dialog.showErrorBox("Venus", `Venus 后台服务已停止，请重新打开 Venus。退出代码：${code ?? "未知"}。`);
      runtime.beginQuit();
      badgePoller?.stop();
      exitingNow = true;
      app.exit(1);
    }
  });
  return true;
}

function assertTrustedRenderer(event) {
  if (
    !mainWindow
    || event.sender !== mainWindow.webContents
    || !isInternalAppUrl(event.senderFrame?.url, backendPort)
  ) {
    throw new Error("无法执行此操作，请重新打开 Venus 后重试。");
  }
}

ipcMain.handle("lc:application-info", (event) => {
  assertTrustedRenderer(event);
  if (!runtime.canStart()) throw new Error("Venus 正在退出");
  return { version: app.getVersion(), app_home: appHome, platform: process.platform, arch: process.arch };
});
ipcMain.handle("lc:open-data-directory", (event, id) => {
  assertTrustedRenderer(event);
  if (!dataDirectoryActions) throw new Error("后台服务暂时不可用，请稍后重试。");
  return dataDirectoryActions.open(id);
});
ipcMain.handle("lc:check-for-updates", (event) => {
  assertTrustedRenderer(event);
  return checkForUpdates(true);
});

ipcMain.handle("lc:select-folder", async (event, title) => {
  assertTrustedRenderer(event);
  if (!folderSelection) throw new Error("暂时无法打开文件夹选择窗口，请稍后重试。");
  return folderSelection.select(title);
});

ipcMain.handle("lc:read-clipboard-text", (event) => {
  assertTrustedRenderer(event);
  return clipboard.readText();
});
ipcMain.handle("lc:write-clipboard-text", (event, value) => {
  assertTrustedRenderer(event);
  return writeClipboardText(clipboard, runtime, value);
});
ipcMain.handle("lc:open-output", (event, outputId) => {
  assertTrustedRenderer(event);
  if (!outputActions) throw new Error("后台服务暂时不可用，请稍后重试。");
  return outputActions.openOutput(outputId);
});
ipcMain.handle("lc:reveal-output", (event, outputId) => {
  assertTrustedRenderer(event);
  if (!outputActions) throw new Error("后台服务暂时不可用，请稍后重试。");
  return outputActions.revealOutput(outputId);
});
ipcMain.handle("lc:select-issue-source", (event, issueId) => {
  assertTrustedRenderer(event);
  if (!fileSelections) throw new Error("后台服务暂时不可用，请稍后重试。");
  return fileSelections.selectIssueSource(issueId);
});
ipcMain.handle("lc:select-recovery-output", (event, issueId) => {
  assertTrustedRenderer(event);
  if (!fileSelections) throw new Error("后台服务暂时不可用，请稍后重试。");
  return fileSelections.selectRecoveryOutput(issueId);
});
ipcMain.handle("lc:show-migration-backup", (event, migrationId) => {
  assertTrustedRenderer(event);
  if (!migrationActions) throw new Error("后台服务暂时不可用，请稍后重试。");
  return migrationActions.showBackup(migrationId).catch(error => ({ ok: false, message: error.message, code: error.code || "backup_display_failed" }));
});
ipcMain.handle("lc:quit-app", (event) => {
  assertTrustedRenderer(event);
  app.quit();
  return { ok: true };
});

function createApplicationMenu() {
  const template = [];
  if (process.platform === "darwin") {
    template.push({
      label: app.name,
      submenu: [
        { role: "about" },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit" },
      ],
    });
  }
  template.push({
    label: "编辑",
    submenu: [
      { role: "undo", label: "撤销" },
      { role: "redo", label: "重做" },
      { type: "separator" },
      { role: "cut", label: "剪切" },
      { role: "copy", label: "复制" },
      { role: "paste", label: "粘贴" },
      { role: "pasteAndMatchStyle", label: "粘贴并匹配样式" },
      { role: "delete", label: "删除" },
      { role: "selectAll", label: "全选" },
    ],
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

let updater = null;
let updateDownloaded = false;
let updateCheckPromise = null;
let interactiveUpdatePromise = null;
let updateConfirmation = null;
let updatePhase = "idle";
let updateFailure = null;

function updateError(code) {
  const messages = {
    check: "暂时无法检查更新，请稍后重试。",
    download: "更新下载失败。请检查网络连接，再次检查更新以重试下载。",
    page: "无法打开下载页，请稍后重试。",
    prepare: "暂时无法准备重启，更新尚未安装。请稍后重新打开 Venus 后检查更新。",
    install: "暂时无法安装更新。后台处理已停止，请重新打开 Venus 后检查更新。",
    restricted: "当前无法安装更新，请先完成首次设置或数据升级。",
  };
  return { ok: false, code, message: messages[code] };
}
function reportUpdateError(result) {
  dialog.showErrorBox(result.code === "check" ? "检查更新" : "更新 Venus", result.message);
  return result;
}

function setupAutoUpdater() {
  ({ autoUpdater: updater } = require("electron-updater"));
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = false;
  updater.on("update-available", () => { updatePhase = "download"; updateFailure = null; });
  updater.on("update-downloaded", () => {
    updatePhase = "idle"; updateFailure = null; updateDownloaded = true;
    if (runtime.canUseProjectFeatures()) void confirmDownloadedUpdate().catch(() => reportUpdateError(updateError("install")));
  });
  updater.on("error", () => {
    if (["download", "install"].includes(updatePhase)) {
      updateFailure = updateError(updatePhase);
      updatePhase = "idle";
      reportUpdateError(updateFailure);
    }
  });
}

function confirmDownloadedUpdate() {
  if (updateConfirmation) return updateConfirmation;
  updateConfirmation = (async () => {
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: "info", message: "更新已下载", detail: "重启 Venus 以安装更新。重启会中断正在处理的任务，请在处理完成后更新。",
      buttons: ["重启并安装", "稍后"], defaultId: 1, cancelId: 1,
    });
    return response === 0 ? installDownloadedUpdate() : { ok: true, deferred: true };
  })().finally(() => { updateConfirmation = null; });
  return updateConfirmation;
}

async function installDownloadedUpdate() {
  if (runtime.isRestricted()) return reportUpdateError(updateError("restricted"));
  try {
    if (!(await prepareForQuit())) return reportUpdateError(updateError("prepare"));
  } catch { return reportUpdateError(updateError("prepare")); }
  try {
    exitingNow = true;
    updatePhase = "install";
    updater.quitAndInstall();
    if (updateFailure) return updateFailure;
    return { ok: true };
  } catch { return reportUpdateError(updateError("install")); }
}

const UPDATE_RELEASES_API = "https://api.github.com/repos/dingshuxin353/live-clipper/releases/latest";
const UPDATE_RELEASES_PAGE = "https://github.com/dingshuxin353/live-clipper/releases/latest";

function fetchLatestVersion() {
  return new Promise((resolve, reject) => {
    const request = https.get(
      UPDATE_RELEASES_API,
      {
        headers: { "User-Agent": "live-clipper-desktop", Accept: "application/vnd.github+json" },
        timeout: 8000,
      },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          if (response.statusCode !== 200) {
            reject(new Error(`HTTP ${response.statusCode}`));
            return;
          }
          try {
            resolve(String(JSON.parse(body).tag_name || "").replace(/^v/, ""));
          } catch (error) {
            reject(error);
          }
        });
      }
    );
    request.on("error", reject);
    request.on("timeout", () => {
      request.destroy();
      reject(new Error("timeout"));
    });
  });
}

function isNewerVersion(latest, current) {
  const parse = (value) => String(value).split(".").map((part) => parseInt(part, 10) || 0);
  const a = parse(latest);
  const b = parse(current);
  for (let i = 0; i < 3; i += 1) {
    if ((a[i] || 0) > (b[i] || 0)) return true;
    if ((a[i] || 0) < (b[i] || 0)) return false;
  }
  return false;
}

function checkForUpdates(interactive) {
  if (!runtime.canUseProjectFeatures()) return Promise.resolve(updateError("restricted"));
  if (interactive && interactiveUpdatePromise) return interactiveUpdatePromise;
  if (!updateCheckPromise) updateCheckPromise = readUpdateInfo().finally(() => { updateCheckPromise = null; });
  if (!interactive) return updateCheckPromise;
  interactiveUpdatePromise = updateCheckPromise.then(showUpdateResult).finally(() => { interactiveUpdatePromise = null; });
  return interactiveUpdatePromise;
}

async function readUpdateInfo() {
  try {
    if (app.isPackaged && !updater) setupAutoUpdater();
    if (updateDownloaded) return { ok: true, downloaded: true };
    updateFailure = null;
    const latest = app.isPackaged ? (await updater.checkForUpdates())?.updateInfo?.version : await fetchLatestVersion();
    if (typeof latest !== "string" || !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(latest)) throw new Error("invalid version");
    return updateFailure || { ok: true, latest };
  } catch {
    return updateFailure || updateError("check");
  }
}

async function showUpdateResult(result) {
  if (!runtime.canUseProjectFeatures()) return updateError("restricted");
  if (!result.ok) return result.code === "download" ? result : reportUpdateError(result);
  let operation = "check";
  try {
    if (result.downloaded || updateDownloaded) {
      operation = "install";
      return await confirmDownloadedUpdate();
    } else if (isNewerVersion(result.latest, app.getVersion())) {
      if (!app.isPackaged) {
        const { response } = await dialog.showMessageBox(mainWindow, {
          type: "info", message: `发现新版本 ${result.latest}`,
          detail: `当前版本：${app.getVersion()}。请打开下载页获取新版本。`,
          buttons: ["打开下载页", "稍后"], defaultId: 1, cancelId: 1,
        });
        if (response === 0) { operation = "page"; await shell.openExternal(UPDATE_RELEASES_PAGE); }
      } else {
        await dialog.showMessageBox(mainWindow, { type: "info", message: `发现新版本 ${result.latest}`, detail: "Venus 将自动下载更新，下载完成后会提示你重启安装。" });
        if (updateFailure) return updateFailure;
      }
    } else {
      await dialog.showMessageBox(mainWindow, { type: "info", message: "未发现新版本", detail: `当前版本：${app.getVersion()}。` });
    }
    return { ok: true };
  } catch { return reportUpdateError(updateError(operation)); }
}

function showWindow(route = null) {
  if (!runtime.canStart() || !backendPort) return;
  if (mainWindow) {
    if (route) {
      const target = appUrl(backendPort, route);
      if (mainWindow.webContents.getURL() !== target) void mainWindow.loadURL(target);
    }
    mainWindow.show();
    mainWindow.focus();
    if (runtime.canUseProjectFeatures()) void badgePoller?.activate();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 980,
    minHeight: 640,
    title: "Venus",
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  mainWindow.loadURL(appUrl(backendPort, route || "/studio"));
  mainWindow.webContents.on("did-navigate", () => {
    void refreshStartupState();
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternalAppUrl(url, backendPort)) {
      void mainWindow.loadURL(url);
    } else {
      try {
        const target = new URL(url);
        if (["http:", "https:"].includes(target.protocol)) void shell.openExternal(url);
      } catch (_error) {
        // Ignore invalid and non-web external targets.
      }
    }
    return { action: "deny" };
  });
  mainWindow.on("close", (event) => {
    if (!runtime.isQuitting()) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function createTray() {
  if (!tray) {
    const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "trayTemplate.png"));
    icon.setTemplateImage(true);
    tray = new Tray(icon);
    tray.setToolTip("Venus");
  }
  const items = runtime.isRestricted()
    ? [
      { label: "打开 Venus", click: () => showWindow("/studio") },
      { type: "separator" },
      { label: "退出 Venus", click: () => app.quit() },
    ]
    : [
      { label: "打开工作室", click: () => showWindow("/studio") },
      { label: "查看项目", click: () => showWindow("/projects") },
      { label: "检查更新", click: () => checkForUpdates(true) },
      { type: "separator" },
      { label: "退出 Venus", click: () => app.quit() },
    ];
  tray.setContextMenu(Menu.buildFromTemplate(items));
}

async function refreshDesktopCapabilities() {
  createTray();
  if (!runtime.canUseProjectFeatures()) {
    badgePoller?.stop();
    badgePoller = null;
    if (app.dock && typeof app.dock.setBadge === "function") app.dock.setBadge("");
    if (updateCheckTimer !== null) clearTimeout(updateCheckTimer);
    updateCheckTimer = null;
    return false;
  }
  if (!badgePoller) {
    badgePoller = createBadgePoller({
      client: backendClient,
      dock: app.dock,
      platform: process.platform,
      isAllowed: () => runtime.canUseProjectFeatures(),
    });
  }
  await badgePoller.start();
  if (updateCheckTimer === null) {
    updateCheckTimer = setTimeout(() => {
      updateCheckTimer = null;
      void checkForUpdates(false);
    }, 5000);
  }
  return true;
}

function refreshStartupState() {
  if (!backendClient || !runtime.canStart()) return Promise.resolve(false);
  if (startupRefresh) return startupRefresh;
  startupRefresh = backendClient.checkReady()
    .then(async (snapshot) => {
      runtime.setStartup(snapshot);
      await refreshDesktopCapabilities();
      return true;
    })
    .catch(() => false)
    .finally(() => {
      startupRefresh = null;
    });
  return startupRefresh;
}

async function shutdownBackend() {
  try {
    await backendClient?.stopService(3000);
  } catch (_error) {
    // Service may not be running; proceed to terminate the web backend.
  }
  if (!backendProcess) {
    backendClient = null;
    outputActions = null;
    fileSelections = null;
    folderSelection = null;
    migrationActions = null;
    dataDirectoryActions = null;
    return;
  }
  const proc = backendProcess;
  proc.kill("SIGTERM");
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      console.warn("[desktop] 后台服务未在 3 秒内退出，发送 SIGKILL");
      try {
        proc.kill("SIGKILL");
      } catch (_error) {
        // Already exited.
      }
      resolve();
    }, 3000);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
  backendClient = null;
  outputActions = null;
  fileSelections = null;
  folderSelection = null;
  migrationActions = null;
  dataDirectoryActions = null;
}

async function prepareForQuit() {
  if (!runtime.beginQuit()) return false;
  badgePoller?.stop();
  badgePoller = null;
  if (updateCheckTimer !== null) clearTimeout(updateCheckTimer);
  updateCheckTimer = null;
  tray?.destroy();
  tray = null;
  await shutdownBackend();
  return true;
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (runtime.canStart() && backendPort) showWindow();
  });

  app.whenReady().then(async () => {
    try {
      if (app.isPackaged) checkMediaTools(process.resourcesPath);
      backendPort = await findFreePort();
      backendClient = new BackendClient({ port: backendPort, token: backendToken });
      dataDirectoryActions = createDataDirectoryActions({ client: backendClient, shell, runtime, appHome });
      outputActions = createOutputActions({ client: backendClient, shell, runtime });
      fileSelections = createFileSelections({
        client: backendClient,
        dialog,
        runtime,
        getWindow: () => mainWindow,
      });
      folderSelection = createFolderSelection({ dialog, runtime, getWindow: () => mainWindow });
      migrationActions = createMigrationActions({ client: backendClient, shell, runtime, appHome });
      startBackend(backendPort);
      const startup = await backendClient.waitUntilReady({ isAlive: () => Boolean(backendProcess) });
      if (!startup?.entry?.mode) throw new Error("无法读取启动状态，请重新打开 Venus。");
      runtime.setStartup(startup);
      await session.defaultSession.cookies.set({
        url: `http://127.0.0.1:${backendPort}`,
        name: "lc_token",
        value: backendToken,
        sameSite: "strict",
        httpOnly: true,
      });
      createApplicationMenu();
      await refreshDesktopCapabilities();
      showWindow("/studio");
    } catch (error) {
      dialog.showErrorBox("Venus", `Venus 启动失败，请重新打开后重试。如仍失败，请联系开发者排查。${error instanceof BackendError || error.code === "media_tools_unavailable" ? `\n${error.message}` : ""}`);
      runtime.beginQuit();
      badgePoller?.stop();
      await shutdownBackend();
      exitingNow = true;
      app.exit(1);
    }
  });

  app.on("window-all-closed", () => {
    // Keep running in the tray.
  });

  app.on("activate", () => {
    if (backendPort && runtime.canStart()) {
      showWindow();
      void refreshStartupState();
    }
  });

  app.on("before-quit", (event) => {
    if (exitingNow) return;
    event.preventDefault();
    if (runtime.isQuitting()) return;
    prepareForQuit().finally(() => {
      exitingNow = true;
      app.exit(0);
    });
  });
}
