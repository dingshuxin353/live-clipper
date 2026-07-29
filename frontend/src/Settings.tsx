import { useEffect, useMemo, useState } from "react";

import { post } from "./api";
import {
  defaultConfig,
  getConfigValue,
  setConfigValue,
  type ConfigField,
} from "./config";
import type { GenericRecord, Model } from "./types";

interface SettingsProps {
  configPayload: GenericRecord | null;
  service: GenericRecord | null;
  scheduler: GenericRecord | null;
  reviewAutomation: GenericRecord | null;
  models: Model[];
  reloadConfig(): Promise<void>;
  refreshModels(): Promise<void>;
  refreshAll(): Promise<void>;
  notify(message: string): void;
  schedulerDraft: GenericRecord | null;
}

interface ConfigControlProps {
  config: GenericRecord;
  field: ConfigField;
  onChange(field: ConfigField, value: unknown): void;
  type?: "text" | "number" | "checkbox";
  step?: string;
  readOnly?: boolean;
  disabled?: boolean;
  placeholder?: string;
  options?: Array<[string, string]>;
}

function ConfigControl({
  config,
  field,
  onChange,
  type = "text",
  step,
  readOnly,
  disabled,
  placeholder,
  options,
}: ConfigControlProps) {
  const value = getConfigValue(config, field);
  if (options) {
    return (
      <select
        data-config-field={field}
        value={String(value ?? "")}
        onChange={(event) => onChange(field, event.target.value)}
      >
        {options.map(([optionValue, label]) => (
          <option key={optionValue} value={optionValue}>{label}</option>
        ))}
      </select>
    );
  }
  if (type === "checkbox") {
    return (
      <input
        data-config-field={field}
        type="checkbox"
        checked={Boolean(value)}
        disabled={disabled}
        onChange={(event) => onChange(field, event.target.checked)}
      />
    );
  }
  return (
    <input
      data-config-field={field}
      type={type}
      step={step}
      value={String(value ?? "")}
      readOnly={readOnly}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(event) => {
        const next = type === "number" && event.target.value !== ""
          ? Number(event.target.value)
          : event.target.value;
        onChange(field, next);
      }}
    />
  );
}

function InfoRows({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <>
      {rows.map(([label, value]) => (
        <div className="info-row" key={label}>
          <span>{label}</span>
          <strong>{String(value || "-")}</strong>
        </div>
      ))}
    </>
  );
}

function formatModelBytes(bytes: unknown) {
  const value = Number(bytes || 0);
  const mb = value / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
}

function modelSourceLabel(source: unknown) {
  return ({ modelscope: "ModelScope", huggingface: "Hugging Face" } as Record<string, string>)[
    String(source || "")
  ] ?? String(source || "-");
}

function ModelList({
  models,
  source,
  refreshModels,
  reloadConfig,
  notify,
}: {
  models: Model[];
  source: unknown;
  refreshModels(): Promise<void>;
  reloadConfig(): Promise<void>;
  notify(message: string): void;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (!models.some((model) => model.downloading || model.state === "downloading")) return;
    const timer = window.setTimeout(() => void refreshModels(), 2000);
    return () => window.clearTimeout(timer);
  }, [models, refreshModels]);

  async function act(model: Model, action: "download" | "select" | "delete") {
    if (action === "delete" && !window.confirm("确定删除该本地模型？删除后需要重新下载才能本机识别。")) {
      return;
    }
    setBusy(model.id);
    try {
      const path = `/api/asr/models/${action}`;
      await post(path, { model: model.id });
      notify({
        download: "已开始下载模型，下载会在后台继续",
        select: "当前识别模型已切换",
        delete: "模型已删除",
      }[action]);
    } catch (error) {
      notify(`${{ download: "下载启动", select: "切换模型", delete: "删除" }[action]}失败：${(error as Error).message}`);
    } finally {
      await Promise.all([refreshModels(), action === "select" ? reloadConfig() : Promise.resolve()]);
      setBusy(null);
    }
  }

  if (!models.length) return <p className="muted">加载中…</p>;
  return (
    <>
      {models.map((model) => {
        const meta = [model.size_note, model.ram_note, model.speed_note, model.accuracy_note]
          .filter(Boolean)
          .join(" · ");
        const downloadSource = model.download_source || source;
        let status = "";
        if (model.state === "installed") {
          status = `${model.current ? "当前使用" : ""}${model.current ? " · " : ""}已安装 · ${formatModelBytes(model.installed_bytes)}`;
        } else if (model.state === "downloading") {
          const percent = model.bytes_total
            ? Math.min(99, Math.round((Number(model.partial_bytes) / Number(model.bytes_total)) * 100))
            : 0;
          status = `下载中 ${percent}% · ${formatModelBytes(model.partial_bytes)} · ${modelSourceLabel(downloadSource)}`;
        } else if (model.state === "damaged") {
          status = model.current ? "当前使用 · 模型损坏" : "损坏需修复";
        } else if (model.partial_bytes || model.last_error) {
          status = model.current
            ? `当前使用 · 尚未下载${model.last_error ? ` · ${model.last_error}` : ""}`
            : String(model.last_error || "下载未完成");
        } else if (model.current) {
          status = "当前使用 · 尚未下载";
        }
        const disabled = busy === model.id;
        return (
          <div className="asr-model-row" key={model.id}>
            <div className="asr-model-info">
              <strong>{model.display_name} <span className="asr-model-tier">{model.tier_label}</span></strong>
              <span className="muted">{meta}</span>
              <span className="asr-model-source">
                将使用：{modelSourceLabel(downloadSource)}
                {model.last_source ? ` · 上次：${modelSourceLabel(model.last_source)}` : ""}
              </span>
            </div>
            <div className="asr-model-side">
              <div className={`asr-model-status ${model.state === "damaged" ? "error" : model.state === "installed" ? "ok" : ""}`}>
                {status}
              </div>
              <div className="asr-model-actions">
                {model.state === "installed" && !model.current && (
                  <>
                    <button className="primary-button small asr-model-action" disabled={disabled} onClick={() => void act(model, "select")} type="button">设为当前模型</button>
                    <button className="secondary-button small asr-model-action asr-model-delete" disabled={disabled} onClick={() => void act(model, "delete")} type="button">删除</button>
                  </>
                )}
                {model.state !== "installed" && model.state !== "downloading" && (
                  <button className="primary-button small asr-model-action" disabled={disabled} onClick={() => void act(model, "download")} type="button">
                    {model.state === "damaged" ? "修复" : model.partial_bytes || model.last_error ? "继续下载" : "下载"}
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </>
  );
}

export function Settings(props: SettingsProps) {
  const {
    configPayload,
    service,
    scheduler,
    reviewAutomation,
    models,
    reloadConfig,
    refreshModels,
    refreshAll,
    notify,
    schedulerDraft,
  } = props;
  const [draft, setDraft] = useState<GenericRecord>({});
  const [dirty, setDirty] = useState(false);
  const [notice, setNotice] = useState<{ messages: string[]; tone: string } | null>(null);

  useEffect(() => {
    if (!dirty && configPayload?.config) setDraft(structuredClone(configPayload.config));
  }, [configPayload, dirty]);

  const change = (field: ConfigField, value: unknown) => {
    setDraft((current) => setConfigValue(current, field, value));
    setDirty(true);
  };
  const control = (field: ConfigField, options: Omit<ConfigControlProps, "config" | "field" | "onChange"> = {}) => (
    <ConfigControl config={draft} field={field} onChange={change} {...options} />
  );

  async function save() {
    try {
      const result = await post<GenericRecord>("/api/config", { config: draft });
      const messages = [
        String(result.message || "配置已保存。"),
        `备份文件：${String(result.backup_path || "无")}`,
      ];
      if (result.requires_web_restart) messages.push("Web host/port 已变化，需要手动重启 Web 控制台。");
      setNotice({ messages, tone: "success" });
      setDirty(false);
      await reloadConfig();
    } catch (error) {
      setNotice({ messages: [(error as Error).message], tone: "error" });
    }
  }

  async function validate() {
    try {
      const result = await post<GenericRecord>("/api/config/validate", { config: draft });
      const warnings = (result.warnings as GenericRecord[] | undefined) ?? [];
      const errors = (result.errors as GenericRecord[] | undefined) ?? [];
      const messages = result.ok
        ? [warnings.length ? "配置检查通过，但有提醒：" : "配置检查通过。", ...warnings.map((item) => String(item.message || item))]
        : errors.map((item) => String(item.message || item));
      setNotice({ messages, tone: result.ok ? "success" : "error" });
    } catch (error) {
      setNotice({ messages: [(error as Error).message], tone: "error" });
    }
  }

  async function action(path: string, message: string) {
    try {
      await post(path);
      setNotice({ messages: [message], tone: "success" });
      await refreshAll();
    } catch (error) {
      setNotice({ messages: [(error as Error).message], tone: "error" });
    }
  }

  const envStatus = (configPayload?.env_status ?? {}) as Record<string, boolean>;
  const healthCards = useMemo(() => {
    const config = draft;
    const review = reviewAutomation?.review_automation ?? {};
    const environment = reviewAutomation?.environment ?? {};
    const sourceDir = config.recording_source_default?.source_dir;
    const inputDir = config.recording_source_default?.input_dir || config.paths?.input_dir;
    const outputRoot = config.recording_source_default?.output_root || config.paths?.output_root;
    const llmKey = config.llm?.api_key_env;
    const asrKey = config.asr?.api_key_env;
    const reviewAvailable = environment.current_mode_available ?? environment.ok;
    return [
      ["录播源", sourceDir ? "已配置" : "未配置", sourceDir || "未填写录播源目录", sourceDir ? "ok" : "warning"],
      ["本地项目库", inputDir && outputRoot ? "正常" : "待配置", `输入：${inputDir || "-"} · 输出：${outputRoot || "-"}`, inputDir && outputRoot ? "ok" : "warning"],
      ["LLM", envStatus[llmKey] ? "已配置" : "未配置", llmKey || "未填写 API key 环境变量名", envStatus[llmKey] ? "ok" : "warning"],
      ["ASR", envStatus[asrKey] ? "已配置" : "未配置", `${config.asr?.backend || "-"} · ${asrKey || "未填写 API key 环境变量名"}`, envStatus[asrKey] ? "ok" : "warning"],
      ["服务", service?.running ? "运行中" : "未运行", service?.service?.pid ? `PID ${service.service.pid}` : "可在自动化页启动", service?.running ? "ok" : "neutral"],
      ["定时任务", scheduler?.scheduler?.enabled ? "已启用" : "未启用", scheduler?.scheduler?.next_due_at ? `下次：${scheduler.scheduler.next_due_at}` : "暂无下一次任务", scheduler?.scheduler?.enabled ? "ok" : "neutral"],
      ["AI 审阅", review.enabled ? (reviewAvailable ? "可用" : "不可用") : "未启用", `${review.mode || "-"} · ${review.provider || "-"}`, review.enabled ? (reviewAvailable ? "ok" : "warning") : "neutral"],
    ];
  }, [draft, envStatus, reviewAutomation, scheduler, service]);

  return (
    <>
      <div className="page-heading">
        <div>
          <h2>设置</h2>
          <p className="muted" id="configMeta">
            {String(configPayload?.config_path || "live-clipper.toml")} · {configPayload?.exists ? "已存在" : "尚未创建"} · API Key 只保存环境变量名{dirty ? " · 有未保存改动" : ""}
          </p>
        </div>
        <div className="button-row">
          <button id="validateConfigBtn" className="secondary-button" onClick={() => void validate()} type="button">检查配置</button>
          <button id="saveConfigBtn" className="primary-button" onClick={() => void save()} type="button">保存配置</button>
          <button id="reloadConfigBtn" className="secondary-button" onClick={() => { setDirty(false); void reloadConfig(); notify("已重新读取配置文件"); }} type="button">重载配置</button>
          <button id="resetConfigBtn" className="secondary-button" onClick={() => { if (window.confirm("恢复默认只会修改当前表单，保存前不会写入文件。继续吗？")) { setDraft(defaultConfig()); setDirty(true); } }} type="button">恢复默认</button>
          <button id="restartServiceBtn" className="secondary-button" onClick={() => void action("/api/config/restart-service", "服务已重启。")} type="button">重启服务</button>
        </div>
      </div>
      {notice && (
        <div id="configNotice" className={`notice ${notice.tone}`}>
          {notice.messages.map((message, index) => <div key={`${message}-${index}`}>{message}</div>)}
        </div>
      )}
      <form id="configForm" className="config-form" onSubmit={(event) => event.preventDefault()}>
        <section className="settings-group health-layer" data-config-layer="health">
          <div className="layer-heading"><div><h3>配置体检</h3><p className="muted">先看状态，再决定要改哪里。</p></div></div>
          <div id="configHealth" className="health-grid">
            {healthCards.map(([label, status, detail, tone]) => (
              <div className={`health-card ${tone}`} key={label}>
                <span>{label}</span><strong>{status}</strong><small>{detail}</small>
              </div>
            ))}
          </div>
        </section>

        <fieldset className="settings-group" data-config-layer="quick-start">
          <legend>基础设置</legend>
          <div className="notice subtle full-span">配好三件事就能用：录像在哪、成片放哪、AI 用哪家。API key 只填环境变量名，明文密钥保存在本机 .env 文件里。</div>
          <div className="settings-section full-span">
            <h4>文件位置</h4>
            <label>录播文件夹{control("recording_source_default.source_dir", { placeholder: "例如 /Volumes/your-nas/recordings" })}<span className="field-help">直播录像所在的文件夹（支持 NAS）。出现新录像会自动切片；留空则不自动扫描。</span></label>
            <label>任务工作区位置{control("paths.workspace_root")}<span className="field-help">每次处理都会在这里建立独立目录，并把本地录像副本和转写、切片产物放在一起。</span></label>
          </div>
          <div className="settings-section full-span">
            <h4>AI 服务</h4>
            <label>AI 服务地址{control("llm.api_base")}<span className="field-help">负责选片、写标题的 AI（OpenAI 兼容接口，如 DeepSeek / 通义 / Kimi）。</span></label>
            <label>AI 模型名{control("llm.model")}</label>
            <label>API key 环境变量名{control("llm.api_key_env")}<span className="field-help">只保存变量名；真正的密钥写在本机 .env 文件里，永远不展示明文。</span></label>
            <label>语音识别方式{control("asr.backend", { options: [["mlx_whisper", "本机识别（需另行安装本地模型）"], ["openai", "云端识别（桌面版默认）"]] })}<span className="field-help">把直播声音转成文字。本机识别首次使用需下载模型；云端更快但按量计费，需在高级设置里填接口信息。</span></label>
          </div>
          <div className="settings-section full-span">
            <h4>本地语音模型</h4>
            <p className="muted field-note">选「本机识别」时使用。模型下载一次即可离线转写，存放在本机应用数据目录。</p>
            <div id="asrModelList" className="asr-model-list">
              <ModelList models={models} source={draft.asr?.model_source} refreshModels={refreshModels} reloadConfig={reloadConfig} notify={notify} />
            </div>
          </div>
          <div className="settings-section full-span">
            <h4>出片</h4>
            <label className="check-row">{control("service.auto_render_after_selection", { type: "checkbox" })} AI 选完片后自动生成成片</label>
          </div>
        </fieldset>

        <fieldset className="settings-group scheduler-fieldset" data-config-layer="automation">
          <legend>自动化</legend>
          <div className="notice subtle full-span">自动化引擎随 App 运行。AI 自动选片默认关闭，勾选下方开关并在「自动化」页点「测试 AI 审阅环境」确认可用后即可全自动出片。</div>
          <label className="check-row">{control("scheduler.enabled", { type: "checkbox" })} 按时间表自动扫描和检查（默认每周日）</label>
          <label className="check-row">{control("review_automation.enabled", { type: "checkbox" })} 让 AI 自动选片（不用人工挑）</label>
          <label>审阅方式{control("review_automation.mode", { options: [["local_agent", "本地 Agent"], ["model", "配置模型直连"]] })}</label>
          <label>本地 Agent{control("review_automation_local_agent.provider", { options: [["codex_cli", "Codex CLI"], ["claude_code", "Claude Code"]] })}</label>
          <label>模型{control("review_automation_model.model", { placeholder: "为空时复用 LLM 模型" })}</label>
          <label>每次最多处理任务数{control("review_automation.max_runs_per_tick", { type: "number" })}</label>
          <label className="check-row">{control("review_automation.auto_render_after_selection", { type: "checkbox" })} 选片后沿用自动渲染</label>
          <details className="scheduler-editor full-span">
            <summary>编辑高级定时任务</summary>
            <div className="notice subtle">默认任务：每周录播扫描（周日 00:00）和每周审阅检查（周日 12:00）。需要自动选片时，把审阅检查的动作类型改为 AI 自动审阅。</div>
            <SchedulerEditor scheduler={scheduler} initialJob={schedulerDraft} refreshAll={refreshAll} setNotice={setNotice} />
          </details>
        </fieldset>

        <details className="settings-group advanced-layer" data-config-layer="advanced">
          <summary>高级设置（一般不需要改）</summary>
          <div className="advanced-grid">
            <fieldset>
              <legend>存储与扫描</legend><p className="muted field-note">应用内部状态目录与扫描节奏，默认值适合绝大多数情况。</p>
              <label>应用内部状态目录{control("paths.work_dir")}</label>
              <label>术语表路径{control("paths.glossary_path")}</label>
              <label>只处理最近多少小时的录像{control("recording_source_default.since_hours", { type: "number" })}</label>
              <label>录像多少分钟没变化才开始处理{control("recording_source_default.min_age_minutes", { type: "number" })}</label>
              <label>稳定性检查秒数{control("recording_source_default.stable_check_seconds", { type: "number" })}</label>
              <label className="check-row">{control("service.enabled", { type: "checkbox" })} 启用自动处理引擎</label>
              <label>清理模式{control("service.cleanup_mode", { readOnly: true })}</label>
            </fieldset>
            <fieldset>
              <legend>语音识别（ASR）</legend><p className="muted field-note">识别模型与语言。只有选了云端识别才需要填接口地址和 key。</p>
              <label>当前识别模型（请在上方模型列表切换）{control("asr.model", { readOnly: true })}</label>
              <label>识别语言{control("asr.language")}</label>
              <label>云端识别 API 地址{control("asr.api_base")}</label>
              <label>云端识别 key 环境变量名{control("asr.api_key_env")}</label>
              <label>Hugging Face token 环境变量名{control("asr.hf_token_env")}</label>
              <label>模型下载源{control("asr.model_source", { options: [["modelscope", "ModelScope（中国大陆推荐）"], ["huggingface", "Hugging Face（国际官方）"]] })}</label>
            </fieldset>
            <fieldset>
              <legend>模型请求</legend><p className="muted field-note">AI 请求的超时与重试策略。</p>
              <label>模型服务名称{control("llm.provider_label")}</label>
              <label>请求超时（秒）{control("llm.timeout_seconds", { type: "number" })}</label>
              <label>重试次数{control("llm.request_attempts", { type: "number" })}</label>
              <label>重试间隔（秒）{control("llm.retry_delay_seconds", { type: "number", step: "0.1" })}</label>
            </fieldset>
            <fieldset>
              <legend>服务与调度</legend><p className="muted field-note">自动化引擎的心跳与时区。</p>
              <label>扫描间隔（分钟）{control("service.scan_interval_minutes", { type: "number" })}</label>
              <label>调度时区{control("scheduler.timezone")}</label>
              <label>tick 秒数{control("scheduler.tick_seconds", { type: "number" })}</label>
              <label>错过执行策略{control("scheduler.missed_policy", { options: [["run_once", "补跑一次"], ["skip", "跳过"]] })}</label>
            </fieldset>
            <fieldset>
              <legend>AI 审阅参数</legend><p className="muted field-note">AI 自动选片的细节参数。</p>
              <label>单任务超时时间（分钟）{control("review_automation.timeout_minutes", { type: "number" })}</label>
              <label>失败后处理{control("review_automation.on_failure", { options: [["keep_needs_review", "保留待审阅"], ["mark_failed", "标记失败"]] })}</label>
              <label>提示词模板{control("review_automation.prompt_template")}</label>
              <label>Agent 超时（分钟）{control("review_automation_local_agent.command_timeout_minutes", { type: "number" })}</label>
              <label className="check-row">{control("review_automation_local_agent.include_review_package_inline", { type: "checkbox" })} 把审阅包放入 prompt</label>
              <label className="check-row">{control("review_automation_local_agent.allow_agent_file_writes", { type: "checkbox", disabled: true })} 允许 Agent 直接写文件（安全边界固定关闭）</label>
              <label>最多候选数{control("review_automation_model.max_candidates", { type: "number" })}</label>
              <label>Temperature{control("review_automation_model.temperature", { type: "number", step: "0.1" })}</label>
              <label>Max tokens{control("review_automation_model.max_tokens", { type: "number" })}</label>
              <label>重试次数{control("review_automation_model.retry_attempts", { type: "number" })}</label>
            </fieldset>
            <fieldset>
              <legend>Web 控制台</legend><p className="muted field-note">本地控制台的监听地址。</p>
              <label>Web Host{control("web.host")}</label>
              <label>Web Port{control("web.port", { type: "number" })}</label>
              <div className="notice subtle full-span">修改 Web host/port 后，需要手动重启 Web 控制台命令本身才会生效。</div>
              <div id="webAccessTokenStatus" className="notice subtle full-span">Web access token：{draft.web?.access_token_configured ? "已配置" : "未配置"}。这里只显示状态，不展示明文。</div>
            </fieldset>
          </div>
        </details>
      </form>
      <div id="envStatus" className="env-grid">
        {Object.entries(envStatus).length ? Object.entries(envStatus).map(([name, configured]) => (
          <div className={`env-row ${configured ? "ok" : "missing"}`} key={name}><strong>{name}</strong><span>{configured ? "已配置" : "未配置"}</span></div>
        )) : <div className="empty">没有需要展示的 API key 环境变量。</div>}
      </div>
      {dirty && (
        <div id="settingsDirtyBar" className="dirty-bar">
          <span>有未保存的更改</span>
          <button id="discardConfigBtn" className="secondary-button" onClick={() => { setDraft(structuredClone(configPayload?.config ?? {})); setDirty(false); }} type="button">放弃</button>
          <button id="saveConfigStickyBtn" className="primary-button" onClick={() => void save()} type="button">保存配置</button>
        </div>
      )}
    </>
  );
}

function SchedulerEditor({
  scheduler,
  initialJob,
  refreshAll,
  setNotice,
}: {
  scheduler: GenericRecord | null;
  initialJob: GenericRecord | null;
  refreshAll(): Promise<void>;
  setNotice(value: { messages: string[]; tone: string }): void;
}) {
  const [job, setJob] = useState<GenericRecord>({
    id: "",
    name: "每周录播扫描",
    enabled: true,
    type: "scan_recordings",
    schedule: "weekly",
    day_of_week: "sun",
    time: "00:00",
    interval_minutes: 60,
    skip_if_running: true,
  });
  useEffect(() => {
    const first = initialJob ?? scheduler?.jobs?.[0];
    if (first) setJob(first);
  }, [initialJob, scheduler]);
  const update = (key: string, value: unknown) => setJob((current) => ({ ...current, [key]: value }));
  async function saveJob() {
    const payload = { ...job };
    if (payload.schedule !== "weekly") delete payload.day_of_week;
    if (!["weekly", "daily"].includes(payload.schedule)) delete payload.time;
    if (payload.schedule !== "interval_minutes") delete payload.interval_minutes;
    try {
      await post("/api/scheduler/jobs", { job: payload });
      setNotice({ messages: ["定时任务已保存。为了让服务使用新配置，请重启服务。"], tone: "success" });
      await refreshAll();
    } catch (error) {
      setNotice({ messages: [(error as Error).message], tone: "error" });
    }
  }
  return (
    <div className="config-form nested">
      <label>任务 id<input id="schedulerJobId" value={String(job.id || "")} onChange={(event) => update("id", event.target.value)} placeholder="weekly_recording_scan" /></label>
      <label>任务名称<input id="schedulerJobName" value={String(job.name || "")} onChange={(event) => update("name", event.target.value)} /></label>
      <label className="check-row"><input id="schedulerJobEnabled" type="checkbox" checked={Boolean(job.enabled)} onChange={(event) => update("enabled", event.target.checked)} /> 启用任务</label>
      <label>动作类型<select id="schedulerJobType" value={String(job.type || "scan_recordings")} onChange={(event) => update("type", event.target.value)}><option value="scan_recordings">扫描录播</option><option value="review_due_check">审阅检查</option><option value="ai_review">AI 自动审阅</option><option value="maintenance_check">维护检查</option></select></label>
      <label>频率<select id="schedulerJobSchedule" value={String(job.schedule || "weekly")} onChange={(event) => update("schedule", event.target.value)}><option value="weekly">每周</option><option value="daily">每天</option><option value="interval_minutes">每隔 N 分钟</option></select></label>
      <label>星期<select id="schedulerJobDay" value={String(job.day_of_week || "sun")} onChange={(event) => update("day_of_week", event.target.value)}><option value="mon">周一</option><option value="tue">周二</option><option value="wed">周三</option><option value="thu">周四</option><option value="fri">周五</option><option value="sat">周六</option><option value="sun">周日</option></select></label>
      <label>时间<input id="schedulerJobTime" value={String(job.time || "")} onChange={(event) => update("time", event.target.value)} placeholder="HH:MM" /></label>
      <label>间隔分钟数<input id="schedulerJobInterval" type="number" value={Number(job.interval_minutes || 60)} onChange={(event) => update("interval_minutes", Number(event.target.value))} /></label>
      <label className="check-row"><input id="schedulerJobSkip" type="checkbox" checked={job.skip_if_running !== false} onChange={(event) => update("skip_if_running", event.target.checked)} /> 上次还在运行时跳过</label>
      <button id="saveSchedulerJobBtn" className="secondary-button" onClick={() => void saveJob()} type="button">保存定时任务</button>
    </div>
  );
}
