const path = require("path");

function formatBadgeCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count > 0 ? String(Math.floor(count)) : "";
}

function createBadgePoller({
  client,
  dock,
  platform = process.platform,
  intervalMs = 15000,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
}) {
  let timer = null;
  let stopped = true;
  let inFlight = null;
  const supported = platform === "darwin" && dock && typeof dock.setBadge === "function";

  const refresh = async () => {
    if (!supported || stopped) return false;
    if (inFlight) return inFlight;
    inFlight = (async () => {
      try {
        const studio = await client.getStudio();
        const count = studio.unseen_result_count;
        if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
          throw new Error("invalid unseen result count");
        }
        dock.setBadge(formatBadgeCount(count));
        return true;
      } catch (_error) {
        return false;
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };

  const start = async () => {
    if (!supported) return false;
    if (!stopped) return refresh();
    stopped = false;
    timer = setIntervalFn(() => void refresh(), intervalMs);
    if (timer && typeof timer.unref === "function") timer.unref();
    return refresh();
  };

  const stop = () => {
    stopped = true;
    if (timer !== null) clearIntervalFn(timer);
    timer = null;
  };

  return { activate: refresh, refresh, start, stop };
}

function requireDesktopId(value, label) {
  if (typeof value !== "string" || !value.trim() || value.length > 128) {
    throw new Error(`${label}无效`);
  }
  return value;
}

function requireResolvedPath(response) {
  const value = response?.path;
  if (typeof value !== "string" || !path.isAbsolute(value)) {
    throw new Error("成片路径无法读取");
  }
  return value;
}

function reuseInFlight(inFlight, key, operation) {
  if (inFlight.has(key)) return inFlight.get(key);
  const request = Promise.resolve()
    .then(operation)
    .finally(() => inFlight.delete(key));
  inFlight.set(key, request);
  return request;
}

function createOutputActions({ client, shell, runtime }) {
  const inFlight = new Map();

  const run = (action, outputId, operation) => {
    const id = requireDesktopId(outputId, "output_id");
    if (!runtime.canStart()) throw new Error("应用正在退出，暂时无法操作成片");
    return reuseInFlight(inFlight, `${action}:${id}`, async () => {
      const target = requireResolvedPath(await client.resolveOutputPath(id));
      await operation(target);
      return { ok: true };
    });
  };

  return {
    openOutput(outputId) {
      return run("open", outputId, async (target) => {
        let failure;
        try {
          failure = await shell.openPath(target);
        } catch (_error) {
          throw new Error("无法打开成片，请确认文件仍然存在");
        }
        if (failure) throw new Error("无法打开成片，请确认文件仍然存在");
      });
    },
    revealOutput(outputId) {
      return run("reveal", outputId, async (target) => {
        try {
          shell.showItemInFolder(target);
        } catch (_error) {
          throw new Error("无法在 Finder 中显示成片，请确认文件仍然存在");
        }
      });
    },
  };
}

const SOURCE_FILTERS = [{ name: "视频文件", extensions: ["m4v", "mkv", "mov", "mp4", "webm"] }];

function createFolderSelection({ dialog, runtime, getWindow = () => null }) {
  let inFlight = null;

  const select = (title) => {
    if (!runtime.canStart()) throw new Error("应用正在退出，暂时无法选择文件夹");
    if (inFlight) return inFlight;
    const normalizedTitle = typeof title === "string" && title.trim()
      ? title.trim().slice(0, 80)
      : "选择文件夹";
    inFlight = Promise.resolve()
      .then(async () => {
        const options = {
          title: normalizedTitle,
          properties: ["openDirectory", "createDirectory"],
        };
        const owner = getWindow();
        let result;
        try {
          result = owner ? await dialog.showOpenDialog(owner, options) : await dialog.showOpenDialog(options);
        } catch (_error) {
          throw new Error("无法打开文件夹选择器，请稍后重试");
        }
        if (!runtime.canStart() || result?.canceled || !result?.filePaths?.[0]) return null;
        const selectedPath = result.filePaths[0];
        if (typeof selectedPath !== "string" || !path.isAbsolute(selectedPath)) {
          throw new Error("文件夹路径无效");
        }
        return selectedPath;
      })
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };

  return { select };
}

function createFileSelections({ client, dialog, runtime, getWindow = () => null, now = () => Date.now() }) {
  const inFlight = new Map();

  const select = (issueId, kind) => {
    const id = requireDesktopId(issueId, "issue_id");
    if (!runtime.canStart()) throw new Error("应用正在退出，暂时无法选择文件");
    return reuseInFlight(inFlight, `${kind}:${id}`, async () => {
      const source = kind === "source";
      const options = source
        ? { title: "重新选择原录像", properties: ["openFile"], filters: SOURCE_FILTERS }
        : { title: "选择本次恢复目录", properties: ["openDirectory", "createDirectory"] };
      const owner = getWindow();
      let result;
      try {
        result = owner ? await dialog.showOpenDialog(owner, options) : await dialog.showOpenDialog(options);
      } catch (_error) {
        throw new Error("无法打开文件选择器，请稍后重试");
      }
      if (result.canceled || !result.filePaths?.[0]) return null;
      let grant;
      try {
        grant = await client.registerFileSelection(id, kind, result.filePaths[0]);
      } catch (_error) {
        throw new Error("文件选择授权失败，请稍后重试");
      }
      if (typeof grant?.selection_token !== "string" || !grant.selection_token) {
        throw new Error("文件选择授权无法创建");
      }
      const ttl = Number(grant.expires_in_seconds);
      if (!Number.isFinite(ttl) || ttl <= 0) throw new Error("文件选择授权无效");
      return {
        selectionToken: grant.selection_token,
        expiresAt: new Date(now() + ttl * 1000).toISOString(),
      };
    });
  };

  return {
    selectIssueSource: (issueId) => select(issueId, "source"),
    selectRecoveryOutput: (issueId) => select(issueId, "recovery_output"),
  };
}

function writeClipboardText(clipboard, runtime, value) {
  if (!runtime.canStart()) throw new Error("应用正在退出，暂时无法复制");
  if (typeof value !== "string" || value.length > 20000) throw new Error("复制内容无效或过长");
  clipboard.writeText(value);
  return { ok: true };
}

function createRuntimeState() {
  let quitting = false;
  return {
    beginQuit() {
      if (quitting) return false;
      quitting = true;
      return true;
    },
    canStart: () => !quitting,
    isQuitting: () => quitting,
  };
}

function safeAppPath(route) {
  return typeof route === "string" && route.startsWith("/") && !route.startsWith("//") ? route : "/studio";
}

function appUrl(port, route = "/studio") {
  return `http://127.0.0.1:${port}${safeAppPath(route)}`;
}

function isInternalAppUrl(value, port) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.port === String(port);
  } catch (_error) {
    return false;
  }
}

module.exports = {
  appUrl,
  createBadgePoller,
  createFileSelections,
  createFolderSelection,
  createOutputActions,
  createRuntimeState,
  formatBadgeCount,
  isInternalAppUrl,
  requireDesktopId,
  writeClipboardText,
};
