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
  const dialog = await screen.findByRole("dialog", { name: "检查现有内容，准备升级" });
  fireEvent.click(within(dialog).getByRole("button", { name: "检查升级内容" }));
  await within(dialog).findByRole("heading", { name: "升级内容已准备好" });
  return dialog;
}
async function reachConfirmation() {
  const dialog = await inspectFlow();
  fireEvent.click(within(dialog).getByRole("button", { name: "继续确认" }));
  await within(dialog).findByRole("heading", { name: "确认升级内容" });
  return dialog;
}

describe("M2 migration flow", () => {
  beforeEach(() => { route(); });

  it("keeps inspect single-flight and states that a failed check wrote nothing", async () => {
    let resolveInspect!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveInspect = resolve; });
    const calls = installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": () => pending });
    render(<App />); const button = await screen.findByRole("button", { name: "检查升级内容" });
    fireEvent.click(button); fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "检查中…" })).toBeDisabled();
    expect(calls.filter(([path]) => path === "/api/migration/inspect")).toHaveLength(1);
    resolveInspect(await jsonResponse({ ok: false, error: { code: "temporary", message: "检查暂时失败", fields: {} } }, 500));
    expect(await screen.findByText("暂时无法检查升级内容")).toBeVisible();
    expect(screen.getByText(/尚未创建备份或修改现有数据/)).toBeVisible();
  });

  it("renders only required choices, preserves a cancelled folder selection, and normalizes weekly scheduling", async () => {
    const plan = { ...MIGRATION_PLAN, discovery: { ...MIGRATION_PLAN.discovery, legacy_weekly_detected: true }, required_choices: ["source_directory", "trigger_mode"] };
    window.liveClipperShell = { selectFolder: vi.fn(async () => null) };
    installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": { ok: true, source: MIGRATION_SNAPSHOT.source, plan } });
    render(<App />); const dialog = await screen.findByRole("dialog"); fireEvent.click(await within(dialog).findByRole("button", { name: "检查升级内容" }));
    expect(await within(dialog).findByRole("heading", { name: "处理必要差异" })).toBeVisible();
    const source = within(dialog).getByDisplayValue("/recordings"); fireEvent.click(within(dialog).getByRole("button", { name: "选择…" }));
    expect(source.closest(".form-path-field")).not.toBeNull();
    await waitFor(() => expect(window.liveClipperShell?.selectFolder).toHaveBeenCalledTimes(1)); expect(source).toHaveValue("/recordings");
    fireEvent.click(within(dialog).getByRole("radio", { name: "定时自动检查" }));
    const schedule = within(dialog).getByRole("combobox", { name: "定时方式" }); expect(schedule).toHaveTextContent("每天固定时间");
    fireEvent.click(schedule); fireEvent.click(await screen.findByRole("option", { name: "固定间隔" }));
    expect(within(dialog).getByRole("combobox", { name: "检查间隔" })).toHaveTextContent("1 小时");
    expect(within(dialog).queryByLabelText("项目名称")).not.toBeInTheDocument();
  });

  it("shows safe history identities, resource attention, and blocks insufficient backup space", async () => {
    const entries = Array.from({ length: 24 }, (_, index) => ({ display_identity: `历史记录 ${index + 1}`, category: "importable", reason_code: null, reason_label: "可安全导入", safe_result: false }));
    const plan = { ...MIGRATION_PLAN, resources: { ...MIGRATION_PLAN.resources, ai: { ...MIGRATION_PLAN.resources.ai, status: "problem", credential_present: false } }, history: { ...MIGRATION_PLAN.history, entries }, backup: { ...MIGRATION_PLAN.backup, space_status: "insufficient" }, readiness: { ...MIGRATION_PLAN.readiness, resource_problems: ["ai", "backup_space"], can_start: false } };
    installFetchMock({ "/api/onboarding": MIGRATION_STARTUP, "/api/migration/inspect": { ok: true, source: MIGRATION_SNAPSHOT.source, plan } }); render(<App />);
    const dialog = await screen.findByRole("dialog"); fireEvent.click(await within(dialog).findByRole("button", { name: "检查升级内容" })); await within(dialog).findByText("迁移后处理");
    fireEvent.click(within(dialog).getByText("查看历史明细")); expect(within(dialog).getByText("历史记录 1")).toBeVisible(); expect(within(dialog).queryByText("历史记录 21")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "显示更多" })); expect(within(dialog).getByText("历史记录 24")).toBeVisible();
    expect(within(dialog).getByText("空间不足")).toBeVisible(); expect(within(dialog).getByRole("button", { name: "继续确认" })).toBeDisabled();
  });

  it("uses the latest validated plan and reuses one execute request id after uncertainty", async () => {
    let executeAttempts = 0; let accepted = false; const executeBodies: Array<Record<string, unknown>> = [];
    const executing = { ...SESSION, state: "backing_up", stage: "copy" };
    installFetchMock({
      "/api/onboarding": MIGRATION_STARTUP,
      "/api/migration": () => jsonResponse(accepted ? { ...MIGRATION_SNAPSHOT, entry: "executing", plan: null, session: executing } : MIGRATION_SNAPSHOT),
      "/api/migration/execute": (options?: RequestInit) => { executeAttempts += 1; executeBodies.push(JSON.parse(String(options?.body))); if (executeAttempts === 1) return Promise.reject(new Error("socket closed")); accepted = true; return jsonResponse({ ok: true, session: executing }, 202); },
    });
    render(<App />); const dialog = await reachConfirmation(); fireEvent.click(within(dialog).getByRole("button", { name: "开始升级" }));
    expect(await within(dialog).findByText(/升级请求暂时无法确认/)).toBeVisible(); fireEvent.click(within(dialog).getByRole("button", { name: "开始升级" }));
    await within(dialog).findByRole("heading", { name: "请保持 Venus 运行" });
    expect(executeBodies).toHaveLength(2); expect(executeBodies[0].request_id).toBe(executeBodies[1].request_id);
    expect(executeBodies[1].plan_hash).toBe(MIGRATION_PLAN.plan_hash);
  });

  it("shows only real stages, refreshes immediately on visibility, and keeps the last stage after a poll error", async () => {
    const executing = { ...SESSION, state: "migrating", stage: "history", revision: 3, processed_history_count: 2, total_history_count: 4 };
    let loads = 0; const originalHidden = document.hidden;
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    try {
      installFetchMock({ "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "executing", session: executing, report: null } }, "/api/migration": () => { loads += 1; return loads < 3 ? jsonResponse({ ...MIGRATION_SNAPSHOT, entry: "executing", plan: null, session: executing }) : Promise.reject(new Error("offline")); } });
      render(<App />); expect(await screen.findByRole("heading", { name: "请保持 Venus 运行" })).toBeVisible(); expect(screen.getByText("2 / 4 条")).toBeVisible();
      Object.defineProperty(document, "hidden", { configurable: true, value: false }); document.dispatchEvent(new Event("visibilitychange"));
      await waitFor(() => expect(loads).toBeGreaterThanOrEqual(2)); expect(screen.getByText("导入历史记录")).toBeVisible();
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
      fireEvent.click(within(dialog).getByRole("button", { name: "查看并修复" }));
      await waitFor(() => expect(migrationLoads).toBeGreaterThanOrEqual(2));
      expect(window.location.pathname).toBe("/studio");
      expect(calls.filter(([path]) => path === "/api/migration/acknowledge")).toHaveLength(1);

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
    ["completed_attention", "查看并修复", 2],
  ])("restores %s before workbench, reveals backup by id, acknowledges, and navigates", async (state, action, blockerCount) => {
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
    if (blockerCount) expect(within(dialog).getByText("2 项条件需要修复")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "在 Finder 中显示备份" })); await waitFor(() => expect(showBackup).toHaveBeenCalledWith("migration-1"));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: action })).toBeEnabled()); fireEvent.click(within(dialog).getByRole("button", { name: action }));
    await waitFor(() => expect(calls.filter(([path]) => path === "/api/migration/acknowledge")).toHaveLength(1));
    const projectHeading = await screen.findByRole("heading", { name: PROJECT.name }); expect(window.location.pathname).toBe("/projects/project-1");
    await waitFor(() => expect(projectHeading).toHaveFocus());
  });

  it("keeps failed facts explicit and returns source/plan drift to a fresh check", async () => {
    const failed = { ...SESSION, state: "failed_rolled_back", stage: "rolled_back", revision: 5, backup_status: "completed", failure: { code: "migration_apply_failed", summary: "迁移未提交，旧数据保持不变，可在确认后重试" } };
    installFetchMock({
      "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "failed", session: failed, report: null } },
      "/api/migration": { ...MIGRATION_SNAPSHOT, entry: "failed", plan: null, session: failed },
      "/api/migration/retry": () => jsonResponse({ ok: false, error: { code: "migration_plan_changed", message: "升级内容已变化，请重新检查", fields: {} } }, 409),
    });
    render(<App />); expect(await screen.findByText("没有修改")).toBeVisible(); expect(screen.getByText("已完成并可复用")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新尝试升级" })); expect(await screen.findByRole("heading", { name: "检查现有内容，准备升级" })).toBeVisible();
    expect(screen.getByText(/请修复后重新检查/)).toBeVisible();
  });

  it("fails closed for migration diagnostics and never exposes force-through actions", async () => {
    const diagnostic = { ...SESSION, state: "diagnostic_required", stage: null, failure: { code: "migration_integrity_failed", summary: "数据状态需要检查" } };
    installFetchMock({ "/api/onboarding": { ...MIGRATION_STARTUP, migration: { entry: "diagnostic", session: diagnostic, report: null } }, "/api/migration": { ...MIGRATION_SNAPSHOT, entry: "diagnostic", plan: null, session: diagnostic } });
    render(<App />); expect(await screen.findByRole("heading", { name: "数据状态需要检查" })).toBeVisible(); expect(screen.getByText(/问题编号：MIGRATION-INTEGRITY-FAILED/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /强制|忽略|删除|继续升级/ })).not.toBeInTheDocument();
  });
});
