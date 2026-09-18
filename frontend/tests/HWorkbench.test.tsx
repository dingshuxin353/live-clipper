import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { PathField, StatusPill } from "../src/workbench-shared";
import type { ProjectSummary } from "../src/project-dto";
import { LegacyRunView } from "../src/RunResultPage";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { PROJECT, RUN, installFetchMock, jsonResponse } from "./helpers";

it("distinguishes scan failure and leaves readonly paths selectable without an unavailable action", () => {
  const view = render(<><StatusPill status="failed" context="scan" /><StatusPill status="failed" /><PathField label="路径" value="/isolated/full/path" isReadOnly /></>);
  expect(screen.getByText("扫描失败")).toBeVisible();
  expect(screen.getByText("处理失败")).toBeVisible();
  const path = screen.getByRole("textbox", { name: /路径/ });
  expect(path).toHaveAttribute("readonly"); expect(path).toBeEnabled();
  expect(screen.queryByText(/请点击/)).not.toBeInTheDocument();
  view.rerender(<PathField label="路径" value="/isolated/full/path" isReadOnly choose={() => undefined} />);
  expect(screen.getByRole("button", { name: "选择文件夹" })).toBeVisible();
  expect(screen.getByRole("textbox", { name: /路径/ })).toHaveAccessibleDescription("请点击「选择文件夹」更改位置。");
});

it("does not fabricate completed stages or historical configuration for an imported record", () => {
  installFetchMock();
  render(<RouterProvider router={createMemoryRouter([{ path: "*", element: <LegacyRunView run={{ ...RUN, status: "completed", current_stage: null, trigger_source: "legacy_import" }} project={PROJECT as ProjectSummary} events={[]} /> }])} />);
  expect(document.querySelectorAll(".stage-rail .done")).toHaveLength(0);
  expect(screen.getByText("原处理配置未记录")).toBeVisible();
  expect(screen.getByText("旧版导入")).toBeVisible();
});

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

  it("keeps project settings in one field order and preserves a cancelled folder choice", async () => {
    const selectFolder = vi.fn(async () => null); window.liveClipperShell = { selectFolder };
    installFetchMock(); route("/projects/project-1?dialog=project-settings"); render(<App />);
    const dialog = await screen.findByRole("dialog", { name: "项目设置" });
    const controls = [within(dialog).getByLabelText("项目名称"), within(dialog).getByLabelText("项目描述（选填）"), within(dialog).getByRole("textbox", { name: /录像文件夹/ }), within(dialog).getByRole("textbox", { name: /成片保存位置/ }), within(dialog).getByRole("combobox", { name: "临时文件清理提醒" }), within(dialog).getByRole("checkbox", { name: "定时扫描" })];
    for (let index = 1; index < controls.length; index += 1) expect(controls[index - 1].compareDocumentPosition(controls[index]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(dialog).getByLabelText("项目描述（选填）")).toHaveAttribute("rows", "4");
    expect(dialog.querySelector(".form-pair")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "选择文件夹" })[0]);
    await waitFor(() => expect(selectFolder).toHaveBeenCalledWith("选择录像文件夹"));
    expect(within(dialog).getByRole("textbox", { name: /录像文件夹/ })).toHaveValue("/recordings");
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

  it("keeps playback positions across consecutive updates and output switches", async () => {
    resultMocks(); route("/projects/project-1/runs/run-result?view=result"); render(<App />);
    const first = await screen.findByLabelText<HTMLVideoElement>("播放 clip-1.mp4");
    fireEvent.loadedMetadata(first);
    act(() => {
      first.currentTime = 1;
      fireEvent.timeUpdate(first);
      first.currentTime = 2;
      fireEvent.timeUpdate(first);
    });
    expect(screen.getByRole("heading", { name: "两条高光已经完成" })).toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: /成片 2/ }));
    const second = screen.getByLabelText<HTMLVideoElement>("播放 clip-2.mp4");
    fireEvent.loadedMetadata(second);
    expect(second.currentTime).toBe(0);
    second.currentTime = 3;
    fireEvent.timeUpdate(second);

    fireEvent.click(screen.getByRole("tab", { name: /成片 1/ }));
    const restoredFirst = screen.getByLabelText<HTMLVideoElement>("播放 clip-1.mp4");
    fireEvent.loadedMetadata(restoredFirst);
    expect(restoredFirst.currentTime).toBe(2);
    fireEvent.click(screen.getByRole("tab", { name: /成片 2/ }));
    const restoredSecond = screen.getByLabelText<HTMLVideoElement>("播放 clip-2.mp4");
    fireEvent.loadedMetadata(restoredSecond);
    expect(restoredSecond.currentTime).toBe(3);
    expect(screen.getByRole("heading", { name: "两条高光已经完成" })).toBeVisible();
  });

  it("normalizes unknown result view and renders a no-clip conclusion", async () => {
    const noClip = { ...RESULT, result: { ...RESULT.result, result_type: "no_clip", selected_count: 0, available_output_count: 0, total_duration_ms: 0, overall_summary: "没有达到发布标准" }, outputs: [], decisions: [{ decision_id: "decision-1", candidate_id: "candidate-1", decision: "rejected", rank: null, candidate_type: "summary", source_start_ms: 0, source_end_ms: 1000, selected_start_ms: null, selected_end_ms: null, remove_ranges: [], hook: null, core_value: null, reason: "信息不完整", rejection_reason_code: "insufficient_context", risks: [], transcript_excerpt: "片段内容", output_id: null }] };
    resultMocks({ "/api/runs/run-result/result": noClip }); route("/projects/project-1/runs/run-result?view=unknown"); render(<App />);
    expect((await screen.findAllByText("本次未选出适合的片段")).length).toBeGreaterThan(0);
    await waitFor(() => expect(window.location.search).toContain("view=result"));
  });

  it("autosaves material edits with the current revision and preserves title ids", async () => {
    const calls = resultMocks({ "/api/outputs/output-1/material": (options?: RequestInit) => options?.method === "PATCH" ? jsonResponse({ ok: true, material: { ...material(1), material_revision: 2, description: "新的发布描述" }, reused: false }) : jsonResponse({ ok: true, material: material(1) }) });
    route("/projects/project-1/runs/run-result?view=materials&output=output-1"); render(<App />);
    const description = await screen.findByLabelText("视频描述");
    expect(description.closest(".astryx-field")).not.toBeNull();
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
    const description = await screen.findByLabelText("视频描述"); fireEvent.change(description, { target: { value: "我的草稿" } });
    expect(await screen.findByText(/你当前的修改尚未保存/)).toBeVisible();
    expect(description).toHaveValue("我的草稿");
    fireEvent.click(screen.getByRole("button", { name: "查看最新内容" }));
    expect(screen.getByText("服务器新描述")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "保留我的修改并保存" }));
    await waitFor(() => expect(writes).toBe(2));
    const patchBodies = calls.filter(([path, options]) => path === "/api/outputs/output-1/material" && options?.method === "PATCH").map(([, options]) => JSON.parse(String(options?.body)));
    expect(patchBodies[1]).toMatchObject({ expected_revision: 2, description: "我的草稿" });
  });

  it("copies the frozen full-material format through the formal clipboard path", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    resultMocks(); route("/projects/project-1/runs/run-result?view=materials&output=output-1"); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "复制全部文案" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("高光标题 1\n\n发布描述 1\n\n#直播 #高光"));
    expect(screen.getByText("已复制全部文案")).toBeVisible();
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
    const issueSummary = { issue_id: "issue-1", issue_code: "output_unwritable", group_key: "output", status: "action_required", impact_level: "blocking", title: "成片保存位置不可写", summary: "无法继续渲染", next_step: "修复后重新检查", issue_revision: 4, available_actions: ["recheck", "select_recovery_output", "copy_diagnostic"] };
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
    fireEvent.click(await within(drawer).findByRole("button", { name: "重新选择原始录像" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/issues/issue-source/source")).toBe(true));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/issues/issue-source/source")?.[1]?.body));
    expect(body.selection_token).toBe("one-time-token");
    expect(JSON.stringify(body)).not.toContain("selected_path");
  });

  it("repairs the frozen review resource after the project has switched models", async () => {
    const issueSummary = { issue_id: "issue-ai", issue_code: "ai_resource_unavailable", group_key: "ai", status: "action_required", impact_level: "blocking", title: "AI 审阅资源不可用", summary: "连接失败", next_step: "修复连接后重新检查", issue_revision: 5, available_actions: ["open_resource_repair", "recheck"] };
    const issue = { ...issueSummary, repair_resource_id: "original.review", category: "resource", scope: { type: "run", project_id: "project-1", run_id: "run-result", output_id: null, material_id: null }, impact: "AI 审阅暂停", preserved_content: "候选与转写已保留", safe_checkpoint: "arbitrate", reuse_stages: ["read_source", "transcribe", "analyze", "arbitrate"], redo_stages: ["review", "render"], automatic_attempt_count: 2, total_attempt_count: 2, next_retry_at: null, retry_exhausted: true, diagnostic: { diagnostic_id: "diag-ai", summary: "连接不可用" }, occurred_at: "2026-08-27T03:00:00Z", updated_at: "2026-08-27T03:00:00Z", resolved_at: null, events: [] };
    const ready = { ...issue, status: "ready_to_recover", issue_revision: 6, available_actions: ["continue_run"] };
    const original = { resource_id: "original.review", name: "原审阅资源", kind: "ai", revision: 1, config: { provider: "custom", endpoint: "https://original.test/v1", model: "original-model", purposes: ["review"] }, validation: {}, ready: false, deleted: false, projects: [], has_credential: true };
    let validationId = ""; let probes = 0;
    const calls = resultMocks({ "/api/runs/run-result/result": { ...RESULT, issues: [issueSummary] }, "/api/issues/issue-ai": { ok: true, issue },
      "/api/resources/original.review/repair-context": { ok: true, repair_context: { revision: 1 } },
      "/api/resources/original.review/revisions/1": { ok: true, resource: original },
      "/api/resources/validate": (options?: RequestInit) => { validationId = JSON.parse(String(options?.body)).request_id; probes++; return Promise.reject(new Error("lost response")); },
      "/api/resources/original.review/repair": { ok: true, resource: original },
      "/api/issues/issue-ai/recheck": { ok: true, issue: ready, reused: false } });
    const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => validationId && String(input) === `/api/resources/operations/${validationId}` ? jsonResponse({ result: { validation_id: "original-proof", results: { review: { state: "ready" } } } }) : originalFetch(input, options)));
    route("/projects/project-1/runs/run-result?view=result&issue=issue-ai"); render(<App />);
    const drawer = await screen.findByRole("dialog", { name: "问题详情" }); fireEvent.click(await within(drawer).findByRole("button", { name: "修复模型连接" }));
    const apiKey = await screen.findByLabelText("原账号的新 API Key");
    expect(screen.getByText(/original-model/)).toBeVisible();
    fireEvent.change(apiKey, { target: { value: "new-secret" } });
    fireEvent.click(screen.getByLabelText("我确认此 API Key 属于原供应商的同一账号和业务空间"));
    fireEvent.click(screen.getByRole("button", { name: "检查并更新 API Key" }));
    await screen.findByRole("button", { name: "继续本次操作" });
    fireEvent.change(apiKey, { target: { value: "newer-draft-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "继续本次操作" }));
    await screen.findByText(/你新输入的 API Key 尚未提交/);
    expect(apiKey).toHaveValue("newer-draft-secret");
    expect(probes).toBe(1);
    expect(within(drawer).getAllByText(/API Key 已更新，请继续检查其他问题/).length).toBeGreaterThan(0);
    expect(calls.some(([path]) => path === "/api/issues/issue-ai/recheck")).toBe(false);
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/resources/original.review/repair")?.[1]?.body));
    expect(body).toMatchObject({ revision: 1, credential: "new-secret", validation_id: "original-proof", confirm_same_account: true });
    expect(calls.some(([path]) => path.includes('/connection'))).toBe(false);
    expect(JSON.stringify(localStorage)).not.toContain('secret');
  });});


it('keeps project draft on revision conflict and saves only after comparing the latest project', async () => {
  let updates = 0; let expected: number | undefined;
  const newer = { ...PROJECT, name: '另一处保存的项目', current_config_revision: 2 };
  const calls = installFetchMock({
    '/api/projects/project-1': (options?: RequestInit) => {
      if (options?.method !== 'PATCH') return jsonResponse({ ok: true, project: updates ? newer : PROJECT });
      updates++; expected = JSON.parse(String(options.body)).expected_revision;
      return updates === 1 ? jsonResponse({ ok: false, error: { code: 'revision_conflict', message: '修订已改变' } }, 409) : jsonResponse({ ok: true, project: newer });
    },
  });
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, options?: RequestInit) => String(input).startsWith('/api/projects/project-1/operations/') ? jsonResponse({ ok: true, project: null }) : originalFetch(input, options)));
  route('/projects/project-1?dialog=project-settings'); render(<App />);
  fireEvent.change(await screen.findByLabelText('项目名称'), { target: { value: '保留的草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await screen.findByText(/项目设置已被其他操作更新/); expect(screen.getByLabelText('项目名称')).toHaveValue('保留的草稿');
  expect(updates).toBe(1); fireEvent.click(screen.getByRole('button', { name: '保留我的修改，继续编辑' }));
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() => expect(updates).toBe(2)); expect(expected).toBe(2);
  expect(calls.some(([, options]) => options?.method === 'PATCH')).toBe(true);
});

it('keeps all 21 entered tags and blocks saving until corrected', async () => {
  const calls = resultMocks({ '/api/outputs/output-1/material': (options?: RequestInit) => {
    const body = JSON.parse(String(options?.body || '{}'));
    return jsonResponse({ material: options?.method === 'PATCH' ? { ...material(1), ...body, material_revision: 2 } : material(1) });
  } });
  route('/projects/project-1/runs/run-result?view=materials&output=output-1'); render(<App />);
  const tags = await screen.findByLabelText('标签（用逗号分隔，最多 20 个）');
  const input = Array.from({ length: 21 }, (_, i) => `标签${i + 1}`).join('，');
  fireEvent.change(tags, { target: { value: input } });
  fireEvent.click(screen.getByRole('button', { name: '立即保存' }));
  expect(tags).toHaveValue(input);
  expect(calls.some(([, options]) => options?.method === 'PATCH')).toBe(false);
  expect(screen.getAllByText('标签最多 20 个，请减少后保存。').length).toBeGreaterThan(0);
  fireEvent.change(tags, { target: { value: ' #直播,直播，新高光' } });
  fireEvent.click(screen.getByRole('button', { name: '立即保存' }));
  await screen.findByText('已保存');
  const body = JSON.parse(String(calls.find(([, options]) => options?.method === 'PATCH')![1]?.body));
  expect(body.tags).toEqual(['直播', '新高光']);
});

it('replays the original material snapshot after response loss without discarding newer text', async () => {
  const writes: Array<Record<string, any>> = [];
  resultMocks({ '/api/outputs/output-1/material': (options?: RequestInit) => {
    if (options?.method !== 'PATCH') return jsonResponse({ material: material(1) });
    const body = JSON.parse(String(options.body)); writes.push(body);
    if (writes.length === 1) return new Response('lost response', { status: 200 });
    return jsonResponse({ material: { ...material(1), ...body, material_revision: writes.length } });
  } });
  route('/projects/project-1/runs/run-result?view=materials&output=output-1'); render(<App />);
  const description = await screen.findByLabelText('视频描述');
  fireEvent.change(description, { target: { value: '原提交' } });
  fireEvent.click(screen.getByRole('button', { name: '立即保存' }));
  await screen.findByText('保存结果未确认');
  fireEvent.change(description, { target: { value: '后续编辑' } });
  fireEvent.click(screen.getByRole('button', { name: '核对保存结果' }));
  await screen.findByText(/新的修改仍未提交/);
  expect(writes).toHaveLength(2); expect(writes[1]).toEqual(writes[0]); expect(description).toHaveValue('后续编辑');
  fireEvent.click(screen.getByRole('button', { name: '立即保存' }));
  await screen.findByText('已保存');
  expect(writes[2]).toMatchObject({ description: '后续编辑', expected_revision: 2 });
  expect(writes[2].request_id).not.toBe(writes[0].request_id);
});

it('does not save queued or departing edits after a conflict and shows all server titles', async () => {
  let rejectSave!: (response: Response) => void; let reads = 0; let writes = 0;
  const server = { ...material(1), material_revision: 2, titles: [{ title_id: 'title-1', text: '服务端首选' }, { title_id: 'title-2', text: '服务端备选' }] };
  resultMocks({ '/api/outputs/output-1/material': (options?: RequestInit) => {
    if (options?.method !== 'PATCH') return jsonResponse({ material: ++reads === 1 ? material(1) : server });
    writes++;
    if (writes === 1) return new Promise<Response>(resolve => { rejectSave = resolve; });
    return jsonResponse({ material: { ...server, description: '排队的新内容', material_revision: 3 } });
  } });
  route('/projects/project-1/runs/run-result?view=materials&output=output-1'); const view = render(<App />);
  const description = await screen.findByLabelText('视频描述');
  fireEvent.change(description, { target: { value: '先提交' } }); fireEvent.click(screen.getByRole('button', { name: '立即保存' }));
  await waitFor(() => expect(writes).toBe(1));
  fireEvent.change(description, { target: { value: '排队的新内容' } });
  await act(async () => rejectSave(await jsonResponse({ error: { code: 'revision_conflict', message: '已更新' } }, 409)));
  await screen.findByRole('button', { name: '查看最新内容' });
  fireEvent.click(screen.getByRole('button', { name: '查看最新内容' }));
  expect(screen.getByText('标题 1（首选）：服务端首选')).toBeVisible();
  expect(screen.getByText('标题 2：服务端备选')).toBeVisible();
  view.unmount(); expect(writes).toBe(1);
  render(<App />); await screen.findByRole('button', { name: '保留我的修改并保存' });
  expect(writes).toBe(1); expect(screen.getByLabelText('视频描述')).toHaveValue('排队的新内容');
  fireEvent.click(screen.getByRole('button', { name: '保留我的修改并保存' })); await screen.findByText('已保存');
});
