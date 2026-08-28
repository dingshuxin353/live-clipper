const assert = require("node:assert/strict");
const test = require("node:test");

const {
  appUrl,
  createBadgePoller,
  createRuntimeState,
  formatBadgeCount,
} = require("../runtime-state");

test("formats only positive unseen result counts for the Dock", () => {
  assert.equal(formatBadgeCount(0), "");
  assert.equal(formatBadgeCount(-1), "");
  assert.equal(formatBadgeCount(3), "3");
  assert.equal(formatBadgeCount("12"), "12");
  assert.equal(formatBadgeCount("invalid"), "");
});

test("badge polling refreshes immediately, keeps the previous value on failure, and stops cleanly", async () => {
  const badgeValues = [];
  let intervalCallback = null;
  let cleared = null;
  let response = { unseen_result_count: 2 };
  const poller = createBadgePoller({
    client: { getStudio: async () => { if (response instanceof Error) throw response; return response; } },
    dock: { setBadge: (value) => badgeValues.push(value) },
    platform: "darwin",
    setIntervalFn: (callback, delay) => { intervalCallback = callback; assert.equal(delay, 15000); return 41; },
    clearIntervalFn: (timer) => { cleared = timer; },
  });

  await poller.start();
  assert.deepEqual(badgeValues, ["2"]);
  response = new Error("offline");
  await poller.refresh();
  assert.deepEqual(badgeValues, ["2"]);
  response = { unseen_result_count: null };
  await poller.refresh();
  assert.deepEqual(badgeValues, ["2"]);
  response = { unseen_result_count: "0" };
  await poller.refresh();
  assert.deepEqual(badgeValues, ["2"]);
  response = { unseen_result_count: 0 };
  await poller.activate();
  assert.deepEqual(badgeValues, ["2", ""]);
  response = { unseen_result_count: 7 };
  await intervalCallback();
  assert.deepEqual(badgeValues, ["2", "", "7"]);
  poller.stop();
  assert.equal(cleared, 41);
});

test("badge polling is a silent no-op away from macOS Dock", async () => {
  let requested = false;
  const poller = createBadgePoller({
    client: { getStudio: async () => { requested = true; return { unseen_result_count: 4 }; } },
    dock: null,
    platform: "linux",
  });
  await poller.start();
  assert.equal(requested, false);
  poller.stop();
});

test("starting badge polling twice keeps only one interval", async () => {
  let schedules = 0;
  const poller = createBadgePoller({
    client: { getStudio: async () => ({ unseen_result_count: 1 }) },
    dock: { setBadge: () => undefined },
    platform: "darwin",
    setIntervalFn: () => { schedules += 1; return 9; },
    clearIntervalFn: () => undefined,
  });
  await poller.start();
  await poller.start();
  assert.equal(schedules, 1);
  poller.stop();
});

test("runtime enters quitting once and blocks later startup work", () => {
  const runtime = createRuntimeState();
  assert.equal(runtime.canStart(), true);
  assert.equal(runtime.beginQuit(), true);
  assert.equal(runtime.beginQuit(), false);
  assert.equal(runtime.isQuitting(), true);
  assert.equal(runtime.canStart(), false);
});

test("desktop URLs stay on the authenticated local origin", () => {
  assert.equal(appUrl(8765, "/studio"), "http://127.0.0.1:8765/studio");
  assert.equal(appUrl(8765, "/projects"), "http://127.0.0.1:8765/projects");
  assert.equal(appUrl(8765, "//example.com/steal"), "http://127.0.0.1:8765/studio");
  assert.equal(appUrl(8765, "https://example.com"), "http://127.0.0.1:8765/studio");
});
