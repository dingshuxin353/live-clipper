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
        const count = Number(studio.pending_review_count);
        if (!Number.isFinite(count) || count < 0) throw new Error("invalid pending review count");
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

module.exports = { appUrl, createBadgePoller, createRuntimeState, formatBadgeCount, isInternalAppUrl };
