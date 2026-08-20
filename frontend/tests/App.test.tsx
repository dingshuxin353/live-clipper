import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { PROJECT, RUN, installFetchMock, jsonResponse } from "./helpers";

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

  it("uses frozen navigation order and routes deep links with browser history", async () => {
    installFetchMock(); render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getAllByRole("link").map((link) => link.textContent)).toEqual(["工作室", "项目", "待审", "资源", "设置"]);
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
    expect(within(rail).getAllByText(/读取录像|语音转写|内容分析|结果仲裁|人工审阅|渲染成片/)).toHaveLength(6);
  });
});
