import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, post } from "./api";
import type { GenericRecord, Model } from "./types";

interface OnboardingProps {
  notify(message: string): void;
}

interface ResultState {
  ok: boolean;
  message: string;
}

function Result({ value, id }: { value: ResultState | null; id: string }) {
  if (!value) return null;
  return <div id={id} className={`onboarding-result ${value.ok ? "ok" : "error"}`}>{value.message}</div>;
}

function formatBytes(value: unknown) {
  const bytes = Number(value || 0);
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function modelStateLabel(model: Model) {
  if (model.state === "installed") return "已就绪";
  if (model.state === "downloading") return "下载中";
  if (model.state === "damaged") return "需要修复";
  if (Number(model.partial_bytes || 0) > 0) return "可继续下载";
  return "未下载";
}

export function Onboarding({ notify }: OnboardingProps) {
  const [visible, setVisible] = useState(false);
  const [step, setStep] = useState(1);
  const [presets, setPresets] = useState<GenericRecord[]>([]);
  const [presetId, setPresetId] = useState("deepseek");
  const [sourceDir, setSourceDir] = useState("");
  const [sourceOk, setSourceOk] = useState(false);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceResult, setSourceResult] = useState<ResultState | null>(null);
  const [asrMode, setAsrMode] = useState<"local" | "cloud">("local");
  const [modelSource, setModelSource] = useState("modelscope");
  const [models, setModels] = useState<Model[]>([]);
  const [initialModel, setInitialModel] = useState("");
  const [selectedModelId, setSelectedModelId] = useState("");
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [downloadActive, setDownloadActive] = useState(false);
  const [downloadFailure, setDownloadFailure] = useState("");
  const [progress, setProgress] = useState("");
  const [asrResult, setAsrResult] = useState<ResultState | null>(null);
  const [asrBase, setAsrBase] = useState("https://api.openai.com/v1");
  const [asrModel, setAsrModel] = useState("whisper-1");
  const [asrKey, setAsrKey] = useState("");
  const [llmBase, setLlmBase] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmOk, setLlmOk] = useState(false);
  const [llmBusy, setLlmBusy] = useState(false);
  const [llmResult, setLlmResult] = useState<ResultState | null>(null);
  const [completeResult, setCompleteResult] = useState<ResultState | null>(null);
  const [completed, setCompleted] = useState(false);
  const [completeBusy, setCompleteBusy] = useState(false);
  const [showEnter, setShowEnter] = useState(false);
  const [skipOpen, setSkipOpen] = useState(false);
  const [skipBusy, setSkipBusy] = useState(false);
  const [skipResult, setSkipResult] = useState<ResultState | null>(null);
  const mounted = useRef(true);
  const pollTimer = useRef<number | null>(null);
  const polling = useRef(false);

  const selectedModel = useMemo(
    () => models.find((model) => model.id === selectedModelId),
    [models, selectedModelId],
  );
  const selectedModelHasActiveDownload = Boolean(
    selectedModel
    && downloadActive
    && activeJobId
    && selectedModel.job_id === activeJobId
    && (selectedModel.downloading || selectedModel.state === "downloading"),
  );
  const localCanAdvance = selectedModel?.state === "installed" || selectedModelHasActiveDownload;
  const cloudReady = Boolean(asrBase.trim() && asrModel.trim() && asrKey.trim());
  const asrCanAdvance = asrMode === "local" ? localCanAdvance : cloudReady;

  const refreshModels = useCallback(async (initialize = false) => {
    const payload = await api<{ ok?: boolean; message?: string; models?: Model[]; download_source?: string }>("/api/asr/models");
    const nextModels = payload.models ?? [];
    if (
      nextModels.length !== 3
      || (initialModel && !nextModels.some((model) => model.id === initialModel))
    ) {
      throw new Error("本地模型列表与当前版本不匹配");
    }
    if (!mounted.current) return payload;
    setModels(nextModels);
    const active = nextModels.find((model) => model.downloading || model.state === "downloading");
    if (initialize) {
      setSelectedModelId(active?.id || initialModel);
      if (["modelscope", "huggingface"].includes(String(payload.download_source))) {
        setModelSource(String(payload.download_source));
      }
    } else {
      setSelectedModelId((current) => nextModels.some((model) => model.id === current)
        ? active?.id || current
        : initialModel);
    }
    setDownloadActive(Boolean(active));
    setActiveJobId(active?.job_id || null);
    return payload;
  }, [initialModel]);

  const pollJob = useCallback(async (jobId: string) => {
    if (!jobId || polling.current) return;
    polling.current = true;
    setActiveJobId(jobId);
    setDownloadActive(true);
    try {
      const jobPayload = await api<{ job?: GenericRecord }>(`/api/jobs/${encodeURIComponent(jobId)}`);
      const job = (jobPayload.job ?? jobPayload) as GenericRecord;
      await refreshModels();
      if (!mounted.current) return;
      const model = models.find((item) => item.id === selectedModelId);
      const downloaded = Number(job.bytes_downloaded ?? model?.bytes_downloaded ?? model?.partial_bytes ?? 0);
      const total = Number(job.bytes_total ?? model?.bytes_total ?? 0);
      const suffix = total > 0 ? ` / ${formatBytes(total)}` : "";
      const percent = total > 0 ? `（${Math.min(100, Math.round((downloaded / total) * 100))}%）` : "";
      setProgress(`正在下载 ${model?.display_name || "模型"}：${formatBytes(downloaded)}${suffix}${percent}`);
      if (job.status === "failed" || job.status === "interrupted") {
        const message = job.status === "interrupted"
          ? "模型下载已中断，已保留进度，可继续下载"
          : String(job.error || job.message || "模型下载失败，可稍后继续");
        setDownloadFailure(message);
        setDownloadActive(false);
        setActiveJobId(null);
        setAsrResult({ ok: false, message });
      } else if (job.status === "succeeded") {
        setDownloadActive(false);
        setActiveJobId(null);
        setProgress("");
        await refreshModels();
        setDownloadFailure("");
        setAsrResult({ ok: true, message: "模型已安装，可以继续" });
      } else {
        pollTimer.current = window.setTimeout(() => void pollJob(jobId), 1000);
      }
    } catch (error) {
      if (mounted.current) {
        setAsrResult({ ok: false, message: `暂时无法读取下载进度：${(error as Error).message}` });
        pollTimer.current = window.setTimeout(() => void pollJob(jobId), 1500);
      }
    } finally {
      polling.current = false;
    }
  }, [models, refreshModels, selectedModelId]);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    api<GenericRecord>("/api/onboarding", {}, controller.signal)
      .then(async (status) => {
        if (!mounted.current || !status.needs_onboarding) return;
        setVisible(true);
        setPresets(status.presets ?? []);
        setInitialModel(String(status.initial_local_model || ""));
        setSelectedModelId(String(status.initial_local_model || ""));
        setAsrMode(status.initial_asr_mode === "cloud" ? "cloud" : "local");
        setSourceDir(String(status.source_dir || ""));
        setAsrBase(String(status.asr_api_base || "https://api.openai.com/v1"));
        setAsrModel(status.asr_backend === "openai" ? String(status.asr_model || "whisper-1") : "whisper-1");
      })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setVisible(true);
          setCompleteResult({ ok: false, message: `无法加载初始设置：${(error as Error).message}` });
        }
      });
    return () => {
      mounted.current = false;
      controller.abort();
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, []);

  useEffect(() => {
    if (!visible || !initialModel) return;
    refreshModels(true)
      .then(() => undefined)
      .catch((error) => {
        setModels([]);
        setAsrResult({ ok: false, message: `无法读取本地模型：${(error as Error).message}` });
      });
  }, [initialModel, refreshModels, visible]);

  useEffect(() => {
    if (activeJobId && !pollTimer.current && !polling.current) void pollJob(activeJobId);
  }, [activeJobId, pollJob]);

  useEffect(() => {
    const preset = presets.find((item) => item.id === presetId);
    if (!preset || presetId === "custom") return;
    setLlmBase(String(preset.api_base || ""));
    setLlmModel(String(preset.model || ""));
    setLlmOk(false);
    setLlmResult(null);
  }, [presetId, presets]);

  useEffect(() => {
    if (!skipOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !skipBusy) setSkipOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [skipBusy, skipOpen]);

  async function validateSource(advance = false) {
    setSourceBusy(true);
    try {
      const result = await post<GenericRecord>("/api/onboarding/test-source", { source_dir: sourceDir });
      const ok = result.ok === true;
      setSourceOk(ok);
      setSourceResult({ ok, message: String(result.message || `文件夹可用，发现 ${result.video_count} 个视频`) });
      if (advance && ok) setStep(2);
    } catch (error) {
      setSourceOk(false);
      setSourceResult({ ok: false, message: (error as Error).message });
    } finally {
      setSourceBusy(false);
    }
  }

  async function selectRecordingFolder() {
    try {
      const selectedPath = await window.liveClipperShell?.selectFolder("选择录播文件夹");
      if (!selectedPath) return;
      setSourceDir(selectedPath);
      setSourceOk(false);
      setSourceResult(null);
      const result = await post<GenericRecord>("/api/onboarding/test-source", { source_dir: selectedPath });
      const ok = result.ok === true;
      setSourceOk(ok);
      setSourceResult({ ok, message: String(result.message || `文件夹可用，发现 ${result.video_count} 个视频`) });
    } catch (error) {
      setSourceResult({ ok: false, message: `无法选择文件夹：${(error as Error).message}` });
    }
  }

  async function startModelDownload(modelId: string) {
    if (downloadActive) {
      setAsrResult({ ok: false, message: "已有模型正在下载，请等待完成" });
      return;
    }
    setSelectedModelId(modelId);
    try {
      const currentPayload = await api<{ models?: Model[] }>("/api/asr/models");
      const currentModels = currentPayload.models ?? [];
      setModels(currentModels);
      const model = currentModels.find((item) => item.id === modelId);
      if (!model) throw new Error("所选模型不存在");
      if (model.state === "installed") return;
      const active = currentModels.find((item) => item.downloading || item.state === "downloading");
      if (active?.job_id) {
        setAsrResult({ ok: false, message: "已有模型正在下载，请等待完成" });
        void pollJob(active.job_id);
        return;
      }
      setDownloadActive(true);
      setDownloadFailure("");
      const payload = await post<GenericRecord>("/api/asr/models/download", { model: modelId, source: modelSource });
      const jobId = String(payload.job?.id || payload.job_id || "");
      if (!jobId) throw new Error("下载任务未返回 job id");
      setActiveJobId(jobId);
      setAsrResult({ ok: true, message: "已开始下载，离开本步骤不会取消任务" });
      void pollJob(jobId);
    } catch (error) {
      setDownloadFailure((error as Error).message);
      setDownloadActive(false);
      setActiveJobId(null);
      setAsrResult({ ok: false, message: (error as Error).message });
    }
  }

  async function testLlm() {
    setLlmBusy(true);
    try {
      const result = await post<GenericRecord>("/api/onboarding/test-llm", {
        api_base: llmBase,
        model: llmModel,
        api_key: llmKey,
      });
      const ok = result.ok === true;
      setLlmOk(ok);
      setLlmResult({ ok, message: String(result.message || "连接失败") });
    } catch (error) {
      setLlmOk(false);
      setLlmResult({ ok: false, message: (error as Error).message });
    } finally {
      setLlmBusy(false);
    }
  }

  async function goToSummary() {
    if (asrMode === "local") {
      try {
        await refreshModels();
      } catch (error) {
        setAsrResult({ ok: false, message: (error as Error).message });
        return;
      }
    }
    setStep(4);
    if (asrMode === "local" && selectedModel?.state !== "installed") {
      setCompleteResult({
        ok: false,
        message: selectedModelHasActiveDownload
          ? "模型仍在下载，安装完成后才能保存设置"
          : downloadFailure || "所选模型尚未安装，返回语音识别步骤完成下载后再保存",
      });
    } else {
      setCompleteResult(null);
    }
  }

  async function complete() {
    if (completed) return;
    if (asrMode === "local") {
      try {
        const payload = await api<{ models?: Model[] }>("/api/asr/models");
        const model = payload.models?.find((item) => item.id === selectedModelId);
        if (model?.state !== "installed") {
          setCompleteResult({ ok: false, message: "下载未完成，不能保存本机识别设置" });
          return;
        }
      } catch (error) {
        setCompleteResult({ ok: false, message: `无法确认模型状态：${(error as Error).message}` });
        return;
      }
    }
    setCompleteBusy(true);
    setCompleteResult({ ok: true, message: "正在保存设置…" });
    let settingsSaved = false;
    try {
      const result = await post<GenericRecord>("/api/onboarding/complete", {
        source_dir: sourceDir,
        llm_api_base: llmBase,
        llm_model: llmModel,
        llm_api_key: llmKey,
        asr_mode: asrMode,
        asr_model: asrMode === "local" ? selectedModelId : asrModel,
        asr_model_source: modelSource,
        asr_api_base: asrBase,
        asr_api_key: asrKey,
      });
      if (!result.ok) throw new Error(String(result.message || "设置未保存，请检查后重试"));
      settingsSaved = true;
      setCompleted(true);
      const serviceStart = await post<GenericRecord>("/api/service/start", {});
      if (serviceStart.ok !== true) {
        setCompleteResult({ ok: false, message: "设置已保存，但自动化服务未启动，可进入主界面后手动启动" });
        setShowEnter(true);
        return;
      }
      window.location.reload();
    } catch (error) {
      if (settingsSaved) {
        setCompleteResult({ ok: false, message: "设置已保存，但自动化服务未启动，可进入主界面后手动启动" });
        setShowEnter(true);
      } else {
        setCompleteResult({ ok: false, message: `设置未保存：${(error as Error).message}` });
      }
    } finally {
      setCompleteBusy(false);
    }
  }

  async function confirmSkip() {
    if (skipBusy) return;
    setSkipBusy(true);
    setSkipResult({ ok: true, message: "正在保存…" });
    try {
      await post("/api/onboarding/skip", {});
      setSkipOpen(false);
      setVisible(false);
    } catch (error) {
      setSkipResult({ ok: false, message: `未能保存：${(error as Error).message}` });
    } finally {
      setSkipBusy(false);
    }
  }

  if (!visible) return null;
  const skipButton = <button className="secondary-button" data-onboarding-skip onClick={() => { setSkipResult(null); setSkipOpen(true); }} type="button">稍后设置</button>;
  const sourceLabel = modelSource === "modelscope" ? "ModelScope（中国大陆）" : "Hugging Face（国际）";
  const summary = asrMode === "local"
    ? [
        ["录播文件夹", sourceDir], ["语音识别", "本机识别"], ["识别模型", selectedModel?.display_name || selectedModelId],
        ["模型档位", selectedModel?.tier_label || ""], ["下载源", sourceLabel],
        ["模型状态", selectedModel?.state === "installed" ? "已安装" : selectedModelHasActiveDownload ? "下载中" : "未安装"],
        ["AI 服务", llmBase], ["AI 模型", llmModel], ["AI key", llmKey ? "已填写（只保存在本机 .env）" : "未填写"],
      ]
    : [
        ["录播文件夹", sourceDir], ["语音识别", "云端识别"], ["识别服务", asrBase], ["识别模型", asrModel],
        ["ASR key", asrKey ? "已填写（只保存在本机 .env）" : "未填写"], ["AI 服务", llmBase],
        ["AI 模型", llmModel], ["AI key", llmKey ? "已填写（只保存在本机 .env）" : "未填写"],
      ];

  return (
    <>
      <div id="onboardingOverlay" className="onboarding-overlay">
        <div className="onboarding-card">
          <div className="onboarding-steps">
            {["1 录播文件夹", "2 语音识别", "3 AI 服务", "4 完成"].map((label, index) => (
              <span className={`onboarding-step-dot ${step === index + 1 ? "active" : ""}`} data-step-dot={index + 1} key={label}>{label}</span>
            ))}
          </div>
          {step === 1 && (
            <section className="onboarding-step" data-step="1">
              <h2>欢迎使用 Venus</h2><p>先告诉我你的直播录像放在哪个文件夹，以后有新录像会自动切片。</p>
              <label>录播文件夹路径<input id="onboardingSourceDir" value={sourceDir} onChange={(event) => { setSourceDir(event.target.value); setSourceOk(false); setSourceResult(null); }} placeholder="例如 /Volumes/your-nas/recordings" /></label>
              {window.liveClipperShell && <button id="onboardingBrowseBtn" className="secondary-button onboarding-browse" disabled={sourceBusy} onClick={() => void selectRecordingFolder()} type="button">选择文件夹</button>}
              <Result id="onboardingSourceResult" value={sourceResult} />
              <div className="onboarding-actions">{skipButton}<button id="onboardingSourceTestBtn" className="secondary-button" disabled={sourceBusy} onClick={() => void validateSource()} type="button">检查文件夹</button><button id="onboardingToStep2Btn" className="primary-button" disabled={sourceBusy} onClick={() => void validateSource(true)} type="button">{sourceBusy ? "检查中…" : "下一步"}</button></div>
            </section>
          )}
          {step === 2 && (
            <section className="onboarding-step" data-step="2">
              <h2>选择语音识别方式</h2><p>默认在本机完成识别。模型只会在你明确点击下载后安装。</p>
              <div className="onboarding-asr-modes">
                <label><input id="onboardingAsrLocal" name="onboardingAsrMode" type="radio" value="local" checked={asrMode === "local"} disabled={downloadActive} onChange={() => setAsrMode("local")} /> 本机识别（默认）</label>
                <label><input id="onboardingAsrCloud" name="onboardingAsrMode" type="radio" value="cloud" checked={asrMode === "cloud"} disabled={downloadActive} onChange={() => setAsrMode("cloud")} /> 云端识别（需要 API Key）</label>
              </div>
              {asrMode === "local" ? (
                <div id="onboardingAsrLocalPanel">
                  <label>模型下载源<select id="onboardingAsrSource" value={modelSource} disabled={downloadActive} onChange={(event) => { setModelSource(event.target.value); setAsrResult({ ok: true, message: `下次下载将使用 ${event.target.selectedOptions[0].textContent}` }); }}><option value="modelscope">ModelScope（中国大陆）</option><option value="huggingface">Hugging Face（国际）</option></select></label>
                  <div id="onboardingAsrModels" className="onboarding-models onboarding-model-grid">
                    {models.map((model) => (
                      <article className={`onboarding-model-card ${model.id === selectedModelId ? "selected" : ""} ${model.state}`} data-onboarding-model-id={model.id} key={model.id}>
                        <button className="onboarding-model-select" disabled={downloadActive} onClick={() => setSelectedModelId(model.id)} type="button"><strong>{model.display_name}</strong><span>{model.tier_label} · {String(model.size_note || "")}</span><span>将使用 {sourceLabel}</span><span className="onboarding-model-state">{modelStateLabel(model)}</span></button>
                        {model.state !== "installed" && <button className="secondary-button onboarding-model-download" disabled={downloadActive} onClick={() => void startModelDownload(model.id)} type="button">{model.state === "damaged" ? "修复并使用" : Number(model.partial_bytes || 0) > 0 ? "继续下载" : "下载并使用"}</button>}
                      </article>
                    ))}
                  </div>
                  {progress && <div id="onboardingAsrProgress" className="onboarding-progress">{progress}</div>}
                </div>
              ) : (
                <div id="onboardingAsrCloudPanel">
                  <p>使用 OpenAI-compatible 音频转写服务，和选片 AI 可以不是同一家。</p>
                  <label>识别服务地址<input id="onboardingAsrBase" value={asrBase} onChange={(event) => setAsrBase(event.target.value)} placeholder="https://api.openai.com/v1" /></label>
                  <label>识别模型<input id="onboardingAsrModel" value={asrModel} onChange={(event) => setAsrModel(event.target.value)} placeholder="whisper-1" /></label>
                  <label>识别 API key<input id="onboardingAsrKey" type="password" value={asrKey} onChange={(event) => setAsrKey(event.target.value)} placeholder="只保存在本机 .env" /></label>
                </div>
              )}
              <Result id="onboardingAsrResult" value={asrResult} />
              <div className="onboarding-actions">{skipButton}<button id="onboardingBackTo1Btn" className="secondary-button" onClick={() => setStep(1)} type="button">上一步</button><button id="onboardingToStep3Btn" className="primary-button" disabled={!asrCanAdvance} onClick={() => setStep(3)} type="button">下一步</button></div>
            </section>
          )}
          {step === 3 && (
            <section className="onboarding-step" data-step="3">
              <h2>选一个 AI 服务</h2><p>切片选题由 AI 完成，需要一个 API key（一场直播的费用通常只要几分钱）。</p>
              <div id="onboardingPresets" className="onboarding-presets">
                {presets.map((preset) => <button className={`onboarding-preset ${presetId === preset.id ? "active" : ""}`} data-onboarding-preset={String(preset.id)} onClick={() => setPresetId(String(preset.id))} key={String(preset.id)} type="button"><strong>{String(preset.label)}</strong>{preset.signup_url && <a href={String(preset.signup_url)} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>获取 API key</a>}</button>)}
              </div>
              <label>服务地址<input id="onboardingLlmBase" value={llmBase} onChange={(event) => { setLlmBase(event.target.value); setLlmOk(false); setLlmResult(null); }} /></label>
              <label>模型<input id="onboardingLlmModel" value={llmModel} onChange={(event) => { setLlmModel(event.target.value); setLlmOk(false); setLlmResult(null); }} /></label>
              <label>API key<input id="onboardingLlmKey" type="password" value={llmKey} onChange={(event) => { setLlmKey(event.target.value); setLlmOk(false); setLlmResult(null); }} placeholder="粘贴你的 API key" /></label>
              <Result id="onboardingLlmResult" value={llmResult} />
              <div className="onboarding-actions">{skipButton}<button id="onboardingBackTo2Btn" className="secondary-button" onClick={() => setStep(2)} type="button">上一步</button><button id="onboardingLlmTestBtn" className="secondary-button" disabled={llmBusy} onClick={() => void testLlm()} type="button">测试连接</button><button id="onboardingToStep4Btn" className="primary-button" disabled={!llmOk} onClick={() => void goToSummary()} type="button">下一步</button></div>
            </section>
          )}
          {step === 4 && (
            <section className="onboarding-step" data-step="4">
              <h2>确认设置</h2>
              <div id="onboardingSummary" className="onboarding-summary">{summary.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
              <Result id="onboardingCompleteResult" value={completeResult} />
              <div className="onboarding-actions">{skipButton}<button id="onboardingBackTo3Btn" className="secondary-button" disabled={completeBusy} onClick={() => setStep(3)} type="button">上一步</button><button id="onboardingCompleteBtn" className="primary-button" disabled={completeBusy || (asrMode === "local" && selectedModel?.state !== "installed")} onClick={() => void complete()} type="button">完成设置</button>{showEnter && <button id="onboardingEnterAppBtn" className="secondary-button" onClick={() => window.location.reload()} type="button">进入主界面</button>}</div>
            </section>
          )}
        </div>
      </div>
      {skipOpen && (
        <div id="onboardingSkipDialog" className="onboarding-skip-dialog" role="dialog" aria-modal="true" aria-labelledby="onboardingSkipTitle" onClick={(event) => { if (event.target === event.currentTarget && !skipBusy) setSkipOpen(false); }}>
          <h2 id="onboardingSkipTitle">确认稍后设置？</h2><p>稍后设置会有以下影响：</p>
          <ul><li>未配置录像目录时不会自动发现新录像</li><li>未配置语音识别时不能完成转写</li><li>未配置 AI 服务时不能自动选片</li><li>已经启动的模型下载不会因离开引导而取消</li></ul>
          <p>你可以进入主界面，之后从以下位置继续设置：</p>
          <ul><li>设置 → 基础设置 → 文件位置 → 录播文件夹</li><li>设置 → 基础设置 → 语音识别方式 / 本地语音模型</li><li>设置 → 基础设置 → AI 服务</li></ul>
          <button id="onboardingSkipContinueBtn" className="secondary-button" disabled={skipBusy} autoFocus onClick={() => setSkipOpen(false)} type="button">继续设置</button>
          <button id="onboardingSkipConfirmBtn" className="primary-button" disabled={skipBusy} onClick={() => void confirmSkip()} type="button">确认稍后设置</button>
          <Result id="onboardingSkipResult" value={skipResult} />
        </div>
      )}
    </>
  );
}
