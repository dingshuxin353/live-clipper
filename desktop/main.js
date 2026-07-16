const { app, BrowserWindow, Tray, Menu, dialog, ipcMain, session, shell, nativeImage } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const https = require("https");
const net = require("net");
const path = require("path");

// 16x16 template tray icon (film frame + play triangle), generated offline.
const TRAY_ICON_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAL0lEQVR42mNgGEzgP4kYQzM5FtLWgP/UMOA/NQz4T4wLaWrAwAUifdMBxUl54AAAKlY5x3hhuhcAAAAASUVORK5CYII=";

let mainWindow = null;
let tray = null;
let backendProcess = null;
let backendPort = null;
let quitting = false;
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
  const { executable, args } = backendCommand(port);
  const env = { ...process.env, LIVE_CLIPPER_WEB_TOKEN: backendToken };
  if (app.isPackaged) {
    env.PATH = `${path.join(process.resourcesPath, "bin")}:${env.PATH || ""}`;
  }
  backendProcess = spawn(executable, args, { env, stdio: ["ignore", "pipe", "pipe"] });
  backendProcess.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`));
  backendProcess.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`));
  backendProcess.on("exit", (code) => {
    backendProcess = null;
    if (!quitting) {
      dialog.showErrorBox("Live Clipper", `后台服务意外退出（代码 ${code ?? "未知"}）。请重新打开应用。`);
      app.quit();
    }
  });
}

function waitForBackend(port, timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const retry = () => {
      if (Date.now() - started > timeoutMs) {
        reject(new Error("后台服务启动超时"));
        return;
      }
      setTimeout(attempt, 250);
    };
    const attempt = () => {
      const request = http.get(
        {
          host: "127.0.0.1",
          port,
          path: "/api/onboarding",
          timeout: 1000,
          headers: { Authorization: `Bearer ${backendToken}` },
        },
        (response) => {
          response.resume();
          if (response.statusCode === 200) resolve();
          else retry();
        }
      );
      request.on("error", retry);
      request.on("timeout", () => {
        request.destroy();
        retry();
      });
    };
    attempt();
  });
}

function postBackend(apiPath, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: "127.0.0.1",
        port: backendPort,
        path: apiPath,
        method: "POST",
        timeout: timeoutMs,
        headers: { Authorization: `Bearer ${backendToken}` },
      },
      (response) => {
        response.resume();
        response.on("end", resolve);
      }
    );
    request.on("error", reject);
    request.on("timeout", () => {
      request.destroy();
      reject(new Error("timeout"));
    });
    request.end("{}");
  });
}

ipcMain.handle("lc:select-folder", async (_event, title) => {
  const options = {
    title: typeof title === "string" && title ? title : "选择文件夹",
    properties: ["openDirectory", "createDirectory"],
  };
  const result = mainWindow ? await dialog.showOpenDialog(mainWindow, options) : await dialog.showOpenDialog(options);
  return result.canceled ? null : result.filePaths[0];
});

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

function showWindow() {
  if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 980,
    minHeight: 640,
    title: "Live Clipper",
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${backendPort}`);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function createTray() {
  const icon = nativeImage.createFromDataURL(TRAY_ICON_DATA_URL);
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Live Clipper");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "打开主界面", click: () => showWindow() },
      { label: "立即扫描录播", click: () => postBackend("/api/service/scan-now").catch(() => {}) },
      { label: "检查更新", click: () => checkForUpdates(true) },
      { type: "separator" },
      { label: "退出 Live Clipper", click: () => app.quit() },
    ])
  );
}

async function shutdownBackend() {
  try {
    await postBackend("/api/service/stop", 3000);
  } catch (_error) {
    // Service may not be running; proceed to terminate the web backend.
  }
  if (!backendProcess) return;
  const proc = backendProcess;
  proc.kill("SIGTERM");
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch (_error) {
        // Already exited.
      }
      resolve();
    }, 4000);
    proc.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(async () => {
    try {
      backendPort = await findFreePort();
      startBackend(backendPort);
      await waitForBackend(backendPort);
      await session.defaultSession.cookies.set({
        url: `http://127.0.0.1:${backendPort}`,
        name: "lc_token",
        value: backendToken,
        sameSite: "strict",
      });
      createTray();
      showWindow();
      setTimeout(() => checkForUpdates(false), 5000);
    } catch (error) {
      dialog.showErrorBox("Live Clipper", `启动失败：${error.message}`);
      quitting = true;
      await shutdownBackend();
      app.exit(1);
    }
  });

  app.on("window-all-closed", () => {
    // Keep running in the tray.
  });

  app.on("activate", () => {
    if (backendPort) showWindow();
  });

  app.on("before-quit", (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    shutdownBackend().finally(() => app.exit(0));
  });
}
