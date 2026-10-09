import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { MIGRATION_PLAN, MIGRATION_SNAPSHOT, MIGRATION_STARTUP, PROJECT, WORKBENCH_ONBOARDING, installFetchMock, jsonResponse } from "./helpers";

const SESSION = {
  migration_id: "migration-1", state: "backing_up", stage: "copy", revision: 1,
  processed_history_count: null, total_history_count: null, backup_status: "pending",
  failure: null, project_id: null, started_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
};
const REPORT = {
  plan_version: 3, plan_hash: "a".repeat(64), project: { project_id: "project-1", name: "默认项目" },
  discovery: { legacy_weekly_detected: false, existing_recordings_scanned: false, trigger_mode: "manual", schedule_mode: null, daily_time: null, interval_minutes: null },
  imported: 2, compatibility: 1, quarantined: 1, safe_results: 1, history_total: 4,
  quarantine_reason_codes: ["state_unrecognized"], backup_created: true, readiness: "ready",
  blocker_count: 0, blocker_codes: [], completed_at: "2026-09-01T00:01:00Z", acknowledged_at: null,
};

function route(path = "/studio") { window.history.replaceState({}, "", path); }
async function inspectFlow() {
  const dialog = await screen.findByRole("dialog", { name: "检查旧版数据" });
  fireEvent.click(within(dialog).getByRole("button", { name: "开始检查" }));
  await within(dialog).findByRole("heading", { name: "核对升级内容" });
  return dialog;
}
async function reachConfirmation() {
  const dialog = await inspectFlow();
  fireEvent.click(within(dialog).getByRole("button", { name: "下一步" }));
  await within(dialog).findByRole("heading", { name: "确认升级" });
  return dialog;
}

describe("M2 migration flow", () => {
  beforeEach(() => { sessionStorage.clear(); route(); });

  it("keeps inspect single-flight and states that a failed check wrote nothing", async () => {
    let resolveInspect!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveInspect = resolve; });
    const calls = installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": () => pending });
    render(<App />); const button = await screen.findByRole("button", { name: "开始检查" });
    fireEvent.click(button); fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "检查中…" })).toBeDisabled();
    expect(calls.filter(([path]) => path === "/api/migration/inspect")).toHaveLength(1);
    resolveInspect(await jsonResponse({ ok: false, error: { code: "temporary", message: "检查暂时失败", fields: {} } }, 500));
    expect(await screen.findByText("检查未完成，请重试。")).toBeVisible();
    expect(screen.getByText(/未修改旧版数据/)).toBeVisible();
  });

  it("renders only required choices, preserves a cancelled folder selection, and normalizes weekly scheduling", async () => {
    const plan = { ...MIGRATION_PLAN, discovery: { ...MIGRATION_PLAN.discovery, legacy_weekly_detected: true }, required_choices: ["source_directory", "trigger_mode"] };
    window.liveClipperShell = { selectFolder: vi.fn(async () => null) };
    installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": { ok: true, source: MIGRATION_SNAPSHOT.source, plan } });
    render(<App />); const dialog = await screen.findByRole("dialog"); fireEvent.click(await within(dialog).findByRole("button", { name: "开始检查" }));
    expect(await within(dialog).findByRole("heading", { name: "核对升级内容" })).toBeVisible();
    const source = within(dialog).getByDisplayValue("/recordings"); fireEvent.click(within(dialog).getByRole("button", { name: "选择文件夹" }));
    expect(source.closest(".form-path-field")).not.toBeNull();
    await waitFor(() => expect(window.liveClipperShell?.selectFolder).toHaveBeenCalledTimes(1)); expect(source).toHaveValue("/recordings");
    fireEvent.click(within(dialog).getByRole("radio", { name: "定时扫描（也可手动）" }));
    const schedule = within(dialog).getByRole("combobox", { name: "定时方式" }); expect(schedule).toHaveTextContent("每天固定时间");
    fireEvent.click(schedule); fireEvent.click(await screen.findByRole("option", { name: "固定间隔" }));
    expect(within(dialog).getByRole("combobox", { name: "扫描间隔" })).toHaveTextContent("1 小时");
    expect(within(dialog).queryByLabelText("项目名称")).not.toBeInTheDocument();
  });

  it("shows safe history identities, resource attention, and blocks insufficient backup space", async () => {
    const entries = Array.from({ length: 24 }, (_, index) => ({ display_identity: `历史记录 ${index + 1}`, category: "importable", reason_code: null, reason_label: "可安全导入", safe_result: false }));
    const plan = { ...MIGRATION_PLAN, resources: { ...MIGRATION_PLAN.resources, ai: { ...MIGRATION_PLAN.resources.ai, status: "problem", credential_present: false } }, history: { ...MIGRATION_PLAN.history, entries }, backup: { ...MIGRATION_PLAN.backup, space_status: "insufficient" }, readiness: { ...MIGRATION_PLAN.readiness, resource_problems: ["ai", "backup_space"], can_start: false } };
    installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": { ok: true, source: MIGRATION_SNAPSHOT.source, plan } }); render(<App />);
    const dialog = await screen.findByRole("dialog"); fireEvent.click(await within(dialog).findByRole("button", { name: "开始检查" })); await within(dialog).findByText("升级后检查");
    fireEvent.click(within(dialog).getByText("查看记录明细")); expect(within(dialog).getByText("历史记录 1")).toBeVisible(); expect(within(dialog).queryByText("历史记录 21")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "显示更多" })); expect(within(dialog).getByText("历史记录 24")).toBeVisible();
    expect(within(dialog).getByText("备份空间不足，请释放磁盘空间后返回重新检查。")).toBeVisible(); expect(within(dialog).getByRole("button", { name: "下一步" })).toBeDisabled();
  });

  it("uses the latest validated plan and reuses one execute request id after uncertainty", async () => {
    let executeAttempts = 0; let accepted = false; const executeBodies: Array<Record<string, unknown>> = [];
    const executing = { ...SESSION, state: "backing_up", stage: "copy" };
    installFetchMock({
      "/api/onboarding": MIGRATION_STARTUP,
      "/api/migration": () => jsonResponse(accepted ? { ...MIGRATION_SNAPSHOT, entry: "executing", plan: null, session: executing } : MIGRATION_SNAPSHOT),
      "/api/migration/execute": (options?: RequestInit) => { executeAttempts += 1; executeBodies.push(JSON.parse(String(options?.body))); if (executeAttempts === 1) return Promise.resolve(new Response("<html>lost JSON response</html>", { status: 200 })); accepted = true; return jsonResponse({ ok: true, session: executing }, 202); },
    });
    render(<App />); const dialog = await reachConfirmation(); fireEvent.click(within(dialog).getByRole("button", { name: "开始升级" }));
    expect(await within(dialog).findByText(/暂时无法确认升级是否已开始/)).toBeVisible(); expect(within(dialog).getByRole("button", { name: "返回修改" })).toBeDisabled(); fireEvent.click(within(dialog).getByRole("button", { name: "继续本次操作" }));
    await within(dialog).findByRole("heading", { name: "正在升级" });
    expect(executeBodies).toHaveLength(2); expect(executeBodies[0].request_id).toBe(executeBodies[1].request_id);
    expect(executeBodies[1].plan_hash).toBe(MIGRATION_PLAN.plan_hash); expect(executeBodies[1]).toEqual(executeBodies[0]);
  });

  it("shows only real stages, refreshes immediately on visibility, and keeps the last stage after a poll error", async () => {
    const executing = { ...SESSION, state: "migrating", stage: "history", revision: 3, processed_history_count: 2, total_history_count: 4 };
    let loads = 0; const originalHidden = document.hidden;
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    try {
      installFetchMock({ "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "executing", session: executing, report: null } }, "/api/migration": () => { loads += 1; return loads < 3 ? jsonResponse({ ...MIGRATION_SNAPSHOT, entry: "executing", plan: null, session: executing }) : Promise.reject(new Error("offline")); } });
      render(<App />); expect(await screen.findByRole("heading", { name: "正在升级" })).toBeVisible(); expect(screen.queryByText("2 / 4 条")).not.toBeInTheDocument(); expect(document.querySelectorAll(".migration-stage-list .done")).toHaveLength(0);
      Object.defineProperty(document, "hidden", { configurable: true, value: false }); document.dispatchEvent(new Event("visibilitychange"));
      await waitFor(() => expect(loads).toBeGreaterThanOrEqual(2)); expect(screen.getByText("导入旧版数据与模型配置")).toBeVisible();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument(); expect(screen.queryByText(/完成 50%|还需 \d+ 分钟/)).not.toBeInTheDocument();
    } finally { Object.defineProperty(document, "hidden", { configurable: true, value: originalHidden }); }
  });

  it("enters the persisted project when the acknowledge response stays pending", async () => {
    const session = { ...SESSION, state: "completed_attention", stage: null, revision: 8, backup_status: "completed", project_id: "project-1" };
    let acknowledged = false; let migrationLoads = 0; const originalHidden = document.hidden;
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    try {
      const calls = installFetchMock({
        "/api/onboarding": () => jsonResponse({ ...WORKBENCH_ONBOARDING, migration: { entry: "completed", session, report: { ...REPORT, acknowledged_at: acknowledged ? "2026-09-01T00:02:00Z" : null } } }),
        "/api/migration": () => { migrationLoads += 1; return jsonResponse({ ...MIGRATION_SNAPSHOT, entry: "completed", plan: null, session, report: { ...REPORT, acknowledged_at: acknowledged ? "2026-09-01T00:02:00Z" : null } }); },
        "/api/migration/acknowledge": () => new Promise<Response>(() => {}),
      });
      render(<App />); const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "进入项目" }));
      await waitFor(() => expect(migrationLoads).toBeGreaterThanOrEqual(2));
      expect(window.location.pathname).toBe("/studio");
      expect(calls.filter(([path]) => path === "/api/migration/acknowledge")).toHaveLength(1);
      const acknowledgeBody = JSON.parse(String(calls.find(([path]) => path === "/api/migration/acknowledge")?.[1]?.body));
      expect(JSON.parse(sessionStorage.getItem('venus.migration.pending')!)).toEqual({ kind: 'acknowledge', id: acknowledgeBody.request_id, migrationId: 'migration-1', revision: 8 });

      acknowledged = true;
      await waitFor(() => { fireEvent(document, new Event("visibilitychange")); expect(migrationLoads).toBeGreaterThanOrEqual(3); });
      await screen.findByRole("heading", { name: PROJECT.name });
      const loadsAfterEnter = migrationLoads; fireEvent(document, new Event("visibilitychange")); await Promise.resolve();
      expect(window.location.pathname).toBe("/projects/project-1");
      expect(migrationLoads).toBe(loadsAfterEnter);
    } finally { Object.defineProperty(document, "hidden", { configurable: true, value: originalHidden }); }
  });

  it.each([
    ["completed_ready", "进入项目", 0],
    ["completed_attention", "进入项目", 2],
  ])("restores %s before workbench, reveals backup by id, acknowledges, and navigates", async (state, action, blockerCount) => {
    route("/");
    const session = { ...SESSION, state, stage: null, revision: 8, backup_status: "completed", project_id: "project-1" };
    const report = { ...REPORT, readiness: blockerCount ? "attention" : "ready", blocker_count: blockerCount, blocker_codes: blockerCount ? ["asr", "ai"] : [] };
    let acknowledged = false;
    const showBackup = vi.fn(async () => ({ ok: true as const })); window.liveClipperShell = { showBackup };
    const calls = installFetchMock({
      "/api/onboarding": () => jsonResponse(acknowledged
        ? { ...WORKBENCH_ONBOARDING, migration: { entry: "completed", session, report: { ...report, acknowledged_at: "2026-09-01T00:02:00Z" } } }
        : { ...WORKBENCH_ONBOARDING, migration: { entry: "completed", session, report } }),
      "/api/migration": { ...MIGRATION_SNAPSHOT, entry: "completed", plan: null, session, report },
      "/api/migration/acknowledge": () => { acknowledged = true; return jsonResponse({ ok: true, session, project_id: "project-1" }); },
    });
    render(<App />); const dialog = await screen.findByRole("dialog"); expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
    if (blockerCount) expect(within(dialog).getByText("2 个问题待处理。")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "在 Finder 中显示备份" })); await waitFor(() => expect(showBackup).toHaveBeenCalledWith("migration-1"));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: action })).toBeEnabled()); fireEvent.click(within(dialog).getByRole("button", { name: action }));
    await waitFor(() => expect(calls.filter(([path]) => path === "/api/migration/acknowledge")).toHaveLength(1));
    const projectHeading = await screen.findByRole("heading", { name: PROJECT.name }); expect(window.location.pathname).toBe("/projects/project-1");
    await waitFor(() => expect(projectHeading).toHaveFocus());
  });

  it("keeps failed facts and reads original session after plan drift without editing a new plan", async () => {
    const failed = { ...SESSION, state: "failed_rolled_back", stage: "rolled_back", revision: 5, backup_status: "completed", failure: { code: "migration_apply_failed", summary: "迁移未提交，旧数据保持不变，可在确认后重试" } };
    let retried = false;
    const calls = installFetchMock({
      "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "failed", session: failed, report: null } },
      "/api/migration": () => jsonResponse({ ...MIGRATION_SNAPSHOT, entry: "failed", plan: null, session: retried ? { ...failed, failure: { code: "migration_plan_changed", summary: "原计划不一致" } } : failed }),
      "/api/migration/retry": () => { retried = true; return jsonResponse({ ok: false, error: { code: "migration_plan_changed", message: "原计划不一致，无法直接继续升级。", fields: {} } }, 409); },
    });
    render(<App />); expect(await screen.findByText("本次升级未修改")).toBeVisible(); expect(screen.getByText("已完成，重试时会重新检查。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重试升级" })); expect(await screen.findByRole("heading", { name: "升级未完成" })).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "重新读取状态" }));
    expect(calls.filter(([path]) => path === "/api/migration/retry")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "返回修改" })).not.toBeInTheDocument();
  });

  it("fails closed for migration diagnostics and never exposes force-through actions", async () => {
    const diagnostic = { ...SESSION, state: "diagnostic_required", stage: null, failure: { code: "migration_integrity_failed", summary: "暂时无法确认数据状态" } };
    installFetchMock({ "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "diagnostic", session: diagnostic, report: null } }, "/api/migration": { ...MIGRATION_SNAPSHOT, entry: "diagnostic", plan: null, session: diagnostic } });
    render(<App />); expect(await screen.findByRole("heading", { name: "暂时无法确认数据状态" })).toBeVisible(); expect(screen.getByText(/问题编号：MIGRATION-INTEGRITY-FAILED/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /强制|忽略|删除|继续升级/ })).not.toBeInTheDocument();
  });
});

it('restores an unknown retry and keeps its original revision and request identity', async () => {
  sessionStorage.clear(); route();
  const failed = { ...SESSION, state: 'failed_rolled_back', stage: 'rolled_back', revision: 5, backup_status: 'completed', failure: { code: 'migration_apply_failed', summary: '上次升级失败' } };
  sessionStorage.setItem('venus.migration.pending', JSON.stringify({ kind: 'retry', id: 'original-retry', migrationId: 'migration-1', revision: 5 }));
  let accepted = false;
  window.liveClipperShell = { quitApp: vi.fn() };
  const calls = installFetchMock({
    '/api/onboarding': { ...MIGRATION_STARTUP, migration: { entry: 'failed', session: failed, report: null } },
    '/api/migration': () => jsonResponse({ ...MIGRATION_SNAPSHOT, entry: accepted ? 'executing' : 'failed', session: accepted ? { ...SESSION, revision: 6 } : failed, report: null }),
    '/api/migration/retry': () => { accepted = true; return jsonResponse({ session: { ...SESSION, revision: 6 } }); },
  });
  render(<App />);
  const resume = await screen.findByRole('button', { name: '继续本次操作' });
  expect(screen.getByRole('button', { name: '退出 Venus' })).toBeDisabled();
  expect(screen.queryByText('未创建')).not.toBeInTheDocument();
  fireEvent.click(resume);
  await screen.findByRole('heading', { name: '正在升级' });
  const request = calls.find(([path]) => path === '/api/migration/retry');
  expect(JSON.parse(String(request?.[1]?.body))).toEqual({ request_id: 'original-retry', migration_id: 'migration-1', expected_revision: 5 });
  expect(calls.some(([path]) => path === '/api/migration/execute')).toBe(false);
});

it('does not show a successful report when its required backup is inconsistent', async () => {
  sessionStorage.clear(); route();
  const session = { ...SESSION, state: 'completed_attention', backup_status: 'failed', project_id: 'project-1' };
  const report = { ...REPORT, backup_created: false };
  installFetchMock({ '/api/onboarding': { ...MIGRATION_STARTUP, migration: { entry: 'completed', session, report } }, '/api/migration': { ...MIGRATION_SNAPSHOT, entry: 'completed', session, report } });
  render(<App />);
  expect(await screen.findByRole('heading', { name: '暂时无法确认数据状态' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '进入项目' })).not.toBeInTheDocument();
});

describe('migration request storage boundary', () => {
  beforeEach(() => { sessionStorage.clear(); route(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('persists only execute identity and keeps the complete original request in memory', async () => {
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP, '/api/migration/execute': () => Promise.reject(new Error('offline')) });
    render(<App />); await reachConfirmation(); fireEvent.click(screen.getByRole('button', { name: '开始升级' }));
    await screen.findByText(/暂时无法确认升级是否已开始/);
    const body = JSON.parse(String(calls.find(([path]) => path === '/api/migration/execute')?.[1]?.body));
    expect(body.choices).toEqual(MIGRATION_PLAN.choices);
    expect(JSON.parse(sessionStorage.getItem('venus.migration.pending')!)).toEqual({ kind: 'execute', id: body.request_id });
    fireEvent.click(screen.getByRole('button', { name: '继续本次操作' }));
    await waitFor(() => expect(calls.filter(([path]) => path === '/api/migration/execute')).toHaveLength(2));
    expect(JSON.parse(String(calls.filter(([path]) => path === '/api/migration/execute')[1][1]?.body))).toEqual(body);
  });

  it.each([false, true])('restores a reloaded execute from backend facts only (accepted=%s)', async accepted => {
    sessionStorage.setItem('venus.migration.pending', JSON.stringify({ kind: 'execute', id: 'original-execute' }));
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP, '/api/migration': accepted ? { ...MIGRATION_SNAPSHOT, entry: 'executing', session: SESSION, plan: null } : MIGRATION_SNAPSHOT });
    render(<App />);
    if (accepted) {
      await screen.findByRole('heading', { name: '正在升级' });
      expect(sessionStorage.getItem('venus.migration.pending')).toBeNull();
    } else {
      await screen.findByRole('heading', { name: '正在核对升级结果' });
      fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
      await waitFor(() => expect(calls.filter(([path]) => path === '/api/migration').length).toBeGreaterThan(1));
      expect(sessionStorage.getItem('venus.migration.pending')).not.toBeNull();
      expect(screen.queryByRole('button', { name: /开始检查|开始升级|继续本次操作/ })).not.toBeInTheDocument();
    }
    expect(calls.some(([path, options]) => path.startsWith('/api/migration/') && options?.method === 'POST')).toBe(false);
  });

  it.each([
    '{broken', 'null', '{}', JSON.stringify({ kind: 'execute', id: 'invalid/id' }),
    JSON.stringify({ kind: 'execute', id: 'old-execute', plan: MIGRATION_PLAN }),
    JSON.stringify({ kind: 'retry', id: 'retry', migrationId: 'migration-1', revision: -1 }),
  ])('does not create an operation from an invalid stored identity: %s', async raw => {
    sessionStorage.setItem('venus.migration.pending', raw);
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP });
    render(<App />); await screen.findByText(/无法读取原操作标识/);
    fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
    await waitFor(() => expect(calls.filter(([path]) => path === '/api/migration').length).toBeGreaterThan(1));
    expect(screen.queryByRole('button', { name: /开始检查|开始升级/ })).not.toBeInTheDocument();
    expect(calls.some(([path, options]) => path.startsWith('/api/migration/') && options?.method === 'POST')).toBe(false);
  });

  it('keeps a failed storage read distinct from no pending operation', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP });
    render(<App />); await screen.findByText(/无法读取原操作标识/);
    expect(screen.queryByRole('button', { name: '开始检查' })).not.toBeInTheDocument();
    expect(calls.some(([path, options]) => path.startsWith('/api/migration/') && options?.method === 'POST')).toBe(false);
  });

  it('does not send execute if saving identity fails before submission', async () => {
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP });
    render(<App />); await reachConfirmation();
    const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
    fireEvent.click(screen.getByRole('button', { name: '开始升级' }));
    await screen.findByText('本次请求未发送：无法保存操作标识，请重试。');
    expect(calls.some(([path]) => path === '/api/migration/execute')).toBe(false);
    save.mockRestore();
  });

  it('does not treat a cleanup failure as execution failure or resend an accepted request', async () => {
    let accepted = false;
    const calls = installFetchMock({
      '/api/onboarding': MIGRATION_STARTUP,
      '/api/migration': () => jsonResponse(accepted ? { ...MIGRATION_SNAPSHOT, entry: 'executing', session: SESSION, plan: null } : MIGRATION_SNAPSHOT),
      '/api/migration/execute': () => { accepted = true; return jsonResponse({ ok: true, session: SESSION }, 202); },
    });
    render(<App />); await reachConfirmation();
    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    fireEvent.click(screen.getByRole('button', { name: '开始升级' }));
    await screen.findByText(/已收到操作结果，但无法清除本地操作标识/);
    expect(screen.queryByRole('heading', { name: '升级未完成' })).not.toBeInTheDocument();
    remove.mockRestore(); fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
    await screen.findByRole('heading', { name: '正在升级' });
    expect(sessionStorage.getItem('venus.migration.pending')).toBeNull();
    expect(calls.filter(([path]) => path === '/api/migration/execute')).toHaveLength(1);
  });

  it('does not replay a stored retry against a different backend session', async () => {
    sessionStorage.setItem('venus.migration.pending', JSON.stringify({ kind: 'retry', id: 'original-retry', migrationId: 'other-session', revision: 5 }));
    const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP, '/api/migration': { ...MIGRATION_SNAPSHOT, entry: 'failed', session: { ...SESSION, state: 'failed_rolled_back', revision: 5 } } });
    render(<App />); await screen.findByRole('heading', { name: '正在核对升级结果' });
    expect(screen.queryByRole('button', { name: '继续本次操作' })).not.toBeInTheDocument();
    expect(calls.some(([path]) => path === '/api/migration/retry')).toBe(false);
  });
});

it('can clear a rejected request marker without treating the rejection as accepted', async () => {
  sessionStorage.clear(); route();
  const calls = installFetchMock({ '/api/onboarding': MIGRATION_STARTUP, '/api/migration/execute': () => jsonResponse({ ok: false, error: { code: 'migration_plan_changed', message: '计划已变化', fields: {} } }, 409) });
  render(<App />); await reachConfirmation();
  const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
  try {
    fireEvent.click(screen.getByRole('button', { name: '开始升级' }));
    await screen.findByText(/已收到操作结果，但无法清除本地操作标识/);
  } finally { remove.mockRestore(); }
  fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }));
  await screen.findByRole('button', { name: '开始检查' });
  expect(sessionStorage.getItem('venus.migration.pending')).toBeNull();
  expect(calls.filter(([path]) => path === '/api/migration/execute')).toHaveLength(1);
  expect(screen.queryByRole('button', { name: '进入项目' })).not.toBeInTheDocument();
});

it('shows retained history and resumes an acknowledged but incomplete resource migration', async () => {
  sessionStorage.clear(); route();
  const session = { ...SESSION, state: 'completed_attention', stage: 'complete', revision: 8, backup_status: 'completed', project_id: 'project-1' };
  let current = { ...MIGRATION_SNAPSHOT, entry: 'incomplete', session, report: null };
  const calls = installFetchMock({
    '/api/onboarding': { ...MIGRATION_STARTUP, migration: current },
    '/api/migration': () => jsonResponse(current),
    '/api/migration/migration-1/history': { ok: true, history: [{ run_id: 'retained', status: 'completed', created_at: '2026-09-01' }] },
    '/api/migration/retry': () => {
      current = { ...current, entry: 'executing', session: { ...session, state: 'validating', stage: 'resources', revision: 9 } };
      return jsonResponse({ ok: true, session: current.session });
    },
  });
  render(<App />);
  expect(await screen.findByRole('heading', { name: '升级尚未完成' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '进入项目' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看已保存的历史记录' }));
  expect(await screen.findByText('已保存 1 条记录。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '继续升级' }));
  expect(await screen.findByText('转换模型配置')).toBeVisible();
  const request = calls.find(([path]) => path === '/api/migration/retry');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ migration_id: 'migration-1', expected_revision: 8 });
  expect(calls.some(([path]) => path === '/api/migration/execute')).toBe(false);
});


it.each(['migration_backup_invalid', 'migration_credential_source_unknown'])('shows the specific recovery error %s without reporting rollback', async (code) => {
  sessionStorage.clear(); route();
  const current = { ...MIGRATION_SNAPSHOT, entry: 'incomplete', session: { ...SESSION, state: 'completed_attention', revision: 8, project_id: 'project-1', backup_status: 'completed' }, report: null };
  installFetchMock({
    '/api/onboarding': { ...MIGRATION_STARTUP, migration: current }, '/api/migration': current,
    '/api/migration/retry': () => jsonResponse({ ok: false, error: { code, message: '原升级条件无法核验，请保留项目并联系开发者。' } }, 409),
  });
  render(<App />); fireEvent.click(await screen.findByRole('button', { name: '继续升级' }));
  expect(await screen.findByText('原升级条件无法核验，请保留项目并联系开发者。')).toBeVisible();
  expect(screen.queryByText('未创建')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '进入项目' })).not.toBeInTheDocument();
});
