import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { Onboarding } from "../src/Onboarding";
import type { OnboardingDraft, OnboardingSession, OnboardingSnapshot } from "../src/project-dto";
import { installFetchMock as installBaseFetchMock, jsonResponse } from "./helpers";

const SESSION: OnboardingSession = { state: "in_progress", current_step: "welcome", revision: 1, draft: {}, pending_finish_request_id: null, failure: null, first_project: null };
const MODELS = [
  { id: "light", display_name: "Light", backend: "mlx", tier: "light", tier_label: "轻量", size_note: "187 MiB", ram_note: "内存较低", speed_note: "速度较快", accuracy_note: "适合轻量处理", recommended: false, state: "not_installed", state_reason: null, installed: false, downloading: false, job_id: null, installed_bytes: 0, partial_bytes: 0, bytes_downloaded: 0, bytes_total: 1000, download_source: "modelscope", current: false },
  { id: "balanced", display_name: "Balanced", backend: "mlx", tier: "balanced", tier_label: "平衡", size_note: "489 MiB", ram_note: "内存适中", speed_note: "速度均衡", accuracy_note: "兼顾精度", recommended: true, state: "installed", state_reason: null, installed: true, downloading: false, job_id: null, installed_bytes: 1000, partial_bytes: 0, bytes_downloaded: 1000, bytes_total: 1000, download_source: "modelscope", current: false },
  { id: "accurate", display_name: "Accurate", backend: "mlx", tier: "high_accuracy", tier_label: "高精度", size_note: "1.6 GiB", ram_note: "内存较高", speed_note: "处理较慢", accuracy_note: "精度优先", recommended: false, state: "damaged", state_reason: "hash", installed: false, downloading: false, job_id: null, installed_bytes: 0, partial_bytes: 300, bytes_downloaded: 300, bytes_total: 1000, download_source: "modelscope", current: false },
] as OnboardingSnapshot["model_catalog"];

function mergeDraft(current: OnboardingDraft, patch: OnboardingDraft): OnboardingDraft {
  const merged = { ...current };
  for (const section of ["asr", "ai", "project"] as const) {
    if (patch[section] !== undefined) merged[section] = { ...(current[section] ?? {}), ...patch[section] };
  }
  return merged;
}

function installFetchMock(overrides: Record<string, unknown> = {}, initialSession: OnboardingSession = SESSION) {
  let currentSession = initialSession;
  return installBaseFetchMock({
    "/api/onboarding/environment-check": { ok: true, environment: onboardingSnapshot().environment },
    "/api/onboarding/session": (options?: RequestInit) => {
      const body = JSON.parse(String(options?.body || "{}"));
      if (currentSession.state !== "in_progress" || body.current_step === "complete") {
        return jsonResponse({ ok: false, error: { code: "validation_failed", message: "首次设置草稿无效", fields: { current_step: "进行中的首次设置不能保存为完成步骤" } } }, 422);
      }
      if (body.expected_revision !== currentSession.revision) {
        return jsonResponse({ ok: false, error: { code: "onboarding_revision_conflict", message: "设置已在另一个窗口更新", fields: {} } }, 409);
      }
      const draft = mergeDraft(currentSession.draft, body.patch || {});
      const currentStep = body.current_step || currentSession.current_step;
      if (JSON.stringify(draft) !== JSON.stringify(currentSession.draft) || currentStep !== currentSession.current_step) {
        currentSession = { ...currentSession, revision: currentSession.revision + 1, current_step: currentStep, draft };
      }
      return jsonResponse({ ok: true, session: currentSession });
    },
    ...overrides,
  });
}

export function onboardingSnapshot(session: OnboardingSession = SESSION): OnboardingSnapshot {
  return { ok: true, entry: { mode: "onboarding", onboarding: session.state === "paused" ? "paused" : session.state === "activation_pending" ? "activation_pending" : "resume", reason_code: null, evidence_codes: [] }, session, environment: { status: "ready", checks: [{ name: "app_home", status: "ready", problem: null }, { name: "service_dir", status: "ready", problem: null }, { name: "workspace_root", status: "ready", problem: null }, { name: "sqlite", status: "ready", problem: null }, { name: "ffmpeg", status: "ready", problem: null }, { name: "ffprobe", status: "ready", problem: null }, { name: "asr_runtime", status: "ready", problem: null }, { name: "embedded_service", status: "ready", problem: null }] }, resources: { asr: { mode: "local", configured: false, ready: false, model_id: null, model_label: null, credential_present: false, problem: "尚未就绪" }, ai: { configured: false, ready: false, provider_label: null, model: null, credential_present: false, problem: "尚未就绪" } }, model_catalog: MODELS, initial_local_model: "balanced", provider_presets: [{ id: "deepseek", label: "DeepSeek", api_base: "https://api.example/v1", model: "chat" }, { id: "custom", label: "其他兼容服务", api_base: "", model: "" }], suggestions: { project_name: "直播录像精选", output_directory: "/output" } };
}

function renderOnboarding(snapshot = onboardingSnapshot(), handlers: Partial<React.ComponentProps<typeof Onboarding>> = {}) {
  const props = { snapshot, onSession: vi.fn(), onRefresh: vi.fn(async () => snapshot), onPaused: vi.fn(), onClose: vi.fn(), ...handlers };
  return { ...render(<MemoryRouter><Onboarding {...props} /></MemoryRouter>), props };
}

describe("five-step first-run setup", () => {
  it("renders the confirmed five-step structure and never exposes the workbench", () => {
    installFetchMock(); renderOnboarding();
    expect(screen.getByLabelText("首次设置步骤")).toBeVisible();
    for (const label of ["开始", "语音识别", "AI 服务", "第一个项目", "完成"]) expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getByRole("dialog", { name: "开始" })).toHaveAttribute("aria-modal", "true");
    expect(screen.queryByText("新建项目")).not.toBeInTheDocument();
  });

  it("blocks the first action on a real environment blocker and can recheck", async () => {
    const snapshot = onboardingSnapshot(); snapshot.environment = { status: "blocked", checks: [{ name: "app_home", status: "blocked", problem: "应用目录不可写" }] };
    let checks = 0;
    installFetchMock({ "/api/onboarding/environment-check": () => { checks += 1; return jsonResponse({ ok: true, environment: checks === 1 ? snapshot.environment : onboardingSnapshot().environment }); } }); renderOnboarding(snapshot);
    await waitFor(() => expect(checks).toBe(1)); expect(screen.getByRole("button", { name: "开始设置" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "开始设置" })).toBeEnabled());
  });

  it("debounces one latest draft save and persists cleared non-secret fields", async () => {
    const session = { ...SESSION, current_step: "project" as const, draft: { project: { name: "项目", source_directory: "/source", output_directory: "/output", trigger_mode: "manual" as const } } };
    const calls = installFetchMock(); renderOnboarding(onboardingSnapshot(session));
    const source = screen.getByLabelText(/录像目录/);
    fireEvent.change(source, { target: { value: "/source/next" } }); fireEvent.change(source, { target: { value: "" } });
    await waitFor(() => expect(calls.filter(([path]) => path === "/api/onboarding/session")).toHaveLength(1), { timeout: 1500 });
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/onboarding/session")?.[1]?.body));
    expect(body.patch.project.source_directory).toBe(""); expect(await screen.findByText("非密钥设置已保存。")).toBeVisible();
  });

  it("clears the failed save state after a revision conflict reload succeeds", async () => {
    const session = { ...SESSION, current_step: "project" as const, draft: { project: { name: "本地草稿", source_directory: "/source", output_directory: "/output", trigger_mode: "manual" as const } } };
    const latestSession = { ...session, revision: 4, draft: { project: { ...session.draft.project, name: "服务器草稿" } } }; const latest = onboardingSnapshot(latestSession);
    installFetchMock({ "/api/onboarding/session": () => jsonResponse({ ok: false, error: { code: "onboarding_revision_conflict", message: "设置已在另一个窗口更新", fields: {} } }, 409) });
    renderOnboarding(onboardingSnapshot(session), { onRefresh: vi.fn(async () => latest) }); fireEvent.change(screen.getByLabelText("项目名称"), { target: { value: "冲突修改" } });
    expect(await screen.findByText("自动保存失败，请处理后重试。", {}, { timeout: 1500 })).toBeVisible(); fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByDisplayValue("服务器草稿")).toBeVisible(); expect(screen.queryByText("设置已在另一个窗口更新")).not.toBeInTheDocument(); expect(screen.queryByText("自动保存失败，请处理后重试。")).not.toBeInTheDocument(); expect(screen.getByText("非密钥设置已保存。")).toBeVisible();
  });

  it("flushes the current step before pausing and keeps the overlay open on failure", async () => {
    const calls = installFetchMock({
      "/api/onboarding/session": { ok: true, session: { ...SESSION, revision: 2 } },
      "/api/onboarding/pause": new Error("offline"),
    });
    renderOnboarding(); const pauseButton = screen.getAllByRole("button", { name: "稍后继续" })[0]; await waitFor(() => expect(pauseButton).toBeEnabled()); fireEvent.click(pauseButton);
    expect(await screen.findByText("暂时无法保存进度")).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(calls.map(([path]) => path).filter((path) => ["/api/onboarding/session", "/api/onboarding/pause"].includes(path))).toEqual(["/api/onboarding/session", "/api/onboarding/pause"]);
  });

  it("selects the backend-recommended balanced model and commits an installed model", async () => {
    const asrSession = { ...SESSION, current_step: "asr" as const, draft: { asr: { mode: "local" as const, local_model_id: "balanced", model_source: "modelscope" } } };
    const committed = { ...asrSession, revision: 2, draft: asrSession.draft };
    const calls = installFetchMock({ "/api/onboarding/resources/asr/local": { ok: true, session: committed } }); renderOnboarding(onboardingSnapshot(asrSession));
    expect(screen.getByText("平衡").closest("button")).toHaveClass("selected");
    fireEvent.click(screen.getByRole("button", { name: "使用这个模型" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/onboarding/resources/asr/local")).toBe(true));
    expect(await screen.findByRole("button", { name: "已保存" })).toBeVisible();
  });

  it("renders only real byte progress with progressbar semantics", () => {
    const session = { ...SESSION, current_step: "asr" as const, draft: { asr: { mode: "local" as const, local_model_id: "light", model_source: "modelscope" } } };
    const snapshot = onboardingSnapshot(session); snapshot.model_catalog[0] = { ...snapshot.model_catalog[0], state: "downloading", downloading: true, job_id: "job-1", bytes_downloaded: 250 };
    installFetchMock({ "/api/jobs/job-1": () => new Promise<Response>(() => undefined) }); renderOnboarding(snapshot);
    const progress = screen.getByRole("progressbar", { name: "模型下载进度" });
    expect(progress).toHaveAttribute("aria-valuenow", "25"); expect(screen.getByText(/250 B \/ 1000 B/)).toBeVisible();
  });

  it("keeps cloud ASR key outside React state and clears it immediately after submission", async () => {
    const marker = "asr-secret-marker"; const session = { ...SESSION, current_step: "asr" as const, draft: { asr: { mode: "cloud" as const, api_base: "https://asr.example/v1", model: "speech" } } };
    const calls = installFetchMock({ "/api/onboarding/resources/asr/cloud": { ok: true, session: { ...session, revision: 2 } } }); renderOnboarding(onboardingSnapshot(session));
    const key = screen.getByLabelText("API key") as HTMLInputElement; fireEvent.input(key, { target: { value: marker } });
    expect(document.body.innerHTML).not.toContain(marker); fireEvent.click(screen.getByRole("button", { name: "测试并保存" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/onboarding/resources/asr/cloud")).toBe(true));
    const body = JSON.parse(String(calls.find(([path]) => path === "/api/onboarding/resources/asr/cloud")?.[1]?.body));
    expect(body.api_key).toBe(marker); expect(key).toHaveValue(""); expect(document.body.innerHTML).not.toContain(marker);
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0); expect(window.location.href).not.toContain(marker);
  });

  it("uses AI presets as prefill and invalidates success when a field changes", async () => {
    const session = { ...SESSION, current_step: "ai" as const, draft: { ai: { provider_id: "deepseek", api_base: "https://api.example/v1", model: "chat" } } };
    const snapshot = onboardingSnapshot(session); snapshot.resources.ai = { configured: true, ready: true, credential_present: true, provider_label: "DeepSeek", model: "chat", problem: null };
    installFetchMock(); renderOnboarding(snapshot); expect(screen.getByText("AI 服务连接成功")).toBeVisible();
    fireEvent.change(screen.getByLabelText("模型"), { target: { value: "new-model" } });
    await waitFor(() => expect(screen.getByText("连接尚未验证")).toBeVisible());
  });

  it("derives the untouched project name from the selected source and performs final validation", async () => {
    const selectFolder = vi.fn(async () => "/recordings/interviews"); window.liveClipperShell = { selectFolder };
    const session = { ...SESSION, current_step: "project" as const, draft: { project: { trigger_mode: "manual" as const, schedule_mode: "daily" as const, daily_time: "22:00", interval_minutes: 60, output_directory: "/output" } } };
    const calls = installFetchMock({ "/api/onboarding/project/validate": { ok: true, valid: true, fatal: [], blockers: [], warnings: [], checks: { asr: { ready: true }, ai: { ready: true }, source_directory: { status: "ready" }, output_directory: { status: "creatable" } }, summary: { recording_source: "/recordings/interviews", discovery: "new_only", processing: "ai_auto", output: "/output" }, existing_video_count: 2, normalized_config: {} } }, session); renderOnboarding(onboardingSnapshot(session));
    fireEvent.click(screen.getAllByRole("button", { name: "选择…" })[0]);
    expect(await screen.findByDisplayValue("interviews")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "检查配置" }));
    expect(await screen.findByText("项目可以创建并启用。")).toBeVisible();
    expect(screen.getByText("目录可读 · 发现 2 个已有录像，默认不会自动处理")).toBeVisible();
    const sequence = calls.filter(([path]) => ["/api/onboarding/session", "/api/onboarding/project/validate"].includes(path));
    expect(sequence.map(([path]) => path)).toEqual(["/api/onboarding/session", "/api/onboarding/project/validate"]);
    expect(JSON.parse(String(sequence[0][1]?.body)).current_step).toBe("project");
    expect(calls.filter(([path]) => path === "/api/onboarding/session").every(([, options]) => JSON.parse(String(options?.body)).current_step !== "complete")).toBe(true);
  });

  it("reuses one finish request identity across a failed retry", async () => {
    const session = { ...SESSION, current_step: "project" as const, revision: 3, draft: { project: { name: "项目", source_directory: "/source", trigger_mode: "manual" as const, schedule_mode: "daily" as const, daily_time: "22:00", interval_minutes: 60, output_directory: "/output" } } };
    let attempts = 0; const calls = installFetchMock({
      "/api/onboarding/project/validate": { ok: true, valid: true, fatal: [], blockers: [], warnings: [], checks: { asr: { ready: true }, ai: { ready: true }, source_directory: { status: "ready" }, output_directory: { status: "ready" } }, summary: { recording_source: "/source", discovery: "new_only", processing: "ai_auto", output: "/output" }, existing_video_count: 0, normalized_config: {} },
      "/api/onboarding/finish": () => { attempts += 1; return attempts === 1 ? Promise.reject(new Error("offline")) : jsonResponse({ ok: true, session: { ...session, state: "completed", current_step: "complete", first_project: { project_id: "p1", name: "项目", activation_state: "active", readiness_state: "ready" } } }, 201); },
    }, session);
    renderOnboarding(onboardingSnapshot(session)); fireEvent.click(screen.getByRole("button", { name: "检查配置" })); await screen.findByText("项目可以创建并启用。");
    fireEvent.click(screen.getByRole("button", { name: "完成设置并创建项目" })); expect(await screen.findByText("创建结果暂时无法确认，请保持当前窗口后重试")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "完成设置并创建项目" })); await screen.findByText("项目 已创建并启用");
    const ids = calls.filter(([path]) => path === "/api/onboarding/finish").map(([, options]) => JSON.parse(String(options?.body)).request_id); expect(new Set(ids).size).toBe(1);
    const patches = calls.filter(([path]) => path === "/api/onboarding/session").map(([, options]) => JSON.parse(String(options?.body)));
    expect(patches.every((body) => body.current_step === "project")).toBe(true);
  });

  it("accepts activation pending with complete only from the finish response", async () => {
    const session = { ...SESSION, current_step: "project" as const, revision: 3, draft: { project: { name: "项目", source_directory: "/source", trigger_mode: "manual" as const, schedule_mode: "daily" as const, daily_time: "22:00", interval_minutes: 60, output_directory: "/output" } } };
    const pending = { ...session, state: "activation_pending" as const, current_step: "complete" as const, pending_finish_request_id: "finish-1", failure: { code: "service_not_ready", summary: "服务未启动" }, first_project: { project_id: "p1", name: "项目", activation_state: "active" as const, readiness_state: "blocked" } };
    const calls = installFetchMock({
      "/api/onboarding/project/validate": { ok: true, valid: true, fatal: [], blockers: [], warnings: [], checks: { asr: { ready: true }, ai: { ready: true }, source_directory: { status: "ready" }, output_directory: { status: "ready" } }, summary: { recording_source: "/source", discovery: "new_only", processing: "ai_auto", output: "/output" }, existing_video_count: 0, normalized_config: {} },
      "/api/onboarding/finish": () => jsonResponse({ ok: true, session: pending }, 202),
    }, session);
    renderOnboarding(onboardingSnapshot(session)); fireEvent.click(screen.getByRole("button", { name: "检查配置" })); await screen.findByText("项目可以创建并启用。");
    fireEvent.click(screen.getByRole("button", { name: "完成设置并创建项目" })); expect(await screen.findByText("项目已保存，本机服务尚未启动")).toBeVisible();
    expect(calls.filter(([path]) => path === "/api/onboarding/session").every(([, options]) => JSON.parse(String(options?.body)).current_step === "project")).toBe(true);
  });

  it("activation pending exposes retry only and never sends finish", async () => {
    const session = { ...SESSION, state: "activation_pending" as const, current_step: "complete" as const, pending_finish_request_id: "finish-1", failure: { code: "service_not_ready", summary: "服务未启动" }, first_project: { project_id: "p1", name: "项目", activation_state: "active" as const, readiness_state: "blocked" } };
    const calls = installFetchMock({ "/api/onboarding/service/retry": { ok: true, session: { ...session, state: "completed", failure: null } } }); renderOnboarding(onboardingSnapshot(session));
    expect(screen.queryByRole("button", { name: /创建项目/ })).not.toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "重新启动服务" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/onboarding/service/retry")).toBe(true)); expect(calls.some(([path]) => path === "/api/onboarding/finish")).toBe(false);
  });

  it("completed offers one selectable relative source file and submits selected scan", async () => {
    const session = { ...SESSION, state: "completed" as const, current_step: "complete" as const, first_project: { project_id: "p1", name: "项目", activation_state: "active" as const, readiness_state: "ready" }, draft: { project: { source_directory: "/secret/source", output_directory: "/output", trigger_mode: "manual" as const } } };
    const calls = installFetchMock({ "/api/projects/p1/source-files": { ok: true, files: [{ relative_path: "ready.mp4", bytes: 10, modified_at: "now", selectable: true, reason: null }, { relative_path: "writing.mp4", bytes: 10, modified_at: "now", selectable: false, reason: "writing" }] }, "/api/projects/p1/scans": { ok: true, scan: { scan_id: "s1", project_id: "p1", status: "success" } } }); renderOnboarding(onboardingSnapshot(session));
    fireEvent.click(await screen.findByRole("button", { name: "选择一条录像试运行" })); const dialog = screen.getByRole("dialog", { name: "选择一条录像试运行" });
    expect(within(dialog).getByText("ready.mp4")).toBeVisible(); expect(within(dialog).queryByText("writing.mp4")).not.toBeInTheDocument(); expect(dialog).not.toHaveTextContent("/secret/source");
    fireEvent.click(within(dialog).getByRole("radio")); fireEvent.click(within(dialog).getByRole("button", { name: "用这条录像试运行" }));
    await waitFor(() => expect(calls.some(([path]) => path === "/api/projects/p1/scans")).toBe(true)); const body = JSON.parse(String(calls.find(([path]) => path === "/api/projects/p1/scans")?.[1]?.body)); expect(body.selected_relative_paths).toEqual(["ready.mp4"]);
  });
});
