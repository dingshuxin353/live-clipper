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
    fireEvent.click(picker);
    await waitFor(() => expect(selectFolder).toHaveBeenCalledWith("选择录播文件夹"));
    expect(screen.getByLabelText("录播文件夹路径")).toHaveValue("");
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
    expect(await screen.findByText("请填写识别 API key")).toBeVisible();
    expect(screen.getByLabelText("识别 API key")).toHaveFocus();
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
});
