import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { App } from "../src/App";
import { ProjectPage } from "../src/ProjectDetail";
import { PROJECT, installFetchMock, jsonResponse } from "./helpers";

const projectKey = "venus.project-operation.project-1";
const projectPath = "/api/projects/project-1/resume";
const pausedProject = { ...PROJECT, activation_state: "paused", workload: { ...PROJECT.workload, processing: 0, queued: 0 } };
const group = { group_key: "shared:output", title: "输出目录问题", count: 1, issue_ids: ["issue-1"], available_actions: ["recheck"] };
function projectMocks(overrides: Record<string, unknown> = {}) {
  return installFetchMock({ "/api/projects/project-1": { ok: true, project: pausedProject },
    [projectPath]: { ok: true, project: PROJECT, initial_scan: null }, ...overrides });
}
function route(path = "/projects/project-1") { window.history.replaceState({}, "", path); }
async function switchProject() {
  await act(async () => { window.history.pushState({}, "", "/projects/project-2"); window.dispatchEvent(new PopStateEvent("popstate")); });
  await screen.findByRole("heading", { name: "另一个项目" });
}

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.removeItem(projectKey);
  sessionStorage.removeItem("venus.project-operation.project-2");
});

it("prepares a group once and restores its original revisions after response loss and remount", async () => {
  let release!: (response: Response) => void;
  let writes = 0;
  const calls = projectMocks({
    "/api/projects/project-1": { ok: true, project: { ...pausedProject, issue_groups: [group] } },
    "/api/issues/issue-1": () => new Promise<Response>(resolve => { release = resolve; }),
    "/api/issue-groups/shared%3Aoutput/recheck": () => ++writes === 1
      ? Promise.reject(new Error("response lost")) : jsonResponse({ ok: true, issues: [] }),
  });
  route(); const view = render(<App />);
  const check = await screen.findByRole("button", { name: "重新检查这组问题" });
  act(() => { fireEvent.click(check); fireEvent.click(check); });
  await waitFor(() => expect(calls.filter(([path]) => path === "/api/issues/issue-1")).toHaveLength(1));
  await act(async () => release(await jsonResponse({ ok: true, issue: { issue_id: "issue-1", issue_revision: 7 } })));
  await screen.findByRole("button", { name: "核对原操作" });
  const original = calls.find(([path]) => path.endsWith("/recheck"))![1]?.body;
  view.unmount();
  projectMocks({ "/api/issue-groups/shared%3Aoutput/recheck": { ok: true, issues: [] } });
  const fetch = vi.mocked(globalThis.fetch);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "核对原操作" }));
  await waitFor(() => expect(fetch.mock.calls.some(([path]) => String(path).endsWith("/recheck"))).toBe(true));
  const replay = fetch.mock.calls.find(([path]) => String(path).endsWith("/recheck"))![1]?.body;
  expect(replay).toBe(original);
  expect(JSON.parse(String(replay))).toEqual({ request_id: expect.any(String), issue_revisions: { "issue-1": 7 } });
  expect(fetch.mock.calls.some(([path]) => String(path) === "/api/issues/issue-1")).toBe(false);
});

it.each(["read", "invalid"])("blocks new operations when the stored project request has a %s error", async failure => {
  if (failure === "invalid") sessionStorage.setItem(projectKey, JSON.stringify({ id: "invalid", action: ["resume"] }));
  const read = Storage.prototype.getItem;
  let unavailable = failure === "read";
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key) {
    if (key === projectKey && unavailable) throw new DOMException("denied", "SecurityError");
    return read.call(this, key);
  });
  const calls = projectMocks(); route(); render(<App />);
  expect(await screen.findByRole("button", { name: "恢复项目" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重新读取操作记录" }));
  expect(calls.filter(([path]) => path === projectPath)).toHaveLength(0);
  if (failure === "invalid") expect(read.call(sessionStorage, projectKey)).not.toBeNull();
  unavailable = false; sessionStorage.removeItem(projectKey);
  fireEvent.click(screen.getByRole("button", { name: "重新读取操作记录" }));
  expect(screen.getByRole("button", { name: "恢复项目" })).toBeEnabled();
});

it("keeps a confirmed result when clearing storage fails and only retries local cleanup", async () => {
  const remove = Storage.prototype.removeItem;
  let unavailable = true;
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, key) {
    if (key === projectKey && unavailable) throw new DOMException("denied", "SecurityError");
    remove.call(this, key);
  });
  const calls = projectMocks(); route(); render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "恢复项目" }));
  expect(await screen.findByText(/操作结果已确认，但无法清除/)).toBeVisible();
  expect(screen.getByRole("button", { name: "恢复项目" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "核对原操作" })).not.toBeInTheDocument();
  unavailable = false;
  fireEvent.click(screen.getByRole("button", { name: "重试清除操作记录" }));
  await waitFor(() => expect(sessionStorage.getItem(projectKey)).toBeNull());
  expect(calls.filter(([path]) => path === projectPath)).toHaveLength(1);
});

it.each(["revision_conflict", "request_id_conflict", "invalid_response"])("handles project %s without replacing an uncertain request", async code => {
  const calls = projectMocks({ [projectPath]: () => code === "invalid_response" ? Promise.resolve(new Response("lost"))
    : jsonResponse({ ok: false, error: { code, message: "提交未完成" } }, 409) });
  route(); render(<App />); fireEvent.click(await screen.findByRole("button", { name: "恢复项目" }));
  if (code === "revision_conflict") {
    await waitFor(() => expect(screen.getByRole("button", { name: "恢复项目" })).toBeEnabled());
    expect(sessionStorage.getItem(projectKey)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "恢复项目" }));
  } else {
    fireEvent.click(await screen.findByRole("button", { name: "核对原操作" }));
  }
  await waitFor(() => expect(calls.filter(([path]) => path === projectPath)).toHaveLength(2));
  const bodies = calls.filter(([path]) => path === projectPath).map(([, options]) => options?.body);
  expect(bodies[0] === bodies[1]).toBe(code !== "revision_conflict");
});

it("does not change a confirmed project operation into an unknown result when refresh fails", async () => {
  let completed = false;
  const calls = projectMocks({
    [projectPath]: () => { completed = true; return jsonResponse({ ok: true, project: PROJECT }); },
    "/api/projects/project-1": () => completed ? Promise.reject(new Error("read failed")) : jsonResponse({ ok: true, project: pausedProject }),
  });
  route(); render(<App />); fireEvent.click(await screen.findByRole("button", { name: "恢复项目" }));
  await screen.findByText(/刷新失败，当前显示的是上次加载/);
  expect(sessionStorage.getItem(projectKey)).toBeNull();
  expect(screen.queryByRole("button", { name: "核对原操作" })).not.toBeInTheDocument();
  expect(calls.filter(([path]) => path === projectPath)).toHaveLength(1);
});

it.each(["preparing", "submitting"])("isolates old project results while %s and preserves the new object's record", async stage => {
  let release!: (response: Response) => void;
  const deferred = () => new Promise<Response>(resolve => { release = resolve; });
  const otherKey = "venus.project-operation.project-2";
  const otherRecord = JSON.stringify({ id: "other-request", action: "enable" });
  sessionStorage.setItem(otherKey, otherRecord);
  const calls = projectMocks({
    "/api/projects/project-1": { ok: true, project: { ...pausedProject, issue_groups: [group] } },
    "/api/projects/project-2": { ok: true, project: { ...pausedProject, project_id: "project-2", name: "另一个项目" } },
    "/api/projects/project-2/runs": { ok: true, runs: [], has_more: false, cursor: null },
    [projectPath]: deferred,
    "/api/issues/issue-1": deferred,
  });
  route(); render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: stage === "preparing" ? "重新检查这组问题" : "恢复项目" }));
  await waitFor(() => expect(release).toBeDefined());
  await switchProject();
  await act(async () => release(await jsonResponse(stage === "preparing" ? { ok: true, issue: { issue_id: "issue-1", issue_revision: 3 } } : { ok: true, project: PROJECT })));
  expect(sessionStorage.getItem(otherKey)).toBe(otherRecord);
  expect(sessionStorage.getItem(projectKey)).toBeNull();
  expect(screen.getByRole("heading", { name: "另一个项目" })).toBeVisible();
  expect(screen.queryByText("项目已恢复")).not.toBeInTheDocument();
  expect(calls.filter(([path]) => path === "/api/issue-groups/shared%3Aoutput/recheck")).toHaveLength(0);
});

it("keeps success confirmed when the page notification throws", async () => {
  const calls = projectMocks();
  const notify = vi.fn(() => { throw new Error('notification failed'); });
  render(<MemoryRouter initialEntries={['/projects/project-1']}><Routes><Route path="/projects/:projectId" element={<ProjectPage notify={notify} />} /></Routes></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '恢复项目' }));
  expect(await screen.findByText(/操作已完成，但更新页面失败/)).toBeVisible();
  expect(sessionStorage.getItem(projectKey)).toBeNull();
  expect(notify).toHaveBeenCalledTimes(1);
  expect(calls.filter(([path]) => path === projectPath)).toHaveLength(1);
  expect(screen.queryByRole('button', { name: '核对原操作' })).not.toBeInTheDocument();
});

it("never removes a later request when a departed page completes", async () => {
  let complete!: (response: Response) => void;
  projectMocks({ [projectPath]: () => new Promise<Response>(resolve => { complete = resolve; }) });
  route(); const view = render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: '恢复项目' }));
  await waitFor(() => expect(complete).toBeDefined());
  view.unmount();
  const later = JSON.stringify({ id: 'later-request', action: 'resume' });
  sessionStorage.setItem(projectKey, later);
  projectMocks(); render(<App />);
  await screen.findByRole('button', { name: '核对原操作' });
  await act(async () => complete(await jsonResponse({ ok: true, project: PROJECT })));
  expect(sessionStorage.getItem(projectKey)).toBe(later);
  expect(screen.getByRole('button', { name: '核对原操作' })).toBeEnabled();
  expect(screen.queryByText('项目已恢复')).not.toBeInTheDocument();
});

it('keeps the enable endpoint and request body unchanged', async () => {
  const calls = projectMocks({ '/api/projects/project-1': { ok: true, project: { ...pausedProject, activation_state: 'inactive' } },
    '/api/projects/project-1/enable': { ok: true, project: PROJECT, initial_scan: null } });
  route(); render(<App />); fireEvent.click(await screen.findByRole('button', { name: '启用项目' }));
  await waitFor(() => expect(calls.filter(([path]) => path.endsWith('/enable'))).toHaveLength(1));
  expect(JSON.parse(String(calls.find(([path]) => path.endsWith('/enable'))![1]?.body)))
    .toEqual({ request_id: expect.stringMatching(/^project-enable-/) });
});

it("saves the prepared project request before sending and retries storage without a new identity", async () => {
  const store = Storage.prototype.setItem;
  let unavailable = true;
  const attempts: string[] = [];
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
    if (key === projectKey) {
      attempts.push(value);
      if (unavailable) throw new DOMException("quota", "QuotaExceededError");
    }
    store.call(this, key, value);
  });
  const calls = installFetchMock({
    "/api/projects/project-1": { ok: true, project: { ...PROJECT, activation_state: "paused" } },
    [projectPath]: { ok: true, project: PROJECT, initial_scan: null },
  });
  window.history.replaceState({}, "", "/projects/project-1");
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "恢复项目" }));
  expect(await screen.findByText(/无法保存本地操作记录/)).toBeVisible();
  expect(calls.filter(([path]) => path === projectPath)).toHaveLength(0);
  unavailable = false;
  fireEvent.click(screen.getByRole("button", { name: "重试保存操作" }));
  await waitFor(() => expect(calls.filter(([path]) => path === projectPath)).toHaveLength(1));
  expect(attempts[1]).toBe(attempts[0]);
  expect(JSON.parse(String(calls.find(([path]) => path === projectPath)?.[1]?.body)))
    .toEqual({ request_id: JSON.parse(attempts[0]).id });
  await waitFor(() => expect(sessionStorage.getItem(projectKey)).toBeNull());
});
