const nodeHttps = require("https");

function createUpdateFlow({ app, dialog, shell, runtime, getWindow, prepareForQuit, markExiting, https = nodeHttps, getUpdater = () => require("electron-updater").autoUpdater }) {
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
    updater = getUpdater();
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
      const { response } = await dialog.showMessageBox(getWindow(), {
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
      markExiting();
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
          const { response } = await dialog.showMessageBox(getWindow(), {
            type: "info", message: `发现新版本 ${result.latest}`,
            detail: `当前版本：${app.getVersion()}。请打开下载页获取新版本。`,
            buttons: ["打开下载页", "稍后"], defaultId: 1, cancelId: 1,
          });
          if (response === 0) { operation = "page"; await shell.openExternal(UPDATE_RELEASES_PAGE); }
        } else {
          await dialog.showMessageBox(getWindow(), { type: "info", message: `发现新版本 ${result.latest}`, detail: "Venus 将自动下载更新，下载完成后会提示你重启安装。" });
          if (updateFailure) return updateFailure;
        }
      } else {
        await dialog.showMessageBox(getWindow(), { type: "info", message: "未发现新版本", detail: `当前版本：${app.getVersion()}。` });
      }
      return { ok: true };
    } catch { return reportUpdateError(updateError(operation)); }
  }

  return { checkForUpdates };
}

module.exports = { createUpdateFlow };
