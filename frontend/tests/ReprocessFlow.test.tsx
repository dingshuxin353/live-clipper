import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { PROJECT, RUN, installFetchMock, jsonResponse } from "./helpers";

const ORIGIN = {
  ...RUN,
  run_id: "run-origin",
  source_name: "final-night.mkv",
  status: "completed" as const,
  current_stage: "render" as const,
  processing_sequence: 1,
  completed_at: "2026-09-01T02:00:00Z",
};
const CURRENT = { asr: "asr.local", analysis: "analysis.main", ai_review: "analysis.main", render: "current_renderer", naming: "system_safe", output_directory: "/outputs/new", retention: "keep" };
const OLD = { ...CURRENT, analysis: "analysis.old", output_directory: "/outputs/old", retention: "remind_after_7_days" };
const PREFLIGHT = {
  ok: true,
  run: { run_id: ORIGIN.run_id, project_id: PROJECT.project_id, status: ORIGIN.status, processing_sequence: 1 },
  source: { path: "/recordings/final-night.mkv", name: "final-night.mkv", expected_content_id: "content-1", content_id: "content-1", bytes: 4096, mtime_ns: 1, state: "ready" },
  current_settings: { config_revision: 2, summary: CURRENT, snapshot: {} },
  changes: [
    { field: "analysis", before: "analysis.old", after: "analysis.main" },
    { field: "output_directory", before: "/outputs/old", after: "/outputs/new" },
  ],
  space: { work_directory: "/work", required_bytes: 4096, available_bytes: 8192, additional_estimate_bytes: null, sufficient: true },
  active_run: null,
  next_processing_sequence: 2,
  blockers: [],
  can_reprocess: true,
  preflight_revision: "revision-1",
};
const VERSIONS = {
  ok: true,
  run_id: "run-origin",
  project_id: PROJECT.project_id,
  content_id: "content-1",
  versions: [
    { run_id: "run-origin", status: "completed", current_stage: "render", processing_sequence: 1, origin_run_id: null, config_revision: 1, queued_at: "2026-09-01T01:00:00Z", started_at: "2026-09-01T01:01:00Z", review_at: null, completed_at: "2026-09-01T02:00:00Z", updated_at: "2026-09-01T02:00:00Z", error_code: null, settings_summary: OLD, result_summary: { result_type: "clips_ready", selected_count: 1, available_output_count: 1, failed_output_count: 0, total_duration_ms: 5000, result_revision: 1, completed_at: "2026-09-01T02:00:00Z" }, changed_fields: [] },
    { run_id: "run-second", status: "failed", current_stage: "review", processing_sequence: 2, origin_run_id: "run-origin", config_revision: 2, queued_at: "2026-09-01T03:00:00Z", started_at: "2026-09-01T03:01:00Z", review_at: null, completed_at: "2026-09-01T03:30:00Z", updated_at: "2026-09-01T03:30:00Z", error_code: "ai_review_failed", settings_summary: CURRENT, result_summary: null, changed_fields: ["analysis", "output_directory", "retention", "result_summary"] },
  ],
};

function route(path: string) { window.history.replaceState({}, "", path); }
function mocks(overrides: Record<string, unknown> = {}) {
  return installFetchMock({
    "/api/runs/run-origin": { ok: true, run: ORIGIN, stage_events: [] },
    "/api/runs/run-origin/reprocess-preflight": PREFLIGHT,
    "/api/runs/run-origin/versions": VERSIONS,
    ...overrides,
  });
}

describe("Spec T reprocess and version flow", () => {
  beforeEach(() => route("/projects/project-1/runs/run-origin"));

  it("shows the terminal entry and submits the frozen two-field contract once", async () => {
    const calls = mocks({
      "/api/runs/run-origin/reprocess": { ok: true, run: { ...VERSIONS.versions[1], run_id: "run-new", status: "queued" }, created: true, reuse_reason: null },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    const dialog = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    expect(dialog).toHaveTextContent("将使用当前设置创建新的处理记录。原有记录和成片不会被修改。");
    for (const phase of ["读取录像", "语音转写", "内容分析", "结果仲裁", "AI 审阅", "渲染成片"]) expect(dialog).toHaveTextContent(phase);
    expect(dialog).toHaveTextContent("不会修改原文件；处理时会在受控工作目录创建临时工作副本。");
    const start = within(dialog).getByRole("button", { name: "开始重新处理" });
    fireEvent.click(start);
    await waitFor(() => expect(calls.filter(([path]) => path === "/api/runs/run-origin/reprocess")).toHaveLength(1));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/runs/run-origin/reprocess")?.[1]?.body));
    expect(body).toEqual({ request_id: expect.stringMatching(/^run-reprocess-/), expected_preflight_revision: "revision-1" });
    expect(window.location.pathname).toBe("/projects/project-1/runs/run-new");
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(calls.filter(([path]) => path === "/api/runs/run-origin/reprocess")).toHaveLength(1);
  });

  it("keeps M8 recovery primary and M9 separate while hiding reprocess for active runs", async () => {
    const issue = { issue_id: "issue-1", issue_code: "ai_review_failed", group_key: "review", status: "ready_to_recover", impact_level: "blocking", title: "可以继续", summary: "从检查点继续", next_step: "继续处理", issue_revision: 2, available_actions: ["continue_run"] };
    const failed = { ...ORIGIN, status: "failed", active_issue_summary: issue };
    mocks({ "/api/runs/run-origin": { ok: true, run: failed, stage_events: [] } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("button", { name: "继续处理" })).toHaveClass("primary");
    expect(screen.getByRole("button", { name: "按当前设置重新处理" })).toBeVisible();
    unmount();
    route("/projects/project-1/runs/run-origin");
    mocks({ "/api/runs/run-origin": { ok: true, run: { ...ORIGIN, status: "processing", current_stage: "analyze" }, stage_events: [] } });
    render(<App />);
    await screen.findByText("内容分析");
    expect(screen.queryByRole("button", { name: /重新处理/ })).not.toBeInTheDocument();
  });

  it("navigates directly to an active run and exposes blocker-authorized repair actions", async () => {
    const active = { ...VERSIONS.versions[1], run_id: "run-active", status: "processing" };
    const { unmount } = render(<App />);
    unmount();
    mocks({ "/api/runs/run-origin/reprocess-preflight": { ...PREFLIGHT, active_run: active } });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    await waitFor(() => expect(window.location.pathname).toBe("/projects/project-1/runs/run-active"));

    route("/projects/project-1/runs/run-origin");
    mocks({ "/api/runs/run-origin/reprocess-preflight": { ...PREFLIGHT, can_reprocess: false, blockers: [{ code: "resource_unavailable", action: "ai_settings", related_id: "analysis.main" }] } });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    const blocked = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    expect(within(blocked).getByRole("button", { name: "打开 AI 设置" })).toBeVisible();
    expect(within(blocked).getByRole("button", { name: "开始重新处理" })).toBeDisabled();
  });

  it("reuses the origin-run request id when the create result is unknown", async () => {
    let attempts = 0;
    const calls = mocks({
      "/api/runs/run-origin/reprocess": () => {
        attempts += 1;
        return attempts === 1 ? Promise.reject(new Error("connection lost")) : jsonResponse({ ok: true, run: { ...VERSIONS.versions[1], run_id: "run-recovered", status: "queued" }, created: false, reuse_reason: "idempotent_request" });
      },
    });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    const dialog = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    fireEvent.click(within(dialog).getByRole("button", { name: "开始重新处理" }));
    expect(await within(dialog).findByText(/结果暂时无法确认/)).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始重新处理" }));
    await waitFor(() => expect(window.location.pathname).toBe("/projects/project-1/runs/run-recovered"));
    const ids = calls.filter(([path]) => path === "/api/runs/run-origin/reprocess").map(([, options]) => JSON.parse(String(options?.body)).request_id);
    expect(ids).toHaveLength(2); expect(new Set(ids).size).toBe(1);
  });

  it("refreshes a changed preflight and requires an explicit second confirmation", async () => {
    let reads = 0; let creates = 0;
    const calls = mocks({
      "/api/runs/run-origin/reprocess-preflight": () => { reads += 1; return jsonResponse({ ...PREFLIGHT, preflight_revision: reads === 1 ? "revision-1" : "revision-2", next_processing_sequence: reads === 1 ? 2 : 3 }); },
      "/api/runs/run-origin/reprocess": () => { creates += 1; return creates === 1 ? jsonResponse({ ok: false, error: { code: "preflight_changed", message: "预检已变化", fields: {} } }, 409) : jsonResponse({ ok: true, run: { ...VERSIONS.versions[1], run_id: "run-after-confirm", status: "queued" }, created: true, reuse_reason: null }); },
    });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    let dialog = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    fireEvent.click(within(dialog).getByRole("button", { name: "开始重新处理" }));
    expect(await within(dialog).findByRole("button", { name: "确认最新检查结果" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "开始重新处理" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认最新检查结果" }));
    dialog = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    fireEvent.click(within(dialog).getByRole("button", { name: "开始重新处理" }));
    await waitFor(() => expect(window.location.pathname).toBe("/projects/project-1/runs/run-after-confirm"));
    const revisions = calls.filter(([path]) => path === "/api/runs/run-origin/reprocess").map(([, options]) => JSON.parse(String(options?.body)).expected_preflight_revision);
    expect(revisions).toEqual(["revision-1", "revision-2"]);
  });

  it("creates the backend source issue without passing a path", async () => {
    const summary = { issue_id: "source-issue", issue_code: "source_missing", group_key: "reprocess-source", status: "action_required", impact_level: "blocking", title: "原始录像需要重新定位", summary: "原始录像不存在", next_step: "选择同一录像", issue_revision: 1, available_actions: ["select_source"] };
    const issue = { ...summary, category: "recording", scope: { type: "run", project_id: "project-1", run_id: "run-origin", output_id: null, material_id: null }, impact: "重新处理已阻止", preserved_content: "既有版本与成片保持不变", safe_checkpoint: "read_source", reuse_stages: [], redo_stages: ["read_source"], automatic_attempt_count: 0, total_attempt_count: 0, next_retry_at: null, retry_exhausted: false, diagnostic: { diagnostic_id: null, summary: null }, occurred_at: "2026-09-01T02:00:00Z", updated_at: "2026-09-01T02:00:00Z", resolved_at: null, events: [] };
    const calls = mocks({
      "/api/runs/run-origin/reprocess-preflight": { ...PREFLIGHT, can_reprocess: false, blockers: [{ code: "source_missing", action: "source_repair", related_id: "run-origin" }] },
      "/api/runs/run-origin/reprocess-source-repair": { ok: true, issue, reused: false },
      "/api/issues/source-issue": { ok: true, issue },
    });
    render(<App />); fireEvent.click(await screen.findByRole("button", { name: "重新处理" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "重新处理这条录像" })).getByRole("button", { name: "找回原录像" }));
    expect(await screen.findByRole("dialog", { name: "问题详情" })).toBeVisible();
    const call = calls.find(([path]) => path === "/api/runs/run-origin/reprocess-source-repair");
    expect(call?.[1]?.body).toBeUndefined();
    expect(window.location.search).toContain("reprocessAfterRepair=1");
  });

  it("traps initial focus and restores it after Escape", async () => {
    mocks(); render(<App />);
    const trigger = await screen.findByRole("button", { name: "重新处理" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "重新处理这条录像" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "关闭" })).toHaveFocus());
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "重新处理这条录像" })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("uses API order for versions, navigates real runs, and compares only backend-marked fields", async () => {
    mocks(); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "初次处理" }));
    const drawer = await screen.findByRole("dialog", { name: "处理版本" });
    const rows = within(drawer).getAllByRole("link");
    expect(rows.map((row) => row.textContent)).toEqual(expect.arrayContaining([expect.stringContaining("初次处理"), expect.stringContaining("第 2 次处理")]));
    fireEvent.click(within(drawer).getByRole("button", { name: "比较第 2 次处理" }));
    const compare = await screen.findByRole("dialog", { name: "比较两次处理" });
    expect(compare).toHaveTextContent("AI 模型");
    expect(compare).toHaveTextContent("输出目录");
    expect(compare).toHaveTextContent("中间产物");
    expect(compare).toHaveTextContent("尚未产生结果");
    expect(compare).not.toHaveTextContent("命名方式");
    fireEvent.click(within(compare).getAllByRole("button", { name: "关闭" }).at(-1)!);
    fireEvent.click(await screen.findByRole("button", { name: "初次处理" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "处理版本" })).getByRole("link", { name: /第 2 次处理/ }));
    expect(window.location.pathname).toBe("/projects/project-1/runs/run-second");
  });
});
