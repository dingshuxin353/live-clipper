import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { Onboarding } from "../src/Onboarding";
import { installFetchMock, MODELS } from "./helpers";

function onboardingPayload() {
  return {
    needs_onboarding: true,
    initial_local_model: MODELS[0].id,
    initial_asr_mode: "local",
    presets: [{ id: "deepseek", label: "DeepSeek", api_base: "https://example.test/v1", model: "chat" }],
  };
}

describe("four-step onboarding", () => {
  it("validates source before advancing, supports back, cloud/local switching, and LLM test", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true, video_count: 2 },
      "/api/onboarding/test-llm": { ok: true, message: "连接成功" },
    });
    render(<Onboarding notify={vi.fn()} />);
    expect(await screen.findByText("1 录播文件夹")).toBeVisible();
    fireEvent.change(screen.getByLabelText("录播文件夹路径"), { target: { value: "/recordings" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(await screen.findByText("选择语音识别方式")).toBeVisible();
    expect(document.querySelector(".astryx-radio-list")).toBeInTheDocument();
    expect(document.querySelectorAll(".astryx-selectable-card")).toHaveLength(3);
    expect(screen.getByRole("combobox", { name: "模型下载源" }).closest(".astryx-selector")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/云端识别/));
    expect(screen.getByLabelText("识别服务地址")).toBeVisible();
    fireEvent.click(screen.getByLabelText(/本机识别/));
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(await screen.findByText("选一个 AI 服务")).toBeVisible();
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "do-not-log" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接成功")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "上一步" }));
    expect(screen.getByText("选择语音识别方式")).toBeVisible();
  });

  it("uses the onboarding Small default even when the saved model API marks Large current", async () => {
    const models = MODELS.map((model, index) => ({
      ...model,
      current: index === 2,
    }));
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/asr/models": { ok: true, models, download_source: "modelscope" },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    expect(screen.getByText("Small").closest(".onboarding-model-card")).toHaveAttribute("data-selected", "true");
    expect(screen.getByText("Large").closest(".onboarding-model-card")).toHaveAttribute("data-selected", "false");
  });

  it("shows the native folder picker only with the Electron bridge and preserves cancellation", async () => {
    const selectFolder = vi.fn(() => Promise.resolve<string | null>(null));
    window.liveClipperShell = { selectFolder };
    installFetchMock({ "/api/onboarding": onboardingPayload() });
    render(<Onboarding notify={vi.fn()} />);
    const picker = await screen.findByRole("button", { name: "选择文件夹" });
    const browse = picker.closest(".onboarding-browse") as HTMLElement;
    expect(browse).toBeInTheDocument();
    fireEvent.click(picker);
    await waitFor(() => expect(selectFolder).toHaveBeenCalledWith("选择录播文件夹"));
    expect(screen.getByLabelText("录播文件夹路径")).toHaveValue("");
  });

  it.each([
    ["a rejected directory", { ok: false, message: "请填写录播文件夹路径" }, "请填写录播文件夹路径"],
    ["a directory check request failure", new Error("目录校验失败"), "网络连接失败"],
  ])("focuses and reveals the source field after %s", async (_case, failure, message) => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": failure,
    });
    render(<Onboarding notify={vi.fn()} />);
    const source = await screen.findByLabelText("录播文件夹路径");
    const scrollIntoView = vi.fn();
    source.scrollIntoView = scrollIntoView;
    const next = screen.getByRole("button", { name: "下一步" });
    expect(next).toBeEnabled();

    fireEvent.click(next);

    expect((await screen.findAllByText(message)).length).toBeGreaterThan(0);
    await waitFor(() => expect(source).toHaveFocus());
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
  });

  it("renders one source error with a direct accessible association and no status surface", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: false, message: "请填写录播文件夹路径" },
    });
    render(<Onboarding notify={vi.fn()} />);
    const source = await screen.findByLabelText("录播文件夹路径");
    source.scrollIntoView = vi.fn();

    fireEvent.click(screen.getByRole("button", { name: "下一步" }));

    const messages = await screen.findAllByText("请填写录播文件夹路径");
    expect(messages).toHaveLength(1);
    expect(messages[0]).toHaveAttribute("role", "alert");
    expect(source).toHaveAttribute("aria-invalid", "true");
    expect(source).toHaveAttribute("aria-errormessage", messages[0].id);
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
    expect(document.querySelector(".astryx-field-status")).not.toBeInTheDocument();
  });

  it("does not steal focus when the user only checks the source folder", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: false, message: "目录不可用" },
    });
    render(<Onboarding notify={vi.fn()} />);
    const source = await screen.findByLabelText("录播文件夹路径");
    const check = screen.getByRole("button", { name: "检查文件夹" });
    check.focus();

    fireEvent.click(check);

    expect(await screen.findByRole("alert")).toHaveTextContent("目录不可用");
    expect(source).not.toHaveFocus();
  });

  it("shares one Chinese live status across both source busy buttons", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": () => new Promise<Response>(() => undefined),
    });
    render(<Onboarding notify={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "检查文件夹" }));

    const buttons = await screen.findAllByRole("button", { name: "检查中…" });
    expect(buttons).toHaveLength(2);
    for (const button of buttons) {
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("data-busy", "true");
      expect(button).not.toHaveAttribute("aria-busy");
    }
    expect(screen.getAllByRole("status").filter(
      (status) => status.textContent === "正在检查录播文件夹",
    )).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });

  it("hides the native picker in browser mode", async () => {
    installFetchMock({ "/api/onboarding": onboardingPayload() });
    render(<Onboarding notify={vi.fn()} />);
    await screen.findByText("欢迎使用 Venus");
    expect(screen.queryByRole("button", { name: "选择文件夹" })).not.toBeInTheDocument();
  });

  it("opens one shared skip dialog and only posts skip after confirmation", async () => {
    const calls = installFetchMock({ "/api/onboarding": onboardingPayload() });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "稍后设置" }));
    expect(screen.getByRole("dialog")).toHaveClass("astryx-dialog");
    expect(screen.getByRole("dialog")).toHaveTextContent("未配置录像目录时不会自动发现新录像");
    expect(calls.some(([path, options]) => path === "/api/onboarding/skip" && options?.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "确认稍后设置" }));
    await waitFor(() => expect(calls.some(([path, options]) => path === "/api/onboarding/skip" && options?.method === "POST")).toBe(true));
  });

  it("keeps blocked ASR advance actionable and explains the missing download", async () => {
    const models = MODELS.map((model) => ({ ...model, state: "missing", current: false }));
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/asr/models": { ok: true, models, download_source: "modelscope" },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    const next = screen.getByRole("button", { name: "下一步" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(await screen.findByText("请先下载并安装所选本地模型")).toBeVisible();
    expect(document.getElementById(`onboardingModelDownload-${MODELS[0].id}`)).toHaveFocus();
  });

  it("allows an active download to continue into the AI step", async () => {
    const models = MODELS.map((model, index) => index === 0
      ? { ...model, state: "downloading", downloading: true, job_id: "job-active" }
      : model);
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/asr/models": { ok: true, models, download_source: "modelscope" },
      "/api/jobs/job-active": { ok: true, job: { status: "running", bytes_downloaded: 1024, bytes_total: 4096 } },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(await screen.findByText("选一个 AI 服务")).toBeVisible();
  });

  it("explains and focuses the first missing cloud field", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByLabelText(/云端识别/));
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    const error = await screen.findByText("请填写识别 API key");
    const key = screen.getByLabelText("识别 API key");
    expect(error).toHaveAttribute("role", "alert");
    expect(key).toHaveFocus();
    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveAttribute("aria-errormessage", error.id);
    expect(document.querySelector(".astryx-field-status")).not.toBeInTheDocument();
  });

  it("closes the skip dialog with Escape and returns focus", async () => {
    installFetchMock({ "/api/onboarding": onboardingPayload() });
    render(<Onboarding notify={vi.fn()} />);
    const trigger = await screen.findByRole("button", { name: "稍后设置" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("recovers an active model job and reports success without exposing secrets to console", async () => {
    const downloading = MODELS.map((model, index) => index === 0
      ? { ...model, state: "downloading", downloading: true, job_id: "job-1", partial_bytes: 1024 }
      : model);
    let modelReads = 0;
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/asr/models": () => {
        modelReads += 1;
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          models: modelReads < 3 ? downloading : MODELS,
          download_source: "modelscope",
        })));
      },
      "/api/jobs/job-1": { ok: true, job: { status: "succeeded", bytes_downloaded: 1024, bytes_total: 1024 } },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    expect(await screen.findByText("模型已安装，可以继续")).toBeVisible();
    expect(consoleSpy).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it("preserves a failed download for retry", async () => {
    const downloading = MODELS.map((model, index) => index === 0
      ? { ...model, state: "downloading", downloading: true, job_id: "job-fail" }
      : model);
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/asr/models": { ok: true, models: downloading, download_source: "modelscope" },
      "/api/jobs/job-fail": { ok: true, job: { status: "failed", error: "下载失败，可稍后继续" } },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    expect(await screen.findByText("下载失败，可稍后继续")).toBeVisible();
  });

  it("completes all four steps and never echoes entered keys in the summary", async () => {
    const calls = installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true, video_count: 1 },
      "/api/onboarding/test-llm": { ok: true, message: "连接成功" },
      "/api/onboarding/complete": { ok: true },
      "/api/service/start": { ok: false },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText("录播文件夹路径"), { target: { value: "/recordings" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选一个 AI 服务");
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "super-secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("确认设置");
    expect(screen.queryByText("super-secret-key")).not.toBeInTheDocument();
    expect(screen.getByText("已填写（只保存在本机 .env）")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "完成设置" }));
    expect(await screen.findByText("设置已保存，但自动化服务未启动，可进入主界面后手动启动")).toBeVisible();
    const completeCall = calls.find(([path, options]) => path === "/api/onboarding/complete" && options?.method === "POST");
    expect(JSON.parse(String(completeCall?.[1]?.body)).llm_api_key).toBe("super-secret-key");
  });

  it("uses Astryx fields, buttons, and banners on onboarding steps 1, 3, and 4", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/onboarding/test-llm": { ok: true, message: "连接成功" },
    });
    render(<Onboarding notify={vi.fn()} />);

    const source = await screen.findByLabelText("录播文件夹路径");
    expect(source.closest(".astryx-text-input")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查文件夹" })).toHaveClass("astryx-button");
    fireEvent.change(source, { target: { value: "/recordings" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));

    expect(await screen.findByText("选一个 AI 服务")).toBeVisible();
    expect(screen.getByLabelText("服务地址").closest(".astryx-text-input")).toBeInTheDocument();
    expect(document.querySelector(".onboarding-preset.astryx-selectable-card")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "phase-b-secret" } });
    expect(document.body.innerHTML).not.toContain("phase-b-secret");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));

    expect(await screen.findByText("确认设置")).toBeVisible();
    expect(document.querySelector(".astryx-list")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "完成设置" })).toHaveClass("astryx-button");
  });

  it("keeps both password values live but never serializes them across rerenders", async () => {
    const notify = vi.fn();
    const calls = installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/onboarding/test-llm": { ok: true, message: "连接成功" },
      "/api/onboarding/complete": { ok: true },
      "/api/service/start": { ok: false },
    });
    const { rerender } = render(<Onboarding notify={notify} />);

    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByLabelText(/云端识别/));
    const asrKey = screen.getByLabelText("识别 API key") as HTMLInputElement;
    const asrMarker = "dummy-asr-marker-v112";
    fireEvent.change(asrKey, { target: { value: asrMarker } });
    rerender(<Onboarding notify={notify} />);

    expect(asrKey.value === asrMarker).toBe(true);
    expect(asrKey.getAttribute("value") === null).toBe(true);
    expect(asrKey.defaultValue === "").toBe(true);
    expect(asrKey.outerHTML.includes(asrMarker)).toBe(false);
    expect(document.body.innerHTML.includes(asrMarker)).toBe(false);
    expect((document.body.textContent ?? "").includes(asrMarker)).toBe(false);
    expect(Object.values(asrKey.dataset).includes(asrMarker)).toBe(false);
    expect(asrKey.type).toBe("password");
    expect(screen.getByText("识别 API key").closest("label")?.control).toBe(asrKey);

    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(await screen.findByText("选一个 AI 服务")).toBeVisible();
    const llmKey = screen.getByLabelText("API key") as HTMLInputElement;
    const llmMarker = "dummy-llm-marker-v112";
    fireEvent.change(llmKey, { target: { value: llmMarker } });
    rerender(<Onboarding notify={notify} />);

    expect(llmKey.value === llmMarker).toBe(true);
    expect(llmKey.getAttribute("value") === null).toBe(true);
    expect(llmKey.defaultValue === "").toBe(true);
    expect(llmKey.outerHTML.includes(llmMarker)).toBe(false);
    expect(document.body.innerHTML.includes(llmMarker)).toBe(false);
    expect((document.body.textContent ?? "").includes(llmMarker)).toBe(false);
    expect(Object.values(llmKey.dataset).includes(llmMarker)).toBe(false);
    expect(llmKey.type).toBe("password");
    expect(screen.getByText("API key").closest("label")?.control).toBe(llmKey);

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("确认设置");
    fireEvent.click(screen.getByRole("button", { name: "完成设置" }));
    await screen.findByText("设置已保存，但自动化服务未启动，可进入主界面后手动启动");

    const llmTestCall = calls.find(([path]) => path === "/api/onboarding/test-llm");
    const completeCall = calls.find(([path]) => path === "/api/onboarding/complete");
    const llmTestBody = JSON.parse(String(llmTestCall?.[1]?.body));
    const completeBody = JSON.parse(String(completeCall?.[1]?.body));
    expect(llmTestBody.api_key === llmMarker).toBe(true);
    expect(completeBody.llm_api_key === llmMarker).toBe(true);
    expect(completeBody.asr_api_key === asrMarker).toBe(true);
    expect(calls.filter(([path, options]) => {
      if (!options?.body || ["/api/onboarding/test-llm", "/api/onboarding/complete"].includes(path)) return false;
      const body = String(options.body);
      return body.includes(asrMarker) || body.includes(llmMarker);
    })).toHaveLength(0);
  });

  it("keeps step 3 advance actionable and focuses the untested connection action", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选一个 AI 服务");
    const next = screen.getByRole("button", { name: "下一步" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(await screen.findByText("请先测试 AI 服务连接")).toBeVisible();
    expect(screen.getByRole("button", { name: "测试连接" })).toHaveFocus();
  });

  it("announces LLM testing in Chinese without exposing Astryx loading text", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/onboarding/test-llm": () => new Promise<Response>(() => undefined),
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选一个 AI 服务");

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    const button = await screen.findByRole("button", { name: "测试中…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("data-busy", "true");
    expect(button).not.toHaveAttribute("aria-busy");
    expect(screen.getAllByRole("status").filter(
      (status) => status.textContent === "正在测试 AI 服务连接",
    )).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });

  it("announces completion saving in Chinese without duplicate live regions", async () => {
    installFetchMock({
      "/api/onboarding": onboardingPayload(),
      "/api/onboarding/test-source": { ok: true },
      "/api/onboarding/test-llm": { ok: true, message: "连接成功" },
      "/api/onboarding/complete": () => new Promise<Response>(() => undefined),
    });
    render(<Onboarding notify={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下一步" }));
    await screen.findByText("选择语音识别方式");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("选一个 AI 服务");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByText("确认设置");

    fireEvent.click(screen.getByRole("button", { name: "完成设置" }));

    const button = await screen.findByRole("button", { name: "保存中…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("data-busy", "true");
    expect(button).not.toHaveAttribute("aria-busy");
    expect(screen.getAllByRole("status").filter(
      (status) => status.textContent === "正在保存设置",
    )).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });
});
