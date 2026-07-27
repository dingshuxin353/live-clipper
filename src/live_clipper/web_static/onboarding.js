(() => {
  "use strict";

  const el = (id) => document.getElementById(id);
  const wizard = {
    step: 1,
    presets: [],
    presetId: "deepseek",
    sourceOk: false,
    asrMode: "local",
    modelSource: "modelscope",
    models: [],
    modelsInitialized: false,
    initialLocalModel: "",
    selectedModelId: "",
    activeJobId: null,
    downloadActive: false,
    downloadFailure: "",
    asrReady: false,
    llmOk: false,
    completed: false,
    skipSubmitting: false,
    pollTimer: null,
    polling: false,
  };

  async function fetchJson(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const body = await response.json();
    return body;
  }

  function showResult(id, ok, message) {
    const target = el(id);
    target.hidden = false;
    target.classList.toggle("ok", Boolean(ok));
    target.classList.toggle("error", !ok);
    target.textContent = message;
  }

  function showStep(step) {
    wizard.step = step;
    document.querySelectorAll(".onboarding-step").forEach((section) => {
      section.hidden = Number(section.dataset.step) !== step;
    });
    document.querySelectorAll("[data-step-dot]").forEach((dot) => {
      dot.classList.toggle("active", Number(dot.dataset.stepDot) === step);
    });
    if (wizard.activeJobId && !wizard.pollTimer && !wizard.polling) pollJob(wizard.activeJobId);
    if (step === 4) updateCompletionState();
  }

  function applyPreset(presetId) {
    const preset = wizard.presets.find((item) => item.id === presetId);
    if (!preset) return;
    wizard.presetId = presetId;
    document.querySelectorAll("[data-onboarding-preset]").forEach((button) => {
      button.classList.toggle("active", button.dataset.onboardingPreset === presetId);
    });
    if (presetId !== "custom") {
      el("onboardingLlmBase").value = preset.api_base;
      el("onboardingLlmModel").value = preset.model;
    }
    invalidateLlm();
  }

  function renderPresets() {
    const container = el("onboardingPresets");
    container.replaceChildren();
    wizard.presets.forEach((preset) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "onboarding-preset";
      button.dataset.onboardingPreset = preset.id;
      const label = document.createElement("strong");
      label.textContent = preset.label;
      button.append(label);
      if (preset.signup_url) {
        const link = document.createElement("a");
        link.href = preset.signup_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "获取 API key";
        link.addEventListener("click", (event) => event.stopPropagation());
        button.append(link);
      }
      button.addEventListener("click", () => applyPreset(preset.id));
      container.append(button);
    });
    applyPreset(wizard.presetId);
  }

  function selectedModel() {
    return wizard.models.find((model) => model.id === wizard.selectedModelId);
  }

  function anyModelDownloading() {
    return wizard.models.find((model) => model.downloading || model.state === "downloading");
  }

  function selectedModelHasActiveDownload() {
    const model = selectedModel();
    return Boolean(
      model
      && wizard.downloadActive
      && wizard.activeJobId
      && model.job_id === wizard.activeJobId
      && (model.downloading || model.state === "downloading"),
    );
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function modelStateLabel(model) {
    if (model.state === "installed") return "已就绪";
    if (model.state === "downloading") return "下载中";
    if (model.state === "damaged") return "需要修复";
    if (Number(model.partial_bytes || 0) > 0) return "可继续下载";
    return "未下载";
  }

  function renderModelCards() {
    const container = el("onboardingAsrModels");
    container.replaceChildren();
    container.className = "onboarding-models onboarding-model-grid";
    wizard.models.forEach((model) => {
      const card = document.createElement("article");
      card.className = "onboarding-model-card";
      card.dataset.onboardingModelId = model.id;
      card.classList.toggle("selected", model.id === wizard.selectedModelId);
      card.classList.toggle("installed", model.state === "installed");
      card.classList.toggle("downloading", model.state === "downloading");
      card.classList.toggle("error", model.state === "damaged");

      const select = document.createElement("button");
      select.type = "button";
      select.className = "onboarding-model-select";
      select.disabled = wizard.downloadActive;
      const name = document.createElement("strong");
      name.textContent = model.display_name;
      const details = document.createElement("span");
      details.textContent = `${model.tier_label} · ${model.size_note}`;
      const source = document.createElement("span");
      source.textContent = `将使用 ${el("onboardingAsrSource").selectedOptions[0]?.textContent || wizard.modelSource}`;
      const state = document.createElement("span");
      state.className = "onboarding-model-state";
      state.textContent = modelStateLabel(model);
      select.append(name, details, source, state);
      select.addEventListener("click", () => {
        if (wizard.downloadActive) return;
        wizard.selectedModelId = model.id;
        renderModelCards();
        updateAsrState();
      });
      card.append(select);

      if (model.state !== "installed") {
        const download = document.createElement("button");
        download.type = "button";
        download.className = "secondary-button onboarding-model-download";
        download.dataset.onboardingModelAction = "download";
        download.disabled = wizard.downloadActive;
        if (model.state === "damaged") {
          download.textContent = "修复并使用";
        } else if (Number(model.partial_bytes || 0) > 0) {
          download.textContent = "继续下载";
        } else {
          download.textContent = "下载并使用";
        }
        download.addEventListener("click", () => startModelDownload(model.id));
        card.append(download);
      }
      container.append(card);
    });
    updateAsrState();
    if (wizard.step === 4) updateCompletionState();
  }

  function updateAsrState() {
    const local = wizard.asrMode === "local";
    el("onboardingAsrLocal").checked = local;
    el("onboardingAsrCloud").checked = !local;
    el("onboardingAsrLocalPanel").hidden = !local;
    el("onboardingAsrCloudPanel").hidden = local;
    el("onboardingAsrLocal").disabled = wizard.downloadActive;
    el("onboardingAsrCloud").disabled = wizard.downloadActive;
    el("onboardingAsrSource").disabled = wizard.downloadActive;
    document.querySelectorAll("[data-onboarding-model-id] button").forEach((button) => {
      button.disabled = wizard.downloadActive;
    });
    const model = selectedModel();
    wizard.asrReady = Boolean(model && model.state === "installed");
    const cloudReady = ["onboardingAsrBase", "onboardingAsrModel", "onboardingAsrKey"].every(
      (id) => el(id).value.trim(),
    );
    const localCanAdvance = wizard.asrReady || selectedModelHasActiveDownload();
    el("onboardingToStep3Btn").disabled = local ? !localCanAdvance : !cloudReady;
  }

  async function refreshModels() {
    const payload = await fetchJson("/api/asr/models");
    if (!payload.ok || !Array.isArray(payload.models)) throw new Error(payload.message || "无法读取本地模型");
    if (payload.models.length !== 3 || !payload.models.some((model) => model.id === wizard.initialLocalModel)) {
      throw new Error("本地模型列表与当前版本不匹配");
    }
    wizard.models = payload.models;
    const active = anyModelDownloading();
    if (!wizard.modelsInitialized) {
      const current = wizard.models.find((model) => model.current);
      wizard.selectedModelId = active ? active.id : current ? current.id : wizard.initialLocalModel;
      if (["modelscope", "huggingface"].includes(payload.download_source)) {
        wizard.modelSource = payload.download_source;
        el("onboardingAsrSource").value = wizard.modelSource;
      }
      wizard.modelsInitialized = true;
    } else if (!wizard.models.some((model) => model.id === wizard.selectedModelId)) {
      wizard.selectedModelId = wizard.initialLocalModel;
    }
    if (active) wizard.selectedModelId = active.id;
    wizard.downloadActive = Boolean(active);
    wizard.activeJobId = active ? active.job_id : null;
    renderModelCards();
    if (wizard.activeJobId && !wizard.pollTimer && !wizard.polling) pollJob(wizard.activeJobId);
    return payload;
  }

  function showDownloadProgress(model, job) {
    const progress = el("onboardingAsrProgress");
    progress.hidden = false;
    const downloaded = Number(job.bytes_downloaded ?? model?.bytes_downloaded ?? model?.partial_bytes ?? 0);
    const total = Number(job.bytes_total ?? model?.bytes_total ?? 0);
    const suffix = total > 0 ? ` / ${formatBytes(total)}` : "";
    const percent = total > 0 ? `（${Math.min(100, Math.round((downloaded / total) * 100))}%）` : "";
    progress.textContent = `正在下载 ${model?.display_name || "模型"}：${formatBytes(downloaded)}${suffix}${percent}`;
  }

  async function pollJob(jobId) {
    if (!jobId || wizard.polling) return;
    wizard.polling = true;
    wizard.activeJobId = jobId;
    wizard.downloadActive = true;
    updateAsrState();
    try {
      const jobPayload = await fetchJson("/api/jobs/" + encodeURIComponent(jobId));
      const job = jobPayload.job || jobPayload;
      await refreshModels();
      showDownloadProgress(selectedModel(), job);
      if (job.status === "failed") {
        wizard.downloadFailure = job.error || job.message || "模型下载失败，可稍后继续";
        wizard.downloadActive = false;
        wizard.activeJobId = null;
        wizard.pollTimer = null;
        wizard.polling = false;
        showResult("onboardingAsrResult", false, wizard.downloadFailure);
        renderModelCards();
        return;
      }
      if (job.status === "interrupted") {
        wizard.downloadFailure = "模型下载已中断，已保留进度，可继续下载";
        wizard.downloadActive = false;
        wizard.activeJobId = null;
        wizard.pollTimer = null;
        wizard.polling = false;
        showResult("onboardingAsrResult", false, wizard.downloadFailure);
        renderModelCards();
        return;
      }
      if (job.status === "succeeded") {
        wizard.downloadActive = false;
        wizard.activeJobId = null;
        wizard.pollTimer = null;
        wizard.polling = false;
        await refreshModels();
        el("onboardingAsrProgress").hidden = true;
        if (selectedModel()?.state === "installed") {
          wizard.downloadFailure = "";
          showResult("onboardingAsrResult", true, "模型已安装，可以继续");
        } else {
          wizard.downloadFailure = "下载完成，但模型完整性验证未通过";
          showResult("onboardingAsrResult", false, wizard.downloadFailure);
        }
        return;
      }
      wizard.pollTimer = window.setTimeout(() => {
        wizard.pollTimer = null;
        pollJob(jobId);
      }, 1000);
      wizard.polling = false;
    } catch (error) {
      wizard.pollTimer = window.setTimeout(() => {
        wizard.pollTimer = null;
        pollJob(jobId);
      }, 1500);
      wizard.polling = false;
      showResult("onboardingAsrResult", false, `暂时无法读取下载进度：${error.message}`);
    }
  }

  async function startModelDownload(modelId) {
    if (wizard.downloadActive) {
      showResult("onboardingAsrResult", false, "已有模型正在下载，请等待完成");
      return;
    }
    wizard.selectedModelId = modelId;
    try {
      await refreshModels();
      const model = selectedModel();
      if (!model) throw new Error("所选模型不存在");
      if (model.state === "installed") return;
      const active = anyModelDownloading();
      if (active) {
        showResult("onboardingAsrResult", false, "已有模型正在下载，请等待完成");
        pollJob(active.job_id);
        return;
      }
      wizard.downloadActive = true;
      wizard.downloadFailure = "";
      renderModelCards();
      const payload = await fetchJson("/api/asr/models/download", {
        method: "POST",
        body: JSON.stringify({ model: modelId, source: wizard.modelSource }),
      });
      if (!payload.ok) throw new Error(payload.message || "无法开始下载");
      const jobId = payload.job?.id || payload.job_id;
      if (!jobId) throw new Error("下载任务未返回 job id");
      wizard.activeJobId = jobId;
      showResult("onboardingAsrResult", true, "已开始下载，离开本步骤不会取消任务");
      pollJob(jobId);
    } catch (error) {
      wizard.downloadFailure = error.message;
      wizard.downloadActive = false;
      wizard.activeJobId = null;
      renderModelCards();
      showResult("onboardingAsrResult", false, error.message);
    }
  }

  function invalidateSource() {
    wizard.sourceOk = false;
    el("onboardingSourceResult").hidden = true;
  }

  function invalidateLlm() {
    wizard.llmOk = false;
    el("onboardingToStep4Btn").disabled = true;
    el("onboardingLlmResult").hidden = true;
  }

  function appendSummaryRow(label, value) {
    const row = document.createElement("div");
    const name = document.createElement("span");
    const content = document.createElement("strong");
    name.textContent = label;
    content.textContent = value;
    row.append(name, content);
    el("onboardingSummary").append(row);
  }

  function renderSummary() {
    const summary = el("onboardingSummary");
    summary.replaceChildren();
    appendSummaryRow("录播文件夹", el("onboardingSourceDir").value);
    if (wizard.asrMode === "local") {
      appendSummaryRow("语音识别", "本机识别");
      appendSummaryRow("识别模型", selectedModel()?.display_name || wizard.selectedModelId);
      appendSummaryRow("模型档位", selectedModel()?.tier_label || "");
      appendSummaryRow("下载源", el("onboardingAsrSource").selectedOptions[0]?.textContent || wizard.modelSource);
      const model = selectedModel();
      appendSummaryRow(
        "模型状态",
        model?.state === "installed" ? "已安装" : selectedModelHasActiveDownload() ? "下载中" : "未安装",
      );
    } else {
      appendSummaryRow("语音识别", "云端识别");
      appendSummaryRow("识别服务", el("onboardingAsrBase").value);
      appendSummaryRow("识别模型", el("onboardingAsrModel").value);
      appendSummaryRow("ASR key", el("onboardingAsrKey").value ? "已填写（只保存在本机 .env）" : "未填写");
    }
    appendSummaryRow("AI 服务", el("onboardingLlmBase").value);
    appendSummaryRow("AI 模型", el("onboardingLlmModel").value);
    appendSummaryRow("AI key", el("onboardingLlmKey").value ? "已填写（只保存在本机 .env）" : "未填写");
  }

  function setSourceControlsLocked(locked) {
    const nextButton = el("onboardingToStep2Btn");
    el("onboardingSourceTestBtn").disabled = locked;
    el("onboardingBrowseBtn").disabled = locked;
    nextButton.disabled = locked;
    nextButton.textContent = locked ? "检查中…" : "下一步";
  }

  async function validateSource({ advance = false } = {}) {
    setSourceControlsLocked(true);
    try {
      const result = await fetchJson("/api/onboarding/test-source", {
        method: "POST",
        body: JSON.stringify({ source_dir: el("onboardingSourceDir").value }),
      });
      wizard.sourceOk = result.ok === true;
      showResult("onboardingSourceResult", wizard.sourceOk, result.message || `文件夹可用，发现 ${result.video_count} 个视频`);
      if (advance && wizard.sourceOk) showStep(2);
    } catch (error) {
      wizard.sourceOk = false;
      showResult("onboardingSourceResult", false, error.message);
    } finally {
      setSourceControlsLocked(false);
    }
  }

  async function selectRecordingFolder() {
    try {
      const selectedPath = await window.liveClipperShell.selectFolder("选择录播文件夹");
      if (!selectedPath) return;
      el("onboardingSourceDir").value = selectedPath;
      invalidateSource();
      await validateSource();
    } catch (error) {
      showResult("onboardingSourceResult", false, `无法选择文件夹：${error.message}`);
    }
  }

  async function testLlm() {
    const button = el("onboardingLlmTestBtn");
    button.disabled = true;
    try {
      const result = await fetchJson("/api/onboarding/test-llm", {
        method: "POST",
        body: JSON.stringify({
          api_base: el("onboardingLlmBase").value,
          model: el("onboardingLlmModel").value,
          api_key: el("onboardingLlmKey").value,
        }),
      });
      wizard.llmOk = result.ok === true;
      showResult("onboardingLlmResult", wizard.llmOk, result.message || "连接失败");
      el("onboardingToStep4Btn").disabled = !wizard.llmOk;
    } catch (error) {
      showResult("onboardingLlmResult", false, error.message);
    } finally {
      button.disabled = false;
    }
  }

  function setCompletionLocked(locked) {
    el("onboardingBackTo3Btn").disabled = locked;
    el("onboardingCompleteBtn").disabled = locked;
    if (!locked) updateCompletionState();
  }

  function updateCompletionState() {
    const model = selectedModel();
    const ready = wizard.asrMode !== "local" || Boolean(model && model.state === "installed");
    el("onboardingCompleteBtn").disabled = !ready;
    if (wizard.step !== 4 || wizard.asrMode !== "local" || wizard.completed) return;
    renderSummary();
    if (!ready) {
      const message = selectedModelHasActiveDownload()
        ? "模型仍在下载，安装完成后才能保存设置"
        : wizard.downloadFailure || "所选模型尚未安装，返回语音识别步骤完成下载后再保存";
      showResult("onboardingCompleteResult", false, message);
    } else {
      el("onboardingCompleteResult").hidden = true;
    }
  }

  async function complete() {
    if (wizard.completed) return;
    if (wizard.asrMode === "local") {
      try {
        await refreshModels();
      } catch (error) {
        showResult("onboardingCompleteResult", false, `无法确认模型状态：${error.message}`);
        return;
      }
      if (selectedModel()?.state !== "installed") {
        showResult("onboardingCompleteResult", false, "下载未完成，不能保存本机识别设置");
        updateCompletionState();
        return;
      }
    }
    setCompletionLocked(true);
    showResult("onboardingCompleteResult", true, "正在保存设置…");
    const payload = {
      source_dir: el("onboardingSourceDir").value,
      llm_api_base: el("onboardingLlmBase").value,
      llm_model: el("onboardingLlmModel").value,
      llm_api_key: el("onboardingLlmKey").value,
      asr_mode: wizard.asrMode,
      asr_model: wizard.asrMode === "local" ? wizard.selectedModelId : el("onboardingAsrModel").value,
      asr_model_source: wizard.modelSource,
      asr_api_base: el("onboardingAsrBase").value,
      asr_api_key: el("onboardingAsrKey").value,
    };
    try {
      const result = await fetchJson("/api/onboarding/complete", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!result.ok) {
        showResult("onboardingCompleteResult", false, result.message || "设置未保存，请检查后重试");
        setCompletionLocked(false);
        return;
      }
      wizard.completed = true;
      const serviceStart = await fetchJson("/api/service/start", { method: "POST", body: "{}" });
      if (serviceStart.ok !== true) {
        showResult("onboardingCompleteResult", false, "设置已保存，但自动化服务未启动，可进入主界面后手动启动");
        el("onboardingEnterAppBtn").hidden = false;
        return;
      }
      window.location.reload();
    } catch (error) {
      if (wizard.completed) {
        showResult("onboardingCompleteResult", false, "设置已保存，但自动化服务未启动，可进入主界面后手动启动");
        el("onboardingEnterAppBtn").hidden = false;
      } else {
        showResult("onboardingCompleteResult", false, `设置未保存：${error.message}`);
        setCompletionLocked(false);
      }
    }
  }

  function openSkipDialog() {
    el("onboardingSkipResult").hidden = true;
    el("onboardingSkipDialog").hidden = false;
    el("onboardingSkipContinueBtn").focus();
  }

  function closeSkipDialog() {
    if (wizard.skipSubmitting) return;
    el("onboardingSkipDialog").hidden = true;
  }

  async function confirmSkip() {
    if (wizard.skipSubmitting) return;
    wizard.skipSubmitting = true;
    el("onboardingSkipConfirmBtn").disabled = true;
    el("onboardingSkipContinueBtn").disabled = true;
    showResult("onboardingSkipResult", true, "正在保存…");
    try {
      const result = await fetchJson("/api/onboarding/skip", {
        method: "POST",
        body: "{}",
      });
      if (!result.ok) throw new Error(result.message || "暂时无法稍后设置");
      el("onboardingSkipDialog").hidden = true;
      el("onboardingOverlay").hidden = true;
    } catch (error) {
      showResult("onboardingSkipResult", false, `未能保存：${error.message}`);
    } finally {
      wizard.skipSubmitting = false;
      el("onboardingSkipConfirmBtn").disabled = false;
      el("onboardingSkipContinueBtn").disabled = false;
    }
  }

  async function init() {
    const status = await fetchJson("/api/onboarding");
    if (!status.needs_onboarding) return;

    wizard.presets = status.presets || [];
    wizard.initialLocalModel = status.initial_local_model;
    wizard.asrMode = status.initial_asr_mode || "local";
    el("onboardingSourceDir").value = status.source_dir || "";
    el("onboardingAsrBase").value = status.asr_api_base || "https://api.openai.com/v1";
    el("onboardingAsrModel").value = status.asr_backend === "openai" ? status.asr_model || "whisper-1" : "whisper-1";
    renderPresets();
    updateAsrState();
    if (window.liveClipperShell) {
      el("onboardingBrowseBtn").hidden = false;
      el("onboardingBrowseBtn").addEventListener("click", selectRecordingFolder);
    }
    try {
      await refreshModels();
    } catch (error) {
      wizard.models = [];
      wizard.asrReady = false;
      showResult("onboardingAsrResult", false, `无法读取本地模型：${error.message}`);
      updateAsrState();
    }

    el("onboardingSourceDir").addEventListener("input", invalidateSource);
    el("onboardingLlmBase").addEventListener("input", invalidateLlm);
    el("onboardingLlmModel").addEventListener("input", invalidateLlm);
    el("onboardingLlmKey").addEventListener("input", invalidateLlm);
    ["onboardingAsrBase", "onboardingAsrModel", "onboardingAsrKey"].forEach((id) => {
      el(id).addEventListener("input", updateAsrState);
    });
    document.querySelectorAll('input[name="onboardingAsrMode"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        if (wizard.downloadActive) return;
        wizard.asrMode = radio.value;
        updateAsrState();
      });
    });
    el("onboardingAsrSource").addEventListener("change", () => {
      if (wizard.downloadActive) return;
      wizard.modelSource = el("onboardingAsrSource").value;
      renderModelCards();
      showResult("onboardingAsrResult", true, `下次下载将使用 ${el("onboardingAsrSource").selectedOptions[0].textContent}`);
    });
    el("onboardingSourceTestBtn").addEventListener("click", () => validateSource());
    el("onboardingToStep2Btn").addEventListener("click", () => validateSource({ advance: true }));
    el("onboardingBackTo1Btn").addEventListener("click", () => showStep(1));
    el("onboardingToStep3Btn").addEventListener("click", () => showStep(3));
    el("onboardingBackTo2Btn").addEventListener("click", () => showStep(2));
    el("onboardingLlmTestBtn").addEventListener("click", testLlm);
    el("onboardingToStep4Btn").addEventListener("click", async () => {
      if (wizard.asrMode === "local") await refreshModels();
      renderSummary();
      showStep(4);
    });
    el("onboardingBackTo3Btn").addEventListener("click", () => showStep(3));
    el("onboardingCompleteBtn").addEventListener("click", complete);
    el("onboardingEnterAppBtn").addEventListener("click", () => window.location.reload());
    document.querySelectorAll("[data-onboarding-skip]").forEach((button) => {
      button.addEventListener("click", openSkipDialog);
    });
    el("onboardingSkipContinueBtn").addEventListener("click", closeSkipDialog);
    el("onboardingSkipConfirmBtn").addEventListener("click", confirmSkip);
    el("onboardingSkipDialog").addEventListener("click", (event) => {
      if (event.target === el("onboardingSkipDialog")) closeSkipDialog();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !el("onboardingSkipDialog").hidden) closeSkipDialog();
    });
    showStep(1);
    el("onboardingOverlay").hidden = false;
  }

  init().catch((error) => {
    showResult("onboardingCompleteResult", false, `无法加载初始设置：${error.message}`);
  });
})();
