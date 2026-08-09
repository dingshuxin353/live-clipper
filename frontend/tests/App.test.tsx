import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { installFetchMock, jsonResponse, MODELS } from "./helpers";

describe("React application shell", () => {
  it("renders tabs in product order, switches pages, and shows the confirmation badge", async () => {
    installFetchMock({
      "/api/confirmations": { ok: true, confirmations: [{ id: "c1", status: "pending", action: "delete_clip" }] },
    });
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    expect(within(navigation).getAllByRole("button").slice(0, 4).map((button) => button.textContent)).toEqual([
      "切片结果", "自动化", "文件清理1", "设置",
    ]);
    expect(within(navigation).getByRole("button", { name: /文件清理.*1/ })).toBeVisible();
    expect(within(navigation).queryByRole("button", { name: /^确认1?$/ })).not.toBeInTheDocument();
    expect(document.querySelector(".astryx-app-shell")).toBeInTheDocument();
    expect(navigation.closest(".astryx-side-nav")).toBeInTheDocument();
    fireEvent.click(within(navigation).getByRole("button", { name: /自动化/ }));
    expect(screen.getByRole("heading", { name: "自动化" })).toBeVisible();
  });

  it("uses the upstream mobile navigation below the 640px CSS breakpoint", async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const matchMedia = vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query === "(max-width: 640px)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    }));
    try {
      installFetchMock();
      render(<App />);

      const toggle = await screen.findByRole("button", { name: "打开导航" });
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      fireEvent.click(toggle);
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" }));

      const drawer = screen.getByRole("dialog", { name: "导航菜单" });
      const items = ["切片结果", "自动化", "文件清理", "设置"].map(
        (label) => within(drawer).getByRole("button", { name: label }),
      );
      expect(items.map((button) => button.textContent)).toEqual(["切片结果", "自动化", "文件清理", "设置"]);
      items[1].focus();
      expect(items[1]).toHaveFocus();
      fireEvent.click(items[1]);

      await waitFor(() => expect(screen.queryByRole("dialog", { name: "导航菜单" })).not.toBeInTheDocument());
      expect(screen.getByRole("heading", { name: "自动化" })).toBeVisible();
      fireEvent.click(toggle);
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(2));
      expect(within(screen.getByRole("dialog", { name: "导航菜单" })).getByRole("button", { name: "切片结果" })).toBeVisible();
    } finally {
      if (originalScrollIntoView) {
        HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      }
      matchMedia.mockRestore();
    }
  });

  it("keeps scheduler actions in one reflow group for narrow layouts", async () => {
    installFetchMock({
      "/api/scheduler": {
        ok: true,
        scheduler: { enabled: true },
        jobs: [{
          id: "weekly_recording_scan",
          name: "每周录播扫描",
          type: "scan_recordings",
          schedule: "weekly",
        }],
      },
    });
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    fireEvent.click(within(navigation).getByRole("button", { name: "自动化" }));

    const row = (await screen.findByText("每周录播扫描")).closest("li");
    expect(row).toHaveClass("scheduler-job");
    expect(row?.querySelector(".scheduler-job-description")).toHaveTextContent(
      "weekly_recording_scan · 扫描录播 · 每周",
    );
    const actions = row?.querySelector(".scheduler-job-actions") as HTMLElement;
    expect(actions).toBeInTheDocument();
    expect(within(actions).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "编辑", "立即执行", "暂停", "启用",
    ]);
  });

  it("separates the adjacent service metric grids", async () => {
    installFetchMock();
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    fireEvent.click(within(navigation).getByRole("button", { name: "自动化" }));

    const metrics = document.getElementById("serviceMetrics");
    const summary = document.getElementById("serviceSummary");
    expect(summary?.previousElementSibling).toBe(metrics);
    expect(summary).toHaveClass("service-summary-grid");
    expect(metrics?.closest(".content-card")).toContainElement(summary);
    for (const row of Array.from(summary?.children ?? [])) {
      expect(row).toHaveClass("info-row");
      const value = row.querySelector(".technical-value");
      expect(value).toHaveAttribute("title", value?.textContent);
    }
  });

  it("reports initial API failure with the existing user-facing boundary", async () => {
    installFetchMock({ "/api/service": new Error("offline") });
    render(<App />);
    const error = await screen.findByText("初次加载失败：网络连接失败");
    expect(error).toBeVisible();
    expect(error).toHaveAttribute("role", "alert");
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
  });

  it("keeps stuck, AI review, and run failures visible as compact alerts", async () => {
    installFetchMock({
      "/api/runs": {
        ok: true,
        runs: [{
          run_id: "run-review",
          source_name: "review.mp4",
          phase: "needs_review",
          stuck: true,
        }],
      },
      "/api/runs/run-review": {
        ok: true,
        run: { run_id: "run-review", phase: "needs_review" },
        ai_review: { status: "failed", error: "审阅器离线" },
        actions: {},
      },
    });
    const review = render(<App />);
    const stuck = await screen.findByText("已处理较长时间仍未完成，可能已卡住。");
    const aiReview = await screen.findByText("上次 AI 审阅失败：审阅器离线");
    expect(stuck).toHaveAttribute("role", "alert");
    expect(aiReview).toHaveAttribute("role", "alert");
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
    review.unmount();

    installFetchMock({
      "/api/runs": {
        ok: true,
        runs: [{ run_id: "run-failed", source_name: "failed.mp4", phase: "failed" }],
      },
      "/api/runs/run-failed": {
        ok: true,
        run: { run_id: "run-failed", phase: "failed", last_error: "转写失败" },
      },
    });
    render(<App />);
    const failed = await screen.findByText("转写失败");
    expect(failed).toHaveAttribute("role", "alert");
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
  });

  it("offers failed runs an actionable retry and shows configuration guidance", async () => {
    const calls = installFetchMock({
      "/api/runs": {
        ok: true,
        runs: [{ run_id: "run-failed", source_name: "failed.mkv", phase: "failed" }],
      },
      "/api/runs/run-failed": {
        ok: true,
        run: { run_id: "run-failed", phase: "failed", last_error: "流水线失败" },
      },
      "/api/runs/run-failed/retry": () => jsonResponse({
        ok: false,
        error_code: "pipeline_configuration_required",
        message: "请先到「设置 → AI 服务」配置 AI API Key，再开始处理录播。",
      }, 409),
    });
    render(<App />);

    expect(await screen.findByText("流水线失败")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重试处理" }));

    expect(await screen.findByText("请先到「设置 → AI 服务」配置 AI API Key，再开始处理录播。")).toBeVisible();
    expect(calls.some(([path, options]) => path === "/api/runs/run-failed/retry" && options?.method === "POST")).toBe(true);
  });

  it("shows the same configuration guidance when manual scan is blocked", async () => {
    installFetchMock({
      "/api/service/scan-now": () => jsonResponse({
        ok: false,
        error_code: "pipeline_configuration_required",
        message: "请先到「设置 → AI 服务」配置 AI API Key，再开始处理录播。",
      }, 409),
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "立即扫描录播" }));

    expect(await screen.findByText("请先到「设置 → AI 服务」配置 AI API Key，再开始处理录播。")).toBeVisible();
  });

  it("labels queued recordings and reports precise no-new-recording feedback", async () => {
    installFetchMock({
      "/api/runs": {
        ok: true,
        runs: [{ run_id: "run-queued", source_name: "queued.mkv", phase: "queued" }],
      },
      "/api/runs/run-queued": {
        ok: true,
        run: { run_id: "run-queued", phase: "queued" },
      },
      "/api/service/scan-now": {
        ok: true,
        discovered_runs: 0,
        started_runs: 0,
        queued_runs: 1,
        duplicate_files: 1,
        message: "没有发现未处理录像：已处理或已排队 1 个，过新 0 个，写入中 0 个。",
      },
    });
    render(<App />);

    const queuedRow = (await screen.findByText("queued.mkv")).closest<HTMLElement>(".run-row");
    expect(queuedRow).not.toBeNull();
    expect(within(queuedRow as HTMLElement).getAllByText("排队中").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "立即扫描录播" }));

    expect(await screen.findByText("没有发现未处理录像：已处理或已排队 1 个，过新 0 个，写入中 0 个。")).toBeVisible();
    expect(screen.queryByText("操作已完成")).not.toBeInTheDocument();
  });

  it("uses one persistent status and no toast for review automation results", async () => {
    installFetchMock({
      "/api/review-automation/run-due": {
        ok: true,
        skipped_reason: "review_automation_disabled",
      },
    });
    render(<App />);
    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    fireEvent.click(within(navigation).getByRole("button", { name: /自动化/ }));
    fireEvent.click(screen.getByRole("button", { name: "立即处理待审阅" }));

    const statusMessage = await screen.findByText(/自动 AI 审阅还没有启用/);
    const status = document.getElementById("reviewAutomationActionStatus");
    expect(status).toBe(statusMessage);
    expect(status).toHaveAttribute("role", "status");
    expect(screen.getAllByText(/自动 AI 审阅还没有启用/)).toHaveLength(1);
    expect(document.querySelector(".astryx-toast")).not.toBeInTheDocument();
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
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
    expect(document.querySelectorAll("[data-config-field]")).toHaveLength(46);
  });

  it("lets skipped-onboarding users paste and save the LLM key without echoing it", async () => {
    const marker = "settings-clipboard-secret";
    const readClipboardText = vi.fn(() => Promise.resolve(` ${marker}\n`));
    window.liveClipperShell = { readClipboardText };
    const calls = installFetchMock({
      "/api/config/llm-key": { ok: true, saved: true, api_key_env: "CHEAP_MODEL_API_KEY", message: "AI API key 已保存" },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));

    fireEvent.click(await screen.findByRole("button", { name: "粘贴 AI API key" }));
    await waitFor(() => expect(readClipboardText).toHaveBeenCalledTimes(1));
    const input = screen.getByLabelText("AI API key") as HTMLInputElement;
    expect(input.value).toBe(marker);
    expect(input.getAttribute("value")).toBeNull();
    expect(document.body.innerHTML).not.toContain(marker);

    fireEvent.click(screen.getByRole("button", { name: "保存 AI API key" }));
    await screen.findByText("AI API key 已保存");
    const request = calls.find(([path]) => path === "/api/config/llm-key");
    expect(JSON.parse(String(request?.[1]?.body)).api_key).toBe(marker);
    expect(input.value).toBe("");
    expect(document.body.innerHTML).not.toContain(marker);
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
    expect(rows[0].closest(".astryx-list")).toContainElement(rows[0] as HTMLElement);
    expect(rows[1].querySelector(".astryx-button")).toBeInTheDocument();
  });

  it("selects and deletes only non-current installed models through the model APIs", async () => {
    const models = [
      { ...MODELS[0] },
      { ...MODELS[1], state: "installed", current: false },
      { ...MODELS[2] },
    ];
    const calls = installFetchMock({ "/api/asr/models": { ok: true, models, download_source: "modelscope" } });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
    const medium = (await screen.findByText("Medium")).closest(".asr-model-row") as HTMLElement;
    fireEvent.click(within(medium).getByRole("button", { name: "设为当前模型" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/asr/models/select" && options?.method === "POST")).toBe(true));
    fireEvent.click(within(medium).getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
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
    fireEvent.click(within(navigation).getByRole("button", { name: /文件清理1/ }));
    fireEvent.click(await screen.findByRole("button", { name: "确认执行" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "确认执行" }));
    await waitFor(() => {
      const posted = calls.filter(([, options]) => options?.method === "POST").map(([path]) => path);
      expect(posted).toEqual(expect.arrayContaining([
        "/api/service/scan-now",
        "/api/review-automation/run-due",
        "/api/confirmations/c1/approve",
      ]));
    });
  });

  it("uses Astryx controls and status surfaces across clips, automation, and confirmations", async () => {
    installFetchMock();
    render(<App />);
    await screen.findByRole("heading", { name: "切片结果" });
    expect(document.querySelector(".astryx-tab-list")).toBeInTheDocument();
    expect(document.querySelector(".astryx-empty-state")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即扫描录播" })).toHaveClass("astryx-button");

    const navigation = screen.getByRole("navigation", { name: "控制台页面" });
    fireEvent.click(within(navigation).getByRole("button", { name: /^自动化$/ }));
    expect(await screen.findByRole("heading", { name: "自动化" })).toBeVisible();
    expect(document.querySelector(".astryx-card")).toBeInTheDocument();
    expect(document.querySelector(".astryx-list")).toBeInTheDocument();

    fireEvent.click(within(navigation).getByRole("button", { name: /^文件清理$/ }));
    expect(await screen.findByRole("heading", { name: "待确认的清理操作" })).toBeVisible();
    expect(screen.getByText("删除成片、中间文件或本地录像副本前，需要你确认。NAS 原始录像不会被删除。")).toBeVisible();
    expect(screen.getByText("没有待确认的清理操作")).toBeVisible();
    expect(within(navigation).queryByRole("button", { name: /^确认$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^确认$/ })).not.toBeInTheDocument();
    expect(document.querySelector(".astryx-empty-state")).toBeInTheDocument();
  });

  it("renders transient feedback with the Astryx Toast surface", async () => {
    installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "立即扫描录播" }));
    expect(await screen.findByText("操作已完成")).toBeInTheDocument();
    expect(document.querySelector(".astryx-toast")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭通知" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Dismiss notification" })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });

  it("announces AI review busy state in Chinese without Astryx loading text", async () => {
    installFetchMock({
      "/api/runs": {
        ok: true,
        runs: [{ run_id: "run-reviewing", source_name: "reviewing.mp4", phase: "needs_review" }],
      },
      "/api/runs/run-reviewing": {
        ok: true,
        run: { run_id: "run-reviewing", phase: "needs_review" },
        active_job: { id: "job-reviewing", status: "running" },
        actions: { can_ai_review: true },
      },
    });
    render(<App />);

    const button = await screen.findByRole("button", { name: "AI 审阅中…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("data-busy", "true");
    expect(button).not.toHaveAttribute("aria-busy");
    const announcements = screen.getAllByRole("status").filter(
      (status) => status.textContent === "AI 审阅正在进行",
    );
    expect(announcements).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });

  it("localizes the root skip dialog close button through Astryx i18n", async () => {
    installFetchMock({
      "/api/onboarding": {
        needs_onboarding: true,
        initial_local_model: MODELS[0].id,
        initial_asr_mode: "local",
        presets: [],
      },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "稍后设置" }));
    expect(screen.getByRole("button", { name: "关闭" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
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
