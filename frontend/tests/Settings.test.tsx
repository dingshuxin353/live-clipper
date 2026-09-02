import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { BrowserRouter, MemoryRouter } from "react-router-dom";

import { Settings } from "../src/Settings";
import { defaultConfig } from "../src/config";
import { formatLocalTime } from "../src/ui/presentation";
import { installFetchMock, MODELS } from "./helpers";

function renderSettings(models = MODELS, entry = "/settings") {
  return render(<MemoryRouter initialEntries={[entry]}><Settings
    configPayload={{ ok: true, config: defaultConfig(), config_path: "live-clipper.toml", exists: true, env_status: {}, warnings: [] }}
    service={{ status: "stopped" }}
    scheduler={{ scheduler: { enabled: true } }}
    reviewAutomation={{ review_automation: { enabled: false }, environment: {} }}
    models={models}
    reloadConfig={vi.fn(async () => undefined)}
    refreshModels={vi.fn(async () => undefined)}
    refreshAll={vi.fn(async () => undefined)}
    notify={vi.fn()}
    schedulerDraft={null}
  /></MemoryRouter>);
}

describe("Astryx settings migration", () => {
  it("maps the 43 active non-secret config fields to an Astryx form control", async () => {
    installFetchMock();
    renderSettings();
    const controls = [...document.querySelectorAll<HTMLElement>("[data-config-field]")];
    expect(controls).toHaveLength(43);
    expect(document.querySelector('[data-config-field="service.enabled"]')).not.toBeInTheDocument();
    expect(document.querySelector('[data-config-field="service.scan_interval_minutes"]')).not.toBeInTheDocument();
    expect(document.querySelector('[data-config-field="review_automation.auto_render_after_selection"]')).not.toBeInTheDocument();
    expect(controls.every((control) => control.closest(
      ".astryx-text-input, .astryx-number-input, .astryx-selector, .astryx-checkbox-input",
    ))).toBe(true);
    expect(document.querySelector(".astryx-form-layout")).toBeInTheDocument();
  });

  it("keeps SettingsSection content spanning the outer settings grid", async () => {
    installFetchMock();
    renderSettings();

    const fileSection = screen.getByRole("heading", { name: "文件位置" }).closest(".settings-section");
    const modelSection = screen.getByRole("heading", { name: "本地语音模型" }).closest(".settings-section");
    expect(fileSection?.querySelector(":scope > .settings-field-grid")).toBeInTheDocument();
    expect(modelSection?.querySelector(":scope > .field-note")).toBeInTheDocument();
  });

  it("renders ordinary settings guidance as non-live supporting text without banners", async () => {
    installFetchMock();
    renderSettings();

    for (const message of [
      "常用设置",
      "自动处理随 Venus 运行",
      "高级定时任务",
      "修改 Web host/port 后，需要手动重启 Web 控制台命令本身才会生效。",
      "Web access token：未配置",
    ]) {
      const node = screen.getByText(message);
      expect(node).not.toHaveAttribute("role", "alert");
      expect(node).not.toHaveAttribute("role", "status");
    }
    expect(document.querySelector(".astryx-banner")).not.toBeInTheDocument();
  });

  it("distinguishes configuration health states with semantic card tones", async () => {
    installFetchMock();
    renderSettings();

    const health = document.getElementById("configHealth");
    expect(health).not.toBeNull();
    const cards = [...(health as HTMLElement).querySelectorAll<HTMLElement>(".health-card")];
    expect(cards.map((card) => card.dataset.tone)).toEqual([
      "warning", "success", "warning", "warning", "neutral", "success", "neutral",
    ]);
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual([
      "录播源：未配置",
      "本地项目库：正常",
      "AI 服务：未配置",
      "语音识别：未配置",
      "服务：未运行",
      "定时任务：已启用",
      "AI 审阅：未启用",
    ]);
  });

  it("marks the legacy time-window setting as informational only", async () => {
    installFetchMock();
    renderSettings();
    fireEvent.click(screen.getByText("高级设置（一般不需要改）"));

    const legacyField = screen.getByLabelText("历史扫描时间范围（仅兼容旧配置）");
    expect(legacyField).toHaveAttribute("readonly");
    expect(screen.getByText("不再按录像日期过滤，所有稳定且未处理的录像都会进入队列。")).toBeVisible();
  });

  it("uses an Astryx AlertDialog instead of window.confirm for restoring defaults", async () => {
    installFetchMock();
    const confirmSpy = vi.spyOn(window, "confirm");
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("恢复默认配置");
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("closes model deletion immediately and submits exactly one request", async () => {
    const models = [
      { ...MODELS[0] },
      { ...MODELS[1], state: "installed", current: false },
      { ...MODELS[2] },
    ];
    const calls = installFetchMock({
      "/api/asr/models": { ok: true, models, download_source: "modelscope" },
      "/api/asr/models/delete": () => new Promise<Response>(() => undefined),
    });
    renderSettings(models);
    const medium = (await screen.findByText("Medium")).closest(".asr-model-row") as HTMLElement;
    fireEvent.click(within(medium).getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(calls.filter(([path, options]) => path === "/api/asr/models/delete" && options?.method === "POST")).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(/\bLoading\b/);
  });

  it("formats API timestamps as compact local time while preserving invalid values", () => {
    expect(formatLocalTime("2026-07-30T06:15:00Z")).toMatch(
      /^2026-07-30 \d{2}:15$/,
    );
    expect(formatLocalTime("not-a-time")).toBe("not-a-time");
  });

  it("accepts only an app-controlled Run return and returns after saving without reprocessing", async () => {
    const calls = installFetchMock();
    window.history.replaceState({}, "", "/settings?returnTo=%2Fprojects%2Fproject-1%2Fruns%2Frun-origin%3Freprocess%3D1");
    render(<BrowserRouter><Settings
      configPayload={{ ok: true, config: defaultConfig(), config_path: "live-clipper.toml", exists: true, env_status: {}, warnings: [] }}
      service={{ status: "stopped" }} scheduler={{ scheduler: { enabled: true } }} reviewAutomation={{ review_automation: { enabled: false }, environment: {} }} models={MODELS}
      reloadConfig={vi.fn(async () => undefined)} refreshModels={vi.fn(async () => undefined)} refreshAll={vi.fn(async () => undefined)} notify={vi.fn()} schedulerDraft={null}
    /></BrowserRouter>);
    fireEvent.click(screen.getByRole("button", { name: "保存并返回原记录" }));
    await waitFor(() => expect(window.location.pathname).toBe("/projects/project-1/runs/run-origin"));
    expect(window.location.search).toBe("?reprocess=1");
    expect(calls.some(([path]) => path.includes("/reprocess"))).toBe(false);
  });

  it("rejects an external settings return target", () => {
    installFetchMock();
    renderSettings(MODELS, "/settings?returnTo=https%3A%2F%2Fevil.example");
    expect(screen.queryByRole("button", { name: "保存并返回原记录" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeVisible();
  });
});
