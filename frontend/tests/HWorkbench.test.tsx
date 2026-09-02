import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { PROJECT, RUN, installFetchMock, jsonResponse } from "./helpers";

const SUMMARY = {
  run_id: "run-result", project: { project_id: "project-1", name: "游戏直播高光" }, source_name: "final-night.mkv",
  result_type: "clips_ready", result_revision: 3, seen: false, overall_summary: "两条高光已经完成",
  available_output_count: 2, failed_output_count: 0, total_duration_ms: 8000, primary_output_id: "output-1",
  completed_at: "2026-08-27T03:00:00Z", issue_summary: null,
};
const OUTPUTS = [1, 2].map((value) => ({
  output_id: `output-${value}`, run_id: "run-result", project_id: "project-1", candidate_id: `candidate-${value}`,
  status: "ready", display_order: value, file_name: `clip-${value}.mp4`, duration_ms: 4000, width: 1920, height: 1080,
  container: "mp4", video_codec: "h264", byte_size: 1024, generated_at: "2026-08-27T03:00:00Z", verified_at: "2026-08-27T03:00:00Z",
  available: true, media_url: `/api/outputs/output-${value}/media`, material: { material_id: `material-${value}`, status: "ready", material_revision: 1, preferred_title_id: `title-${value}`, saved_at: null }, active_issue_summary: null,
}));
const RESULT = {
  ok: true,
  result: { run_id: "run-result", review_session_id: "review-1", result_type: "clips_ready", candidate_count: 2, selected_count: 2, rejected_count: 0, available_output_count: 2, failed_output_count: 0, total_duration_ms: 8000, overall_summary: "两条高光已经完成", warnings: [], format_version: 1, result_revision: 3, seen: false, seen_at: null, source_kind: "ai_review", completed_at: "2026-08-27T03:00:00Z", updated_at: "2026-08-27T03:00:00Z" },
  review_session: null, decisions: [], outputs: OUTPUTS, issues: [], available_actions: ["mark_seen"],
};
const RESULT_RUN = { ...RUN, run_id: "run-result", source_name: "final-night.mkv", status: "completed", current_stage: "render", completed_at: "2026-08-27T03:00:00Z", has_result: true, result_summary: SUMMARY };

function route(path: string) { window.history.replaceState({}, "", path); }
function resultMocks(overrides: Record<string, unknown> = {}) {
  return installFetchMock({
    "/api/runs/run-result": { ok: true, run: RESULT_RUN, stage_events: [] },
    "/api/runs/run-result/result": RESULT,
    "/api/runs/run-result/result/seen": { ok: true, result: { ...RESULT.result, seen: true, seen_at: "2026-08-27T03:01:00Z" }, unseen_result_count: 0, reused: false },
    "/api/outputs/output-1": { ok: true, output: { ...OUTPUTS[0], display_path: "/output/clip-1.mp4" } },
    "/api/outputs/output-2": { ok: true, output: { ...OUTPUTS[1], display_path: "/output/clip-2.mp4" } },
    "/api/outputs/output-1/material": { ok: true, material: material(1) },
    "/api/outputs/output-2/material": { ok: true, material: material(2) },
    ...overrides,
  });
}
function material(value: number) { return { material_id: `material-${value}`, output_id: `output-${value}`, status: "ready", material_revision: 1, titles: [{ title_id: `title-${value}`, text: `高光标题 ${value}` }], preferred_title_id: `title-${value}`, description: `发布描述 ${value}`, tags: ["直播", "高光"], generated_from: "ai_review", saved_at: null, active_issue_summary: null }; }

describe("Venus 1.0 result workbench", () => {
  beforeEach(() => { route("/studio"); delete window.liveClipperShell; });

  it("lists unseen results without marking them seen", async () => {
    const calls = installFetchMock({ "/api/clips": { ok: true, view: "new", unseen_result_count: 1, results: [SUMMARY], cursor: null, has_more: false } });
    route("/clips?view=new"); render(<App />);
    expect(await screen.findByText("final-night.mkv")).toBeVisible();
    expect(screen.getByText("两条高光已经完成")).toBeVisible();
    expect(calls.some(([path]) => path.includes("/result/seen"))).toBe(false);
  });

  it("renders native output playback and marks the rendered result seen once", async () => {
    const calls = resultMocks(); route("/projects/project-1/runs/run-result?view=result"); render(<App />);
    expect(await screen.findByRole("heading", { name: "两条高光已经完成" })).toBeVisible();
    const video = document.querySelector("video");
    expect(video).toHaveAttribute("src", "/api/outputs/output-1/media");
    await waitFor(() => expect(calls.filter(([path, options]) => path === "/api/runs/run-result/result/seen" && options?.method === "POST")).toHaveLength(1));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/runs/run-result/result/seen")?.[1]?.body));
    expect(body.expected_result_revision).toBe(3);
  });

  it("normalizes unknown result view and renders a no-clip conclusion", async () => {
    const noClip = { ...RESULT, result: { ...RESULT.result, result_type: "no_clip", selected_count: 0, available_output_count: 0, total_duration_ms: 0, overall_summary: "没有达到发布标准" }, outputs: [], decisions: [{ decision_id: "decision-1", candidate_id: "candidate-1", decision: "rejected", rank: null, candidate_type: "summary", source_start_ms: 0, source_end_ms: 1000, selected_start_ms: null, selected_end_ms: null, remove_ranges: [], hook: null, core_value: null, reason: "信息不完整", rejection_reason_code: "insufficient_context", risks: [], transcript_excerpt: "片段内容", output_id: null }] };
    resultMocks({ "/api/runs/run-result/result": noClip }); route("/projects/project-1/runs/run-result?view=unknown"); render(<App />);
    expect(await screen.findByText("本次没有适合生成的片段")).toBeVisible();
    await waitFor(() => expect(window.location.search).toContain("view=result"));
  });

  it("autosaves material edits with the current revision and preserves title ids", async () => {
    const calls = resultMocks({ "/api/outputs/output-1/material": (options?: RequestInit) => options?.method === "PATCH" ? jsonResponse({ ok: true, material: { ...material(1), material_revision: 2, description: "新的发布描述" }, reused: false }) : jsonResponse({ ok: true, material: material(1) }) });
    route("/projects/project-1/runs/run-result?view=materials&output=output-1"); render(<App />);
    const description = await screen.findByLabelText("描述");
    fireEvent.change(description, { target: { value: "新的发布描述" } });
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/outputs/output-1/material" && options?.method === "PATCH")).toBe(true), { timeout: 2500 });
    const body = JSON.parse(String(calls.find(([path, options]) => path === "/api/outputs/output-1/material" && options?.method === "PATCH")?.[1]?.body));
    expect(body.expected_revision).toBe(1);
    expect(body.titles).toEqual([{ title_id: "title-1", text: "高光标题 1" }]);
    expect(body.description).toBe("新的发布描述");
  });

  it("keeps the local draft on revision conflict and reapplies it against the refreshed revision", async () => {
    let reads = 0; let writes = 0;
    const server = { ...material(1), material_revision: 2, description: "服务器新描述" };
    const calls = resultMocks({ "/api/outputs/output-1/material": (options?: RequestInit) => { if (options?.method !== "PATCH") { reads += 1; return jsonResponse({ ok: true, material: reads === 1 ? material(1) : server }); } writes += 1; return writes === 1 ? jsonResponse({ ok: false, error: { code: "revision_conflict", message: "发布物料已更新", fields: {} }, current: server }, 409) : jsonResponse({ ok: true, material: { ...server, material_revision: 3, description: "我的草稿" }, reused: false }); } });
    route("/projects/project-1/runs/run-result?view=materials&output=output-1"); render(<App />);
    const description = await screen.findByLabelText("描述"); fireEvent.change(description, { target: { value: "我的草稿" } });
    expect(await screen.findByText(/你的草稿仍保留/)).toBeVisible();
    expect(description).toHaveValue("我的草稿");
    fireEvent.click(screen.getByRole("button", { name: "查看已保存版本" }));
    expect(screen.getByText("服务器新描述")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "立即保存" }));
    await waitFor(() => expect(writes).toBe(2));
    const patchBodies = calls.filter(([path, options]) => path === "/api/outputs/output-1/material" && options?.method === "PATCH").map(([, options]) => JSON.parse(String(options?.body)));
    expect(patchBodies[1]).toMatchObject({ expected_revision: 2, description: "我的草稿" });
  });

  it("copies the frozen full-material format through the formal clipboard path", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    resultMocks(); route("/projects/project-1/runs/run-result?view=materials&output=output-1"); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "复制全部物料" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("高光标题 1\n\n发布描述 1\n\n#直播 #高光"));
    expect(screen.getByText("已复制全部物料")).toBeVisible();
  });

  it("keeps a failed output visible without presenting it as playable", async () => {
    const failed = { ...OUTPUTS[1], status: "failed", available: false, media_url: null, active_issue_summary: { issue_id: "issue-output", issue_code: "render_failed", group_key: "render", status: "action_required", impact_level: "local", title: "这条成片渲染失败", summary: "另一条成片仍可使用", next_step: "重试这条成片", issue_revision: 1, available_actions: ["recheck"] } };
    resultMocks({ "/api/runs/run-result/result": { ...RESULT, result: { ...RESULT.result, result_type: "partial", failed_output_count: 1, available_output_count: 1 }, outputs: [OUTPUTS[0], failed] } });
    route("/projects/project-1/runs/run-result?view=result&output=output-2"); render(<App />);
    expect(await screen.findByText("另一条成片仍可使用")).toBeVisible();
    expect(screen.queryByLabelText("播放 clip-2.mp4")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /成片 2/ })).toHaveTextContent("当前不可用");
  });

  it("renders backend-authorized issue actions and submits revision-bound recheck", async () => {
    const issueSummary = { issue_id: "issue-1", issue_code: "output_unwritable", group_key: "output", status: "action_required", impact_level: "blocking", title: "输出目录不可写", summary: "无法继续渲染", next_step: "修复后重新检查", issue_revision: 4, available_actions: ["recheck", "select_recovery_output", "copy_diagnostic"] };
    const issue = { ...issueSummary, category: "storage", scope: { type: "run", project_id: "project-1", run_id: "run-result", output_id: null, material_id: null }, impact: "渲染暂停", preserved_content: "审阅结果已保留", safe_checkpoint: "review", reuse_stages: ["read_source", "transcribe", "analyze", "arbitrate", "review"], redo_stages: ["render"], automatic_attempt_count: 0, total_attempt_count: 1, next_retry_at: null, retry_exhausted: false, diagnostic: { diagnostic_id: "diag-1", summary: "permission denied" }, occurred_at: "2026-08-27T03:00:00Z", updated_at: "2026-08-27T03:00:00Z", resolved_at: null, events: [] };
    const calls = resultMocks({ "/api/runs/run-result/result": { ...RESULT, issues: [issueSummary] }, "/api/issues/issue-1": { ok: true, issue }, "/api/issues/issue-1/recheck": { ok: true, issue, reused: false } });
    route("/projects/project-1/runs/run-result?view=result&issue=issue-1"); render(<App />);
    const drawer = await screen.findByRole("dialog", { name: "问题详情" });
    expect(await within(drawer).findByText("审阅结果已保留")).toBeVisible();
    fireEvent.click(within(drawer).getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/issues/issue-1/recheck" && options?.method === "POST")).toBe(true));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/issues/issue-1/recheck")?.[1]?.body));
    expect(body.expected_issue_revision).toBe(4);
  });

  it("uses only a desktop selection token when replacing a missing source", async () => {
    const issueSummary = { issue_id: "issue-source", issue_code: "source_missing", group_key: "source", status: "action_required", impact_level: "blocking", title: "原录像已移动", summary: "需要重新选择原录像", next_step: "选择同一录像", issue_revision: 2, available_actions: ["select_source"] };
    const issue = { ...issueSummary, category: "source", scope: { type: "run", project_id: "project-1", run_id: "run-result", output_id: null, material_id: null }, impact: "处理暂停", preserved_content: "已完成步骤已保留", safe_checkpoint: "analyze", reuse_stages: ["read_source", "transcribe", "analyze"], redo_stages: ["review", "render"], automatic_attempt_count: 0, total_attempt_count: 1, next_retry_at: null, retry_exhausted: false, diagnostic: { diagnostic_id: null, summary: null }, occurred_at: "2026-08-27T03:00:00Z", updated_at: "2026-08-27T03:00:00Z", resolved_at: null, events: [] };
    window.liveClipperShell = { selectIssueSource: vi.fn(() => Promise.resolve({ selectionToken: "one-time-token", expiresAt: "2026-08-27T03:05:00Z" })) };
    const calls = resultMocks({ "/api/runs/run-result/result": { ...RESULT, issues: [issueSummary] }, "/api/issues/issue-source": { ok: true, issue }, "/api/issues/issue-source/source": { ok: true, issue, reused: false } });
    route("/projects/project-1/runs/run-result?view=result&issue=issue-source"); render(<App />);
    const drawer = await screen.findByRole("dialog", { name: "问题详情" });
    fireEvent.click(await within(drawer).findByRole("button", { name: "重新选择原录像" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/issues/issue-source/source")).toBe(true));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/issues/issue-source/source")?.[1]?.body));
    expect(body.selection_token).toBe("one-time-token");
    expect(JSON.stringify(body)).not.toContain("selected_path");
  });

  it("repairs an inline AI connection, tests it, then rechecks the issue", async () => {
    const issueSummary = { issue_id: "issue-ai", issue_code: "ai_resource_unavailable", group_key: "ai", status: "action_required", impact_level: "blocking", title: "AI 审阅资源不可用", summary: "连接失败", next_step: "修复连接后重新检查", issue_revision: 5, available_actions: ["open_resource_repair", "recheck"] };
    const issue = { ...issueSummary, category: "resource", scope: { type: "run", project_id: "project-1", run_id: "run-result", output_id: null, material_id: null }, impact: "AI 审阅暂停", preserved_content: "候选与转写已保留", safe_checkpoint: "arbitrate", reuse_stages: ["read_source", "transcribe", "analyze", "arbitrate"], redo_stages: ["review", "render"], automatic_attempt_count: 2, total_attempt_count: 2, next_retry_at: null, retry_exhausted: true, diagnostic: { diagnostic_id: "diag-ai", summary: "连接不可用" }, occurred_at: "2026-08-27T03:00:00Z", updated_at: "2026-08-27T03:00:00Z", resolved_at: null, events: [] };
    const ready = { ...issue, status: "ready_to_recover", issue_revision: 6, available_actions: ["continue_run"] };
    const calls = resultMocks({ "/api/runs/run-result/result": { ...RESULT, issues: [issueSummary] }, "/api/issues/issue-ai": { ok: true, issue }, "/api/resources/analysis.main/repair-context": { ok: true, repair_context: { resource_id: "analysis.main", display_name: "AI 审阅资源", resource_type: "analysis", api_base: "https://api.example.com/v1", model: "review-model", credential_state: "missing", repair_capability: "inline_connection", settings_url: "/settings", issue_id: "issue-ai" } }, "/api/resources/analysis.main/connection": { ok: true, resource_id: "analysis.main", api_base: "https://api.example.com/v1", model: "review-model", credential_updated: true, reused: false }, "/api/resources/analysis.main/connection-test": { ok: true, resource_id: "analysis.main", success: true, tested_at: "2026-08-27T03:02:00Z", reused: false }, "/api/issues/issue-ai/recheck": { ok: true, issue: ready, reused: false } });
    route("/projects/project-1/runs/run-result?view=result&issue=issue-ai"); render(<App />);
    const drawer = await screen.findByRole("dialog", { name: "问题详情" }); fireEvent.click(await within(drawer).findByRole("button", { name: "修复资源连接" }));
    fireEvent.change(await screen.findByLabelText("API Key（留空表示不更新）"), { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存、测试并重新检查" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/resources/analysis.main/connection-test")).toBe(true));
    const connectionBody = JSON.parse(String(calls.find(([path]) => path === "/api/resources/analysis.main/connection")?.[1]?.body));
    expect(connectionBody).toMatchObject({ issue_id: "issue-ai", api_key: "new-secret" });
    expect(calls.some(([path]) => path === "/api/issues/issue-ai/recheck")).toBe(true);
  });
});
