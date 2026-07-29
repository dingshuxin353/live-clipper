import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { installFetchMock, MODELS } from "./helpers";

describe("React application shell", () => {
  it("renders tabs in product order, switches pages, and shows the confirmation badge", async () => {
    installFetchMock({
      "/api/confirmations": { ok: true, confirmations: [{ id: "c1", status: "pending", action: "delete_clip" }] },
    });
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    expect(within(navigation).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "▶切片结果", "◷自动化", "✓确认1", "☰设置",
    ]);
    fireEvent.click(within(navigation).getByRole("button", { name: /自动化/ }));
    expect(screen.getByRole("heading", { name: "自动化" })).toBeVisible();
  });

  it("reports initial API failure with the existing user-facing boundary", async () => {
    installFetchMock({ "/api/service": new Error("offline") });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("初次加载失败：网络连接失败");
  });

  it("loads config, marks edits dirty, discards, and submits all configured values", async () => {
    const calls = installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
    const source = await screen.findByLabelText(/录播文件夹/);
    fireEvent.change(source, { target: { value: "/recordings" } });
    expect(screen.getByText("有未保存的更改")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "放弃" }));
    expect(source).toHaveValue("");
    fireEvent.change(source, { target: { value: "/recordings" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存配置" })[0]);
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/config" && options?.method === "POST")).toBe(true));
    const saveCall = calls.find(([path, options]) => path === "/api/config" && options?.method === "POST");
    const body = JSON.parse(String(saveCall?.[1]?.body));
    expect(body.config.recording_source_default.source_dir).toBe("/recordings");
    expect(document.querySelectorAll("[data-config-field]")).toHaveLength(47);
  });

  it("keeps Small, Medium, Large order and protects current model actions", async () => {
    installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
    const list = await screen.findByText("Small");
    const rows = document.querySelectorAll(".asr-model-row");
    expect([...rows].map((row) => row.querySelector("strong")?.textContent)).toEqual([
      "Small 轻量", "Medium 平衡", "Large 高精度",
    ]);
    expect(list.closest(".asr-model-row")).not.toHaveTextContent("删除");
    expect(screen.getAllByRole("button", { name: "下载" })).toHaveLength(2);
  });

  it("selects and deletes only non-current installed models through the model APIs", async () => {
    const models = [
      { ...MODELS[0] },
      { ...MODELS[1], state: "installed", current: false },
      { ...MODELS[2] },
    ];
    const calls = installFetchMock({ "/api/asr/models": { ok: true, models, download_source: "modelscope" } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
    const medium = (await screen.findByText("Medium")).closest(".asr-model-row") as HTMLElement;
    fireEvent.click(within(medium).getByRole("button", { name: "设为当前模型" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/asr/models/select" && options?.method === "POST")).toBe(true));
    fireEvent.click(within(medium).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/asr/models/delete" && options?.method === "POST")).toBe(true));
  });

  it("runs scan, review automation, and confirmation operations through existing APIs", async () => {
    const calls = installFetchMock({
      "/api/confirmations": { ok: true, confirmations: [{ id: "c1", status: "pending", action: "delete_clip" }] },
      "/api/review-automation/run-due": { ok: true, skipped_reason: "review_automation_disabled" },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "立即扫描录播" }));
    const navigation = screen.getByRole("navigation", { name: "控制台页面" });
    fireEvent.click(within(navigation).getByRole("button", { name: /自动化/ }));
    fireEvent.click(await screen.findByRole("button", { name: "立即处理待审阅" }));
    expect((await screen.findAllByText(/自动 AI 审阅还没有启用/))[0]).toBeVisible();
    fireEvent.click(within(navigation).getByRole("button", { name: /确认1/ }));
    fireEvent.click(await screen.findByRole("button", { name: "确认执行" }));
    await waitFor(() => {
      const posted = calls.filter(([, options]) => options?.method === "POST").map(([path]) => path);
      expect(posted).toEqual(expect.arrayContaining([
        "/api/service/scan-now",
        "/api/review-automation/run-due",
        "/api/confirmations/c1/approve",
      ]));
    });
  });

  it("adds the Electron safe-area class only when the existing bridge is present", async () => {
    installFetchMock();
    window.liveClipperShell = { selectFolder: vi.fn(() => Promise.resolve(null)) };
    const { unmount } = render(<App />);
    await screen.findByText("直播切片 · 本地控制台");
    expect(document.body).toHaveClass("in-app-shell");
    unmount();
  });

  it("aborts initial requests on unmount so stale results cannot write state", () => {
    const signals: AbortSignal[] = [];
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, options?: RequestInit) => {
      if (options?.signal) signals.push(options.signal);
      return new Promise<Response>(() => undefined);
    }));
    const { unmount } = render(<App />);
    unmount();
    expect(signals.length).toBeGreaterThan(0);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });
});
