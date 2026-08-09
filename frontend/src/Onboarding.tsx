import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Field } from "@astryxdesign/core/Field";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { List, ListItem } from "@astryxdesign/core/List";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { RadioList, RadioListItem } from "@astryxdesign/core/RadioList";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { Selector } from "@astryxdesign/core/Selector";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";

import { api, post } from "./api";
import type { GenericRecord, Model } from "./types";
import { semanticToneStyles } from "./ui/presentation";

interface OnboardingProps {
  notify(message: string): void;
}

interface ResultState {
  ok: boolean;
  message: string;
}

function Result({
  value,
  id,
}: {
  value: ResultState | null;
  id: string;
}) {
  if (!value) return null;
  return (
    <Text
      as="div"
      className="onboarding-result"
      id={id}
      role={value.ok ? "status" : "alert"}
      type="supporting"
      xstyle={semanticToneStyles[value.ok ? "success" : "error"]}
    >
      {value.message}
    </Text>
  );
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

async function readClipboardText() {
  if (window.liveClipperShell?.readClipboardText) return window.liveClipperShell.readClipboardText();
  if (navigator.clipboard?.readText) return navigator.clipboard.readText();
  throw new Error("当前环境无法直接读取剪贴板，请使用 Command+V 粘贴");
}

function applySecretValue(
  input: HTMLInputElement | null,
  secretRef: React.MutableRefObject<string>,
  rawValue: string,
) {
  const value = rawValue.trim();
  if (!value) throw new Error("剪贴板里没有可用的 API key");
  if (!input) throw new Error("API key 输入框尚未就绪，请重试");
  input.value = value;
  secretRef.current = value;
  input.focus();
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
  const [llmBase, setLlmBase] = useState("");
  const [llmModel, setLlmModel] = useState("");
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
  const asrBaseRef = useRef<HTMLInputElement>(null);
  const asrModelRef = useRef<HTMLInputElement>(null);
  const asrKeyInputRef = useRef<HTMLInputElement>(null);
  const llmKeyInputRef = useRef<HTMLInputElement>(null);
  const sourceInputRef = useRef<HTMLInputElement>(null);
  const asrKeyRef = useRef("");
  const llmKeyRef = useRef("");

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
  const sourceError = sourceResult && !sourceResult.ok ? sourceResult.message : "";
  const asrFieldError = asrMode === "cloud" && asrResult && !asrResult.ok
    ? {
        "请填写识别服务地址": "onboardingAsrBase",
        "请填写识别模型": "onboardingAsrModel",
        "请填写识别 API key": "onboardingAsrKey",
      }[asrResult.message]
    : undefined;

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

  function focusSourceInput() {
    window.setTimeout(() => {
      sourceInputRef.current?.focus();
      sourceInputRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  async function validateSource(advance = false) {
    setSourceBusy(true);
    try {
      const result = await post<GenericRecord>("/api/onboarding/test-source", { source_dir: sourceDir });
      const ok = result.ok === true;
      setSourceOk(ok);
      setSourceResult({ ok, message: String(result.message || `文件夹可用，发现 ${result.video_count} 个视频`) });
      if (advance && ok) {
        setStep(2);
      } else if (advance) {
        focusSourceInput();
      }
    } catch (error) {
      setSourceOk(false);
      setSourceResult({ ok: false, message: (error as Error).message });
      if (advance) focusSourceInput();
    } finally {
      setSourceBusy(false);
    }
  }

  async function selectRecordingFolder() {
    try {
      const selectFolder = window.liveClipperShell?.selectFolder;
      if (!selectFolder) return;
      const selectedPath = await selectFolder("选择录播文件夹");
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
        api_key: llmKeyRef.current,
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

  async function pasteAsrKey() {
    try {
      applySecretValue(asrKeyInputRef.current, asrKeyRef, await readClipboardText());
      setAsrResult(null);
    } catch (error) {
      setAsrResult({ ok: false, message: (error as Error).message });
    }
  }

  async function pasteLlmKey() {
    try {
      applySecretValue(llmKeyInputRef.current, llmKeyRef, await readClipboardText());
      setLlmOk(false);
      setLlmResult(null);
    } catch (error) {
      setLlmResult({ ok: false, message: (error as Error).message });
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
    setCompleteResult(null);
    let settingsSaved = false;
    try {
      const result = await post<GenericRecord>("/api/onboarding/complete", {
        source_dir: sourceDir,
        llm_api_base: llmBase,
        llm_model: llmModel,
        llm_api_key: llmKeyRef.current,
        asr_mode: asrMode,
        asr_model: asrMode === "local" ? selectedModelId : asrModel,
        asr_model_source: modelSource,
        asr_api_base: asrBase,
        asr_api_key: asrKeyRef.current,
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

  function advanceFromAsr() {
    if (asrMode === "local" && !localCanAdvance) {
      setAsrResult({ ok: false, message: "请先下载并安装所选本地模型" });
      window.setTimeout(() => {
        document.getElementById(`onboardingModelDownload-${selectedModelId}`)?.focus();
      });
      return;
    }
    if (asrMode === "cloud") {
      const missing = [
        [asrBase.trim(), "onboardingAsrBase", "请填写识别服务地址"],
        [asrModel.trim(), "onboardingAsrModel", "请填写识别模型"],
        [asrKeyRef.current.trim(), "onboardingAsrKey", "请填写识别 API key"],
      ].find(([value]) => !value);
      if (missing) {
        setAsrResult({ ok: false, message: String(missing[2]) });
        window.setTimeout(() => {
          ({
            onboardingAsrBase: asrBaseRef,
            onboardingAsrModel: asrModelRef,
            onboardingAsrKey: asrKeyInputRef,
          } as Record<string, React.RefObject<HTMLInputElement | null>>)[String(missing[1])]?.current?.focus();
        });
        return;
      }
    }
    setAsrResult(null);
    setStep(3);
  }

  function advanceFromLlm() {
    if (!llmOk) {
      setLlmResult({ ok: false, message: "请先测试 AI 服务连接" });
      window.setTimeout(() => document.getElementById("onboardingLlmTestBtn")?.focus());
      return;
    }
    void goToSummary();
  }

  if (!visible) return null;
  const skipButton = (
    <Button
      data-onboarding-skip
      label="稍后设置"
      onClick={() => { setSkipResult(null); setSkipOpen(true); }}
      variant="secondary"
    />
  );
  const sourceLabel = modelSource === "modelscope" ? "ModelScope（中国大陆）" : "Hugging Face（国际）";
  const hasLlmKey = Boolean(llmKeyRef.current);
  const hasAsrKey = Boolean(asrKeyRef.current);
  const summary = asrMode === "local"
    ? [
        ["录播文件夹", sourceDir], ["语音识别", "本机识别"], ["识别模型", selectedModel?.display_name || selectedModelId],
        ["模型档位", selectedModel?.tier_label || ""], ["下载源", sourceLabel],
        ["模型状态", selectedModel?.state === "installed" ? "已安装" : selectedModelHasActiveDownload ? "下载中" : "未安装"],
        ["AI 服务", llmBase], ["AI 模型", llmModel], ["AI key", hasLlmKey ? "已填写（只保存在本机 .env）" : "未填写"],
      ]
    : [
        ["录播文件夹", sourceDir], ["语音识别", "云端识别"], ["识别服务", asrBase], ["识别模型", asrModel],
        ["ASR key", hasAsrKey ? "已填写（只保存在本机 .env）" : "未填写"], ["AI 服务", llmBase],
        ["AI 模型", llmModel], ["AI key", hasLlmKey ? "已填写（只保存在本机 .env）" : "未填写"],
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
              <TextInput
                aria-errormessage={sourceError ? "onboardingSourceError" : undefined}
                aria-invalid={Boolean(sourceError)}
                label="录播文件夹路径"
                onChange={(value) => { setSourceDir(value); setSourceOk(false); setSourceResult(null); }}
                placeholder="例如 /Volumes/your-nas/recordings"
                ref={sourceInputRef}
                status={sourceError ? { type: "error" } : undefined}
                value={sourceDir}
                width="100%"
              />
              {window.liveClipperShell?.selectFolder && (
                <div className="onboarding-browse">
                  <Button id="onboardingBrowseBtn" isDisabled={sourceBusy} label="选择文件夹" onClick={() => void selectRecordingFolder()} />
                </div>
              )}
              {sourceError && (
                <Text
                  as="div"
                  className="onboarding-result"
                  id="onboardingSourceError"
                  role="alert"
                  type="supporting"
                  xstyle={semanticToneStyles.error}
                >
                  {sourceError}
                </Text>
              )}
              <Result id="onboardingSourceResult" value={sourceResult?.ok ? sourceResult : null} />
              <div className="onboarding-actions">
                {skipButton}
                <Button data-busy={sourceBusy ? "true" : undefined} icon={sourceBusy ? <Spinner aria-hidden="true" aria-label="检查中…" shade="inherit" size="sm" /> : undefined} id="onboardingSourceTestBtn" isDisabled={sourceBusy} label={sourceBusy ? "检查中…" : "检查文件夹"} onClick={() => void validateSource()} />
                <Button data-busy={sourceBusy ? "true" : undefined} icon={sourceBusy ? <Spinner aria-hidden="true" aria-label="检查中…" shade="inherit" size="sm" /> : undefined} id="onboardingToStep2Btn" isDisabled={sourceBusy} label={sourceBusy ? "检查中…" : "下一步"} onClick={() => void validateSource(true)} variant="primary" />
                <VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">
                  {sourceBusy ? "正在检查录播文件夹" : ""}
                </VisuallyHidden>
              </div>
            </section>
          )}
          {step === 2 && (
            <section className="onboarding-step" data-step="2">
              <h2>选择语音识别方式</h2><p>默认在本机完成识别。模型只会在你明确点击下载后安装。</p>
              <RadioList
                className="onboarding-asr-modes"
                htmlName="onboardingAsrMode"
                isDisabled={downloadActive}
                isLabelHidden
                label="语音识别方式"
                onChange={(value) => {
                  setAsrMode(value === "cloud" ? "cloud" : "local");
                  setAsrResult(null);
                }}
                orientation="horizontal"
                value={asrMode}
              >
                <RadioListItem label="本机识别（默认）" value="local" description="模型下载后可离线识别" />
                <RadioListItem label="云端识别（需要 API Key）" value="cloud" description="使用 OpenAI-compatible 服务" />
              </RadioList>
              {asrMode === "local" ? (
                <div id="onboardingAsrLocalPanel">
                  <Selector
                    className="onboarding-source-selector"
                    htmlName="onboardingAsrSource"
                    isDisabled={downloadActive}
                    label="模型下载源"
                    onChange={(value) => {
                      setModelSource(value);
                      const label = value === "modelscope" ? "ModelScope（中国大陆）" : "Hugging Face（国际）";
                      setAsrResult({ ok: true, message: `下次下载将使用 ${label}` });
                    }}
                    options={[
                      { value: "modelscope", label: "ModelScope（中国大陆）" },
                      { value: "huggingface", label: "Hugging Face（国际）" },
                    ]}
                    value={modelSource}
                    width="100%"
                  />
                  <div id="onboardingAsrModels" className="onboarding-models onboarding-model-grid">
                    {models.map((model) => (
                      <SelectableCard
                        className={`onboarding-model-card ${model.state}`}
                        data-onboarding-model-id={model.id}
                        isDisabled={downloadActive}
                        isSelected={model.id === selectedModelId}
                        key={model.id}
                        label={model.display_name}
                        onChange={() => {
                          setSelectedModelId(model.id);
                          setAsrResult(null);
                        }}
                        padding={3}
                      >
                        <div className="onboarding-model-choice">
                          <strong>{model.display_name}</strong>
                          <span>{model.tier_label} · {String(model.size_note || "")}</span>
                          <span>将使用 {sourceLabel}</span>
                          <span className="onboarding-model-state">{modelStateLabel(model)}</span>
                          {model.state !== "installed" && (
                            <Button
                              className="onboarding-model-download"
                              id={`onboardingModelDownload-${model.id}`}
                              isDisabled={downloadActive}
                              label={model.state === "damaged" ? "修复并使用" : Number(model.partial_bytes || 0) > 0 ? "继续下载" : "下载并使用"}
                              onClick={(event) => {
                                event.stopPropagation();
                                void startModelDownload(model.id);
                              }}
                              size="sm"
                              variant="secondary"
                              width="100%"
                            />
                          )}
                        </div>
                      </SelectableCard>
                    ))}
                  </div>
                  {progress && (
                    <div id="onboardingAsrProgress" className="onboarding-progress">
                      <ProgressBar
                        hasValueLabel
                        label={progress}
                        max={Number(selectedModel?.bytes_total || 1)}
                        value={Number(selectedModel?.partial_bytes || 0)}
                      />
                    </div>
                  )}
                </div>
              ) : (
                <FormLayout id="onboardingAsrCloudPanel">
                  <p>使用 OpenAI-compatible 音频转写服务，和选片 AI 可以不是同一家。</p>
                  <TextInput aria-errormessage={asrFieldError === "onboardingAsrBase" ? "onboardingAsrResult" : undefined} aria-invalid={asrFieldError === "onboardingAsrBase"} id="onboardingAsrBase" label="识别服务地址" onChange={(value) => { setAsrBase(value); setAsrResult(null); }} placeholder="https://api.openai.com/v1" ref={asrBaseRef} status={asrFieldError === "onboardingAsrBase" ? { type: "error" } : undefined} value={asrBase} width="100%" />
                  <TextInput aria-errormessage={asrFieldError === "onboardingAsrModel" ? "onboardingAsrResult" : undefined} aria-invalid={asrFieldError === "onboardingAsrModel"} id="onboardingAsrModel" label="识别模型" onChange={(value) => { setAsrModel(value); setAsrResult(null); }} placeholder="whisper-1" ref={asrModelRef} status={asrFieldError === "onboardingAsrModel" ? { type: "error" } : undefined} value={asrModel} width="100%" />
                  <Field inputID="onboardingAsrKey" label="识别 API key" width="100%">
                    <div className="secret-input-row">
                      <input
                        aria-errormessage={asrFieldError === "onboardingAsrKey" ? "onboardingAsrResult" : undefined}
                        aria-invalid={asrFieldError === "onboardingAsrKey"}
                        autoComplete="off"
                        className="onboarding-secret-input"
                        id="onboardingAsrKey"
                        onChange={(event) => { asrKeyRef.current = event.currentTarget.value; setAsrResult(null); }}
                        placeholder="直接粘贴，安全保存在本机"
                        ref={asrKeyInputRef}
                        spellCheck={false}
                        type="password"
                      />
                      <Button label="粘贴识别 API key" onClick={() => void pasteAsrKey()} />
                    </div>
                  </Field>
                </FormLayout>
              )}
              <Result id="onboardingAsrResult" value={asrResult} />
              <div className="onboarding-actions">
                {skipButton}
                <Button id="onboardingBackTo1Btn" label="上一步" onClick={() => setStep(1)} variant="secondary" />
                <Button id="onboardingToStep3Btn" label="下一步" onClick={advanceFromAsr} variant="primary" />
              </div>
            </section>
          )}
          {step === 3 && (
            <section className="onboarding-step" data-step="3">
              <h2>选一个 AI 服务</h2><p>切片选题由 AI 完成，需要一个 API key（一场直播的费用通常只要几分钱）。</p>
              <div id="onboardingPresets" className="onboarding-presets">
                {presets.map((preset) => (
                  <SelectableCard
                    className="onboarding-preset"
                    data-onboarding-preset={String(preset.id)}
                    isSelected={presetId === preset.id}
                    key={String(preset.id)}
                    label={String(preset.label)}
                    onChange={() => setPresetId(String(preset.id))}
                    padding={3}
                  >
                    <strong>{String(preset.label)}</strong>
                    {preset.signup_url && <a href={String(preset.signup_url)} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>获取 API key</a>}
                  </SelectableCard>
                ))}
              </div>
              <FormLayout>
                <TextInput id="onboardingLlmBase" label="服务地址" onChange={(value) => { setLlmBase(value); setLlmOk(false); setLlmResult(null); }} value={llmBase} width="100%" />
                <TextInput id="onboardingLlmModel" label="模型" onChange={(value) => { setLlmModel(value); setLlmOk(false); setLlmResult(null); }} value={llmModel} width="100%" />
                <Field inputID="onboardingLlmKey" label="API key" width="100%">
                  <div className="secret-input-row">
                    <input
                      autoComplete="off"
                      className="onboarding-secret-input"
                      id="onboardingLlmKey"
                      onChange={(event) => { llmKeyRef.current = event.currentTarget.value; setLlmOk(false); setLlmResult(null); }}
                      placeholder="直接粘贴，安全保存在本机"
                      ref={llmKeyInputRef}
                      spellCheck={false}
                      type="password"
                    />
                    <Button label="粘贴 AI API key" onClick={() => void pasteLlmKey()} />
                  </div>
                </Field>
              </FormLayout>
              <Result id="onboardingLlmResult" value={llmResult} />
              <div className="onboarding-actions">
                {skipButton}
                <Button id="onboardingBackTo2Btn" label="上一步" onClick={() => setStep(2)} />
                <Button data-busy={llmBusy ? "true" : undefined} icon={llmBusy ? <Spinner aria-hidden="true" aria-label="测试中…" shade="inherit" size="sm" /> : undefined} id="onboardingLlmTestBtn" isDisabled={llmBusy} label={llmBusy ? "测试中…" : "测试连接"} onClick={() => void testLlm()} />
                <VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">
                  {llmBusy ? "正在测试 AI 服务连接" : ""}
                </VisuallyHidden>
                <Button id="onboardingToStep4Btn" label="下一步" onClick={advanceFromLlm} variant="primary" />
              </div>
            </section>
          )}
          {step === 4 && (
            <section className="onboarding-step" data-step="4">
              <h2>确认设置</h2>
              <List className="onboarding-summary" density="compact" hasDividers id="onboardingSummary">
                {summary.map(([label, value]) => <ListItem endContent={<strong className="technical-value" title={value}>{value}</strong>} key={label} label={label} />)}
              </List>
              {asrMode === "local" && selectedModel?.state !== "installed" && (
                <div className="onboarding-result">
                  <Text as="div" role="alert" type="supporting" xstyle={semanticToneStyles.warning}>模型下载尚未完成</Text>
                  <Text as="div" type="supporting" xstyle={semanticToneStyles.warning}>{selectedModelHasActiveDownload ? "下载会继续进行；安装完成后才能保存设置。" : "请返回语音识别步骤完成下载。"}</Text>
                </div>
              )}
              <Result id="onboardingCompleteResult" value={completeResult} />
              <div className="onboarding-actions">
                {skipButton}
                <Button id="onboardingBackTo3Btn" isDisabled={completeBusy} label="上一步" onClick={() => setStep(3)} />
                <Button data-busy={completeBusy ? "true" : undefined} icon={completeBusy ? <Spinner aria-hidden="true" aria-label="保存中…" shade="inherit" size="sm" /> : undefined} id="onboardingCompleteBtn" isDisabled={completeBusy} label={completeBusy ? "保存中…" : "完成设置"} onClick={() => void complete()} variant="primary" />
                <VisuallyHidden as="div" aria-atomic="true" aria-live="polite" role="status">
                  {completeBusy ? "正在保存设置" : ""}
                </VisuallyHidden>
                {showEnter && <Button id="onboardingEnterAppBtn" label="进入主界面" onClick={() => window.location.reload()} />}
              </div>
            </section>
          )}
        </div>
      </div>
      <Dialog
        id="onboardingSkipDialog"
        isOpen={skipOpen}
        maxHeight="85vh"
        onOpenChange={(open) => {
          if (!skipBusy) setSkipOpen(open);
        }}
        purpose="info"
        width={460}
      >
        <div className="onboarding-skip-dialog">
          <DialogHeader hasDivider onOpenChange={setSkipOpen} title="确认稍后设置？" />
          <div className="onboarding-skip-content">
            <p>稍后设置会有以下影响：</p>
            <ul><li>未配置录像目录时不会自动发现新录像</li><li>未配置语音识别时不能完成转写</li><li>未配置 AI 服务时不能自动选片</li><li>已经启动的模型下载不会因离开引导而取消</li></ul>
            <p>你可以进入主界面，之后从以下位置继续设置：</p>
            <ul><li>设置 → 基础设置 → 文件位置 → 录播文件夹</li><li>设置 → 基础设置 → 语音识别方式 / 本地语音模型</li><li>设置 → 基础设置 → AI 服务</li></ul>
          </div>
          <div className="onboarding-dialog-actions">
            <Button id="onboardingSkipContinueBtn" isDisabled={skipBusy} label="继续设置" onClick={() => setSkipOpen(false)} variant="secondary" />
            <Button id="onboardingSkipConfirmBtn" isDisabled={skipBusy} label="确认稍后设置" onClick={() => void confirmSkip()} variant="primary" />
          </div>
          <Result id="onboardingSkipResult" value={skipResult} />
        </div>
      </Dialog>
    </>
  );
}
