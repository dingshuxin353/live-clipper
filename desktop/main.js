const { app, BrowserWindow, Tray, Menu, clipboard, dialog, ipcMain, session, shell, nativeImage } = require("electron");
const { spawn } = require("child_process");
const https = require("https");
const net = require("net");
const path = require("path");
const { BackendClient, redactText } = require("./backend-client");
const {
  appUrl,
  createBadgePoller,
  createFileSelections,
  createOutputActions,
  createRuntimeState,
  isInternalAppUrl,
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
let exitingNow = false;
const runtime = createRuntimeState();
const backendToken = require("crypto").randomBytes(16).toString("hex");

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
  const env = { ...process.env, LIVE_CLIPPER_WEB_TOKEN: backendToken };
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
      dialog.showErrorBox("Venus", `后台服务意外退出（代码 ${code ?? "未知"}）。请重新打开应用。`);
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
    throw new Error("桌面操作来源无效");
  }
}

ipcMain.handle("lc:select-folder", async (event, title) => {
  assertTrustedRenderer(event);
  const options = {
    title: typeof title === "string" && title ? title : "选择文件夹",
    properties: ["openDirectory", "createDirectory"],
  };
  const result = mainWindow ? await dialog.showOpenDialog(mainWindow, options) : await dialog.showOpenDialog(options);
  return result.canceled ? null : result.filePaths[0];
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
  if (!outputActions) throw new Error("后台服务尚未就绪");
  return outputActions.openOutput(outputId);
});
ipcMain.handle("lc:reveal-output", (event, outputId) => {
  assertTrustedRenderer(event);
  if (!outputActions) throw new Error("后台服务尚未就绪");
  return outputActions.revealOutput(outputId);
});
ipcMain.handle("lc:select-issue-source", (event, issueId) => {
  assertTrustedRenderer(event);
  if (!fileSelections) throw new Error("后台服务尚未就绪");
  return fileSelections.selectIssueSource(issueId);
});
ipcMain.handle("lc:select-recovery-output", (event, issueId) => {
  assertTrustedRenderer(event);
  if (!fileSelections) throw new Error("后台服务尚未就绪");
  return fileSelections.selectRecoveryOutput(issueId);
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

function setupAutoUpdater() {
  ({ autoUpdater: updater } = require("electron-updater"));
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = false;
  updater.on("update-downloaded", async (info) => {
    updateDownloaded = true;
    const { response } = await dialog.showMessageBox({
      type: "info",
      message: `新版本 ${info.version} 已下载完成`,
      detail: "重启 Venus 即可完成更新。",
      buttons: ["立即重启更新", "以后再说"],
      defaultId: 0,
    });
    if (response === 0) installDownloadedUpdate();
  });
  updater.on("error", () => {
    // Silent: update failures must never disturb normal usage.
  });
}

async function installDownloadedUpdate() {
  if (!(await prepareForQuit())) return;
  exitingNow = true;
  updater.quitAndInstall();
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

async function checkForUpdates(interactive) {
  if (app.isPackaged) {
    if (!updater) setupAutoUpdater();
    if (updateDownloaded) {
      installDownloadedUpdate();
      return;
    }
    try {
      const result = await updater.checkForUpdates();
      const latestVersion = result?.updateInfo?.version;
      if (interactive && latestVersion && !isNewerVersion(latestVersion, app.getVersion())) {
        dialog.showMessageBox({ type: "info", message: "已是最新版本", detail: `当前版本 ${app.getVersion()}。` });
      }
    } catch (error) {
      if (interactive) dialog.showErrorBox("检查更新", `暂时无法检查更新：${error.message}`);
    }
    return;
  }
  let latest;
  try {
    latest = await fetchLatestVersion();
  } catch (error) {
    if (interactive) dialog.showErrorBox("检查更新", `暂时无法检查更新：${error.message}`);
    return;
  }
  if (!latest) return;
  if (isNewerVersion(latest, app.getVersion())) {
    const { response } = await dialog.showMessageBox({
      type: "info",
      message: `发现新版本 ${latest}`,
      detail: `当前版本 ${app.getVersion()}。前往下载页获取更新。`,
      buttons: ["去下载", "以后再说"],
      defaultId: 0,
    });
    if (response === 0) shell.openExternal(UPDATE_RELEASES_PAGE);
  } else if (interactive) {
    dialog.showMessageBox({ type: "info", message: "已是最新版本", detail: `当前版本 ${app.getVersion()}。` });
  }
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
    void badgePoller?.activate();
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
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "trayTemplate.png"));
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Venus");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "打开工作室", click: () => showWindow("/studio") },
      { label: "查看项目", click: () => showWindow("/projects") },
      { label: "检查更新", click: () => checkForUpdates(true) },
      { type: "separator" },
      { label: "退出 Venus", click: () => app.quit() },
    ])
  );
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
}

async function prepareForQuit() {
  if (!runtime.beginQuit()) return false;
  badgePoller?.stop();
  badgePoller = null;
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
      backendPort = await findFreePort();
      backendClient = new BackendClient({ port: backendPort, token: backendToken });
      outputActions = createOutputActions({ client: backendClient, shell, runtime });
      fileSelections = createFileSelections({
        client: backendClient,
        dialog,
        runtime,
        getWindow: () => mainWindow,
      });
      startBackend(backendPort);
      await backendClient.waitUntilReady({ isAlive: () => Boolean(backendProcess) });
      await session.defaultSession.cookies.set({
        url: `http://127.0.0.1:${backendPort}`,
        name: "lc_token",
        value: backendToken,
        sameSite: "strict",
        httpOnly: true,
      });
      createApplicationMenu();
      createTray();
      badgePoller = createBadgePoller({ client: backendClient, dock: app.dock, platform: process.platform });
      await badgePoller.start();
      showWindow("/studio");
      setTimeout(() => checkForUpdates(false), 5000);
    } catch (error) {
      dialog.showErrorBox("Venus", `启动失败：${error.message}`);
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
      void badgePoller?.activate();
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
