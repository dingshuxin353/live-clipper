import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../src/App";
import { formatLocalTime } from "../src/ui/presentation";
import { installFetchMock, MODELS } from "./helpers";

describe("Astryx settings migration", () => {
  it("maps every one of the 47 config fields to an Astryx form control", async () => {
    installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
    const controls = [...document.querySelectorAll<HTMLElement>("[data-config-field]")];
    expect(controls).toHaveLength(47);
    expect(controls.every((control) => control.closest(
      ".astryx-text-input, .astryx-number-input, .astryx-selector, .astryx-checkbox-input",
    ))).toBe(true);
    expect(document.querySelector(".astryx-form-layout")).toBeInTheDocument();
  });

  it("keeps SettingsSection content spanning the outer settings grid", async () => {
    installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));

    const fileSection = screen.getByRole("heading", { name: "文件位置" }).closest(".settings-section");
    const modelSection = screen.getByRole("heading", { name: "本地语音模型" }).closest(".settings-section");
    expect(fileSection?.querySelector(":scope > .settings-field-grid")).toBeInTheDocument();
    expect(modelSection?.querySelector(":scope > .field-note")).toBeInTheDocument();
  });

  it("renders ordinary settings guidance as non-live supporting text without banners", async () => {
    installFetchMock();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));

    for (const message of [
      "配好三件事就能用",
      "自动化引擎随 App 运行",
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

  it("uses an Astryx AlertDialog instead of window.confirm for restoring defaults", async () => {
    installFetchMock();
    const confirmSpy = vi.spyOn(window, "confirm");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
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
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /设置/ }));
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
});
