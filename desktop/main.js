const { app, BrowserWindow, Tray, Menu, clipboard, dialog, ipcMain, session, shell, nativeImage } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");
const { BackendClient, BackendError, redactText } = require("./backend-client");
const { checkMediaTools } = require("./media-runtime");
const { registerDesktopCapabilities } = require("./desktop-capabilities");
const { createUpdateFlow } = require("./update-flow");
const {
  appUrl,
  createBadgePoller,
  createRuntimeState,
  electronRuntimeHome,
  isInternalAppUrl,
  resolveTrustedAppHome,
} = require("./runtime-state");

let mainWindow = null;
let tray = null;
let backendProcess = null;
let backendPort = null;
let backendClient = null;
let badgePoller = null;
let appHome = null;
let startupRefresh = null;
let updateCheckTimer = null;
let exitingNow = false;
const runtime = createRuntimeState();
const updates = createUpdateFlow({
  app, dialog, shell, runtime, getWindow: () => mainWindow,
  prepareForQuit, markExiting: () => { exitingNow = true; },
});
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
      { label: "检查更新", click: () => updates.checkForUpdates(true) },
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
      void updates.checkForUpdates(false);
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
      registerDesktopCapabilities({
        ipcMain, app, clipboard, dialog, shell, client: backendClient, runtime, appHome,
        getWindow: () => mainWindow, getPort: () => backendPort, checkForUpdates: updates.checkForUpdates,
      });
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
