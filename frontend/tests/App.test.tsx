import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { PROJECT, RUN, WORKBENCH_ONBOARDING, installFetchMock, jsonResponse } from "./helpers";

function route(path: string) { window.history.replaceState({}, "", path); }

async function openCreateWizard() {
  fireEvent.click(await screen.findByRole("button", { name: /新建项目/ }));
  const dialog = await screen.findByRole("dialog", { name: "新建项目" });
  fireEvent.change(within(dialog).getByLabelText(/项目名称/), { target: { value: "访谈项目" } });
  fireEvent.change(within(dialog).getByLabelText(/录像目录/), { target: { value: "/recordings/interview" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "下一步" }));
  await screen.findByText(/第 2 步/);
  fireEvent.click(within(dialog).getByRole("button", { name: "下一步" }));
  await screen.findByText(/第 3 步/);
  fireEvent.change(within(dialog).getByLabelText(/成片输出目录/), { target: { value: "/outputs/interview" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "下一步" }));
  await screen.findByText(/第 4 步/);
  return dialog;
}

describe("Venus 1.0 core workbench", () => {
  beforeEach(() => { route("/studio"); localStorage.clear(); });

  it("holds back the workbench until startup classification completes", async () => {
    installFetchMock({ "/api/onboarding": () => new Promise<Response>(() => undefined) }); render(<App />);
    expect(screen.getByText("正在准备 Venus")).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /新建项目/ })).not.toBeInTheDocument();
  });

  it("aborts the startup classification when the gate is discarded", async () => {
    let signal: AbortSignal | undefined;
    installFetchMock({ "/api/onboarding": (options?: RequestInit) => { signal = options?.signal as AbortSignal | undefined; return new Promise<Response>(() => undefined); } });
    const view = render(<App />); await waitFor(() => expect(signal).toBeDefined()); view.unmount(); expect(signal?.aborted).toBe(true);
  });

  it("fails closed for the retired or unknown startup DTO", async () => {
    installFetchMock({ "/api/onboarding": { needs_onboarding: false } }); render(<App />);
    expect(await screen.findByText("暂时无法确认数据状态")).toBeVisible(); expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
  });

  it.each([
    ["migration_required", "检测到现有数据"],
    ["diagnostic_required", "数据状态需要检查"],
  ])("renders the %s safety boundary without onboarding or project creation", async (mode, title) => {
    installFetchMock({ "/api/onboarding": { ...WORKBENCH_ONBOARDING, entry: { mode, onboarding: null, reason_code: "safe-123", evidence_codes: ["existing"] } } }); render(<App />);
    expect(await screen.findByText(title)).toBeVisible(); expect(screen.getByText("问题编号：safe-123")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(screen.queryByRole("button", { name: /新建项目/ })).not.toBeInTheDocument();
  });

  it("starts a new session before opening the five-step overlay", async () => {
    const session = { state: "in_progress", current_step: "welcome", revision: 1, draft: {}, pending_finish_request_id: null, failure: null, first_project: null };
    const calls = installFetchMock({
      "/api/onboarding": { ...WORKBENCH_ONBOARDING, entry: { mode: "onboarding", onboarding: "new", reason_code: null, evidence_codes: [] }, session: null },
      "/api/onboarding/start": { ok: true, session },
    });
    render(<App />); expect(await screen.findByRole("dialog", { name: "开始" })).toBeVisible();
    expect(calls.some(([path]) => path === "/api/onboarding/start")).toBe(true);
  });

  it("keeps a paused session in Studio with one resume entry", async () => {
    const session = { state: "paused", current_step: "ai", revision: 4, draft: {}, pending_finish_request_id: null, failure: null, first_project: null };
    installFetchMock({ "/api/onboarding": { ...WORKBENCH_ONBOARDING, entry: { mode: "onboarding", onboarding: "paused", reason_code: null, evidence_codes: [] }, session } }); render(<App />);
    expect(await screen.findByText("首次设置尚未完成")).toBeVisible(); expect(screen.getAllByRole("button", { name: "继续首次设置" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /新建项目/ })).not.toBeInTheDocument(); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("returns focus to the Studio resume entry after Escape pauses onboarding", async () => {
    const session = { state: "in_progress" as const, current_step: "welcome" as const, revision: 1, draft: {}, pending_finish_request_id: null, failure: null, first_project: null };
    const saved = { ...session, revision: 2 }; const paused = { ...saved, state: "paused" as const, revision: 3 };
    installFetchMock({
      "/api/onboarding": { ...WORKBENCH_ONBOARDING, entry: { mode: "onboarding", onboarding: "resume", reason_code: null, evidence_codes: [] }, session },
      "/api/onboarding/environment-check": { ok: true, environment: WORKBENCH_ONBOARDING.environment },
      "/api/onboarding/session": { ok: true, session: saved },
      "/api/onboarding/pause": { ok: true, session: paused },
    });
    render(<App />); const dialog = await screen.findByRole("dialog", { name: "开始" });
    const pauseButton = within(dialog).getAllByRole("button", { name: "稍后继续" })[0]; await waitFor(() => expect(pauseButton).toBeEnabled()); fireEvent.keyDown(document, { key: "Escape" });
    const pausedCard = (await screen.findByText("首次设置尚未完成")).closest("article"); expect(pausedCard).not.toBeNull();
    const resume = within(pausedCard!).getByRole("button", { name: "继续首次设置" }); await waitFor(() => expect(resume).toHaveFocus());
  });

  it("uses frozen navigation order and routes deep links with browser history", async () => {
    installFetchMock(); render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getAllByRole("link").map((link) => link.textContent)).toEqual(["工作室", "项目", "成片", "资源", "设置"]);
    fireEvent.click(within(navigation).getByRole("link", { name: "项目" }));
    expect(await screen.findByRole("heading", { name: "项目" })).toBeVisible();
    expect(window.location.pathname).toBe("/projects");
    window.history.back();
    await waitFor(() => expect(window.location.pathname).toBe("/studio"));
  });

  it("sorts projects by action priority instead of backend order", async () => {
    installFetchMock({ "/api/projects": { ok: true, projects: [PROJECT, { ...PROJECT, project_id: "blocked", name: "受阻项目", main_status: "blocked", updated_at: "2026-08-19T01:00:00Z" }] } });
    route("/projects"); render(<App />);
    await screen.findByText("受阻项目");
    expect(document.querySelectorAll(".project-row")[0]).toHaveTextContent("受阻项目");
  });

  it("persists the four-step draft under the frozen localStorage key", async () => {
    installFetchMock(); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /新建项目/ }));
    fireEvent.change(await screen.findByLabelText(/项目名称/), { target: { value: "可恢复草稿" } });
    await waitFor(() => expect(JSON.parse(localStorage.getItem("venus.project-draft.v1") ?? "{}").name).toBe("可恢复草稿"));
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.click(screen.getByRole("button", { name: /新建项目/ }));
    expect(await screen.findByLabelText(/项目名称/)).toHaveValue("可恢复草稿");
  });

  it("separates fatal, blocker and warning validation semantics", async () => {
    installFetchMock({ "/api/projects/validate": { ok: true, valid: false, fatal: [{ field: "name", code: "fatal", message: "名称非法" }], blockers: [{ field: "resources.asr_ref", code: "blocked", message: "ASR 不可用" }], warnings: [{ field: "source.directory", code: "shared", message: "目录已共享" }], normalized_config: null } });
    render(<App />); const dialog = await openCreateWizard();
    expect(within(dialog).getByText("必须修正")).toBeVisible();
    expect(within(dialog).getByText("启用前需处理")).toBeVisible();
    expect(within(dialog).getByText("提醒")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "创建并启用" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "保存为未启用" })).toBeDisabled();
  });

  it("allows blockers to be saved inactive but keeps activation disabled", async () => {
    installFetchMock({ "/api/projects/validate": { ok: true, valid: false, fatal: [], blockers: [{ field: "resources.asr_ref", code: "blocked", message: "ASR 不可用" }], warnings: [], normalized_config: PROJECT.config.config } });
    render(<App />); const dialog = await openCreateWizard();
    expect(within(dialog).getByRole("button", { name: "保存为未启用" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "创建并启用" })).toBeDisabled();
  });

  it("blocks project creation while the backend remains in legacy data mode", async () => {
    installFetchMock({ "/api/project-form-options": { ok: true, data_mode: "legacy", resources: [], first_scan_modes: ["new_only"], lookback_days: [3, 7, 30], schedule_modes: ["daily", "interval"], interval_minutes: [30, 60, 180, 360, 720], intermediate_retention: ["keep"], timezone: "Asia/Tokyo", defaults: { first_scan_mode: "new_only", lookback_days: null, schedule_enabled: false, schedule_mode: "daily", daily_time: "22:00", intermediate_retention: "keep" } } });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: /新建项目/ }));
    expect(await screen.findByText(/旧版数据尚未完成迁移确认/)).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
  });

  it("reuses one request_id when a create response fails and the user retries", async () => {
    let attempts = 0;
    const calls = installFetchMock({ "/api/projects": (options?: RequestInit) => { if (options?.method !== "POST") return jsonResponse({ ok: true, projects: [PROJECT] }); attempts += 1; return attempts === 1 ? jsonResponse({ ok: false, error: { code: "temporary", message: "暂时失败", fields: {} } }, 500) : jsonResponse({ ok: true, project: PROJECT, initial_scan: null }, 201); } });
    render(<App />); const dialog = await openCreateWizard();
    fireEvent.click(within(dialog).getByRole("button", { name: "创建并启用" }));
    expect(await within(dialog).findByText("暂时失败")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "创建并启用" }));
    await waitFor(() => expect(attempts).toBe(2));
    const bodies = calls.filter(([path, options]) => path === "/api/projects" && options?.method === "POST").map(([, options]) => JSON.parse(String(options?.body)));
    expect(bodies[0].request_id).toBe(bodies[1].request_id);
    expect(bodies[1].project.config).toMatchObject({ schema_version: 2, resources: { review_ref: "analysis.main" }, processing: { review_strategy: "ai_auto" } });
  });

  it("locks the manual scan button and prevents duplicate writes", async () => {
    let finish!: () => void; const pending = new Promise<Response>((resolve) => { finish = () => resolve(new Response(JSON.stringify({ ok: true, scan: { scan_id: "scan-1", project_id: "project-1", status: "success", created_count: 0 } }), { status: 200, headers: { "Content-Type": "application/json" } })); });
    const calls = installFetchMock({ "/api/projects/project-1/scans": () => pending }); route("/projects/project-1"); render(<App />);
    const button = await screen.findByRole("button", { name: "手动扫描" }); fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "扫描中…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "扫描中…" }));
    expect(calls.filter(([path]) => path === "/api/projects/project-1/scans")).toHaveLength(1); finish();
  });

  it("submits only selected and selectable source files", async () => {
    const chooseProject = { ...PROJECT, config: { ...PROJECT.config, config: { ...PROJECT.config.config, source: { ...PROJECT.config.config.source, first_scan_mode: "choose_existing" } } } };
    const calls = installFetchMock({ "/api/projects/project-1": { ok: true, project: chooseProject }, "/api/projects/project-1/source-files": { ok: true, files: [{ relative_path: "ready.mkv", bytes: 10, modified_at: "2026-08-20T01:00:00Z", selectable: true, reason: null }, { relative_path: "writing.mkv", bytes: 10, modified_at: "2026-08-20T01:00:00Z", selectable: false, reason: "仍在写入" }] }, "/api/projects/project-1/scans": { ok: true, scan: { scan_id: "scan-selected", project_id: "project-1", status: "success", created_count: 1 } } });
    route("/projects/project-1?dialog=choose-recordings&projectId=project-1"); render(<App />);
    const ready = await screen.findByRole("checkbox", { name: /ready.mkv/ }); const writing = screen.getByRole("checkbox", { name: /writing.mkv/ });
    expect(writing).toBeDisabled(); fireEvent.click(ready); fireEvent.click(screen.getByRole("button", { name: "扫描所选录像" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/projects/project-1/scans" && String(options?.body).includes("ready.mkv"))).toBe(true));
    const body = JSON.parse(String(calls.find(([path, options]) => path === "/api/projects/project-1/scans" && options?.method === "POST")?.[1]?.body));
    expect(body.selected_relative_paths).toEqual(["ready.mkv"]);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "选择已有录像" })).not.toBeInTheDocument());
    expect(window.location.pathname).toBe("/projects/project-1");
    expect(window.location.search).toBe("");
  });

  it("loads the newly created project when navigation reuses the detail route", async () => {
    const created = { ...PROJECT, project_id: "project-2", name: "新项目 B", description: "新项目详情", activation_state: "inactive", main_status: "inactive", workload: { processing: 0, queued: 0, failed: 0, completed: 0, new_results: 0 } };
    installFetchMock({
      "/api/projects": (options?: RequestInit) => options?.method === "POST" ? jsonResponse({ ok: true, project: created, initial_scan: null }, 201) : jsonResponse({ ok: true, projects: [PROJECT] }),
      "/api/projects/project-2": { ok: true, project: created },
      "/api/projects/project-2/runs": { ok: true, runs: [], cursor: null, has_more: false },
    });
    route("/projects/project-1"); render(<App />);
    expect(await screen.findByRole("heading", { name: PROJECT.name })).toBeVisible();
    const dialog = await openCreateWizard();
    fireEvent.click(within(dialog).getByRole("button", { name: "保存为未启用" }));
    expect(await screen.findByRole("heading", { name: "新项目 B" })).toBeVisible();
    expect(window.location.pathname).toBe("/projects/project-2");
    expect(screen.queryByRole("heading", { name: PROJECT.name })).not.toBeInTheDocument();
  });

  it("preserves global settings and exposes project resources read-only", async () => {
    installFetchMock(); route("/settings"); const view = render(<App />);
    expect(await screen.findByRole("heading", { name: "设置" })).toBeVisible();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeVisible();
    expect(screen.getByLabelText("录播文件夹")).toBeVisible();
    view.unmount();
    route("/resources"); render(<App />);
    expect(await screen.findByRole("heading", { name: "资源" })).toBeVisible();
    expect(screen.getByText("本地 ASR")).toBeVisible();
    expect(screen.getByText("主分析模型")).toBeVisible();
    expect(screen.getByRole("link", { name: "前往设置" })).toHaveAttribute("href", "/settings");
    expect(screen.queryByRole("button", { name: /删除|新增|编辑/ })).not.toBeInTheDocument();
  });

  it("shows the current unseen result count in the top navigation", async () => {
    installFetchMock({ "/api/studio": { ok: true, through_event_id: 0, changes: [], unseen_result_count: 3, legacy_awaiting_review_count: 0, workload: PROJECT.workload, unattended_changes: { created: [], completed: [], failed: [] }, needs_attention: { failed_runs: [], blocked_project_ids: [], issue_groups: [] }, in_progress: { processing: [], queued: [] }, recent_results: [], project_health: [PROJECT], projects: [PROJECT] } });
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(await within(navigation).findByLabelText("3 条新成片")).toHaveTextContent("3");
  });

  it("requires an explicit explanation and confirmation before pausing", async () => {
    let paused = false;
    const calls = installFetchMock({
      "/api/projects/project-1": () => jsonResponse({ ok: true, project: paused ? { ...PROJECT, activation_state: "paused", main_status: "paused" } : PROJECT }),
      "/api/projects/project-1/pause": (options?: RequestInit) => { paused = options?.method === "POST"; return jsonResponse({ ok: true, project: { ...PROJECT, activation_state: "paused", main_status: "paused" }, initial_scan: null }); },
    });
    route("/projects/project-1"); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "暂停项目" }));
    const dialog = await screen.findByRole("alertdialog", { name: "暂停项目" });
    expect(within(dialog).getByText(/已有工作会继续，手动扫描仍可用/)).toBeVisible();
    expect(calls.filter(([path]) => path === "/api/projects/project-1/pause")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认暂停" }));
    await waitFor(() => expect(calls.filter(([path]) => path === "/api/projects/project-1/pause")).toHaveLength(1));
    expect(await screen.findByRole("button", { name: "恢复项目" })).toBeVisible();
  });

  it("refreshes immediately when a hidden page becomes visible", async () => {
    const originalHidden = document.hidden; let projectLoads = 0;
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    try {
      installFetchMock({ "/api/projects": () => { projectLoads += 1; return jsonResponse({ ok: true, projects: [PROJECT] }); } });
      route("/projects"); render(<App />);
      expect(await screen.findByText(PROJECT.name)).toBeVisible();
      expect(projectLoads).toBe(1);
      Object.defineProperty(document, "hidden", { configurable: true, value: false });
      document.dispatchEvent(new Event("visibilitychange"));
      await waitFor(() => expect(projectLoads).toBe(2));
    } finally {
      Object.defineProperty(document, "hidden", { configurable: true, value: originalHidden });
    }
  });

  it("keeps the last successful project list visible after refresh failure", async () => {
    let count = 0; installFetchMock({ "/api/projects": () => { count += 1; return count === 1 ? jsonResponse({ ok: true, projects: [PROJECT] }) : jsonResponse({ ok: false, error: { code: "offline", message: "列表刷新失败", fields: {} } }, 500); } });
    route("/projects"); render(<App />); expect(await screen.findByText("游戏直播高光")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText(/刷新失败：列表刷新失败/)).toBeVisible();
    expect(screen.getByText("游戏直播高光")).toBeVisible();
  });

  it("renders a run deep link and the frozen six-stage rail", async () => {
    installFetchMock({ "/api/runs/run-1": { ok: true, run: RUN, stage_events: [] } }); route("/projects/project-1/runs/run-1"); render(<App />);
    expect(await screen.findByRole("heading", { name: "night.mkv" })).toBeVisible();
    const rail = screen.getByRole("region", { name: "处理阶段" });
    expect(within(rail).getAllByText(/读取录像|语音转写|内容分析|结果仲裁|AI 审阅|渲染成片/)).toHaveLength(6);
  });
});
