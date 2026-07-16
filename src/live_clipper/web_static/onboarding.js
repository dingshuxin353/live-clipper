(() => {
  const el = (id) => document.getElementById(id);

  if (window.liveClipperShell) {
    document.body.classList.add("in-app-shell");
  }
  const wizard = {
    presets: [],
    presetId: "deepseek",
    sourceDir: "",
    sourceOk: false,
    llmOk: false,
  };

  async function fetchJson(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    return response.json();
  }

  function showResult(id, ok, message) {
    const node = el(id);
    node.hidden = false;
    node.classList.toggle("ok", ok);
    node.classList.toggle("error", !ok);
    node.textContent = message;
  }

  function showStep(step) {
    document.querySelectorAll(".onboarding-step").forEach((section) => {
      section.hidden = section.dataset.step !== String(step);
    });
    document.querySelectorAll(".onboarding-step-dot").forEach((dot) => {
      dot.classList.toggle("active", dot.dataset.stepDot === String(step));
    });
  }

  function applyPreset(presetId) {
    wizard.presetId = presetId;
    const preset = wizard.presets.find((item) => item.id === presetId);
    if (!preset) return;
    const isCustom = preset.id === "custom";
    el("onboardingLlmBase").value = preset.api_base;
    el("onboardingLlmModel").value = preset.model;
    el("onboardingLlmBase").readOnly = !isCustom;
    el("onboardingLlmModel").readOnly = !isCustom;
    wizard.llmOk = false;
    el("onboardingToStep3Btn").disabled = true;
    el("onboardingLlmResult").hidden = true;
    document.querySelectorAll("[data-preset-id]").forEach((button) => {
      button.classList.toggle("active", button.dataset.presetId === presetId);
    });
  }

  function renderPresets() {
    el("onboardingPresets").innerHTML = wizard.presets
      .map(
        (preset) => `
        <button type="button" class="onboarding-preset" data-preset-id="${preset.id}">
          <strong>${preset.label}</strong>
          ${preset.signup_url ? `<a href="${preset.signup_url}" target="_blank" rel="noopener">注册领取 key ↗</a>` : ""}
        </button>`
      )
      .join("");
    document.querySelectorAll("[data-preset-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        if (event.target.closest("a")) return;
        applyPreset(button.dataset.presetId);
      });
    });
    applyPreset(wizard.presetId);
  }

  function renderSummary() {
    el("onboardingSummary").innerHTML = [
      `<div><span>录播文件夹</span><strong>${wizard.sourceDir}</strong></div>`,
      `<div><span>AI 服务</span><strong>${el("onboardingLlmBase").value}</strong></div>`,
      `<div><span>模型</span><strong>${el("onboardingLlmModel").value}</strong></div>`,
      `<div><span>API key</span><strong>${el("onboardingLlmKey").value ? "已填写（只保存在本机 .env）" : "未填写"}</strong></div>`,
    ].join("");
  }

  async function testSource() {
    const button = el("onboardingSourceTestBtn");
    button.disabled = true;
    try {
      const payload = await fetchJson("/api/onboarding/test-source", {
        method: "POST",
        body: JSON.stringify({ source_dir: el("onboardingSourceDir").value }),
      });
      wizard.sourceOk = payload.ok === true;
      if (wizard.sourceOk) {
        wizard.sourceDir = payload.path;
        showResult("onboardingSourceResult", true, `找到了！该文件夹里有 ${payload.video_count} 个视频文件。`);
      } else {
        showResult("onboardingSourceResult", false, payload.message || "检查失败");
      }
      el("onboardingToStep2Btn").disabled = !wizard.sourceOk;
    } finally {
      button.disabled = false;
    }
  }

  async function testLlm() {
    const button = el("onboardingLlmTestBtn");
    button.disabled = true;
    button.textContent = "测试中…";
    try {
      const payload = await fetchJson("/api/onboarding/test-llm", {
        method: "POST",
        body: JSON.stringify({
          api_base: el("onboardingLlmBase").value,
          api_key: el("onboardingLlmKey").value,
          model: el("onboardingLlmModel").value,
        }),
      });
      wizard.llmOk = payload.ok === true;
      showResult("onboardingLlmResult", wizard.llmOk, payload.message || (wizard.llmOk ? "连接成功" : "测试失败"));
      el("onboardingToStep3Btn").disabled = !wizard.llmOk;
    } finally {
      button.disabled = false;
      button.textContent = "测试连接";
    }
  }

  async function complete() {
    const button = el("onboardingCompleteBtn");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const payload = await fetchJson("/api/onboarding/complete", {
        method: "POST",
        body: JSON.stringify({
          source_dir: wizard.sourceDir,
          llm_api_base: el("onboardingLlmBase").value,
          llm_model: el("onboardingLlmModel").value,
          llm_api_key: el("onboardingLlmKey").value,
        }),
      });
      if (payload.ok !== true) {
        showResult("onboardingCompleteResult", false, payload.message || "保存失败");
        return;
      }
      await fetchJson("/api/service/start", { method: "POST", body: "{}" }).catch(() => {});
      window.location.reload();
    } finally {
      button.disabled = false;
      button.textContent = "完成设置";
    }
  }

  async function init() {
    let status;
    try {
      status = await fetchJson("/api/onboarding");
    } catch (_error) {
      return;
    }
    if (!status || status.needs_onboarding !== true) return;
    wizard.presets = status.presets || [];
    renderPresets();
    el("onboardingOverlay").hidden = false;
    showStep(1);

    const browseBtn = el("onboardingBrowseBtn");
    if (window.liveClipperShell) {
      browseBtn.hidden = false;
      browseBtn.addEventListener("click", async () => {
        const folder = await window.liveClipperShell.selectFolder("选择录播文件夹");
        if (!folder) return;
        el("onboardingSourceDir").value = folder;
        testSource().catch(() => {});
      });
    }
    el("onboardingSourceTestBtn").addEventListener("click", () => testSource().catch(() => {}));
    el("onboardingLlmTestBtn").addEventListener("click", () => testLlm().catch(() => {}));
    el("onboardingCompleteBtn").addEventListener("click", () => complete().catch(() => {}));
    el("onboardingToStep2Btn").addEventListener("click", () => showStep(2));
    el("onboardingToStep3Btn").addEventListener("click", () => {
      renderSummary();
      showStep(3);
    });
    el("onboardingBackTo1Btn").addEventListener("click", () => showStep(1));
    el("onboardingBackTo2Btn").addEventListener("click", () => showStep(2));
    el("onboardingSkipBtn").addEventListener("click", () => {
      el("onboardingOverlay").hidden = true;
    });
  }

  init();
})();
