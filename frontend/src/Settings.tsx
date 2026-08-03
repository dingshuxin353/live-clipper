import { useEffect, useMemo, useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { FormLayout } from "@astryxdesign/core/FormLayout";
import { List, ListItem } from "@astryxdesign/core/List";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";

import { post } from "./api";
import {
  defaultConfig,
  getConfigValue,
  setConfigValue,
  type ConfigField,
} from "./config";
import type { GenericRecord, Model } from "./types";
import { formatLocalTime, semanticToneStyles } from "./ui/presentation";

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

interface SettingsFieldProps {
  config: GenericRecord;
  field: ConfigField;
  label: string;
  onChange(field: ConfigField, value: unknown): void;
  description?: string;
  type?: "text" | "number" | "checkbox";
  step?: number;
  readOnly?: boolean;
  disabled?: boolean;
  placeholder?: string;
  options?: Array<[string, string]>;
}

function SettingsField({
  config,
  field,
  label,
  onChange,
  description,
  type = "text",
  step,
  readOnly,
  disabled,
  placeholder,
  options,
}: SettingsFieldProps) {
  const value = getConfigValue(config, field);
  const shared = {
    "data-config-field": field,
    className: "settings-field",
    width: "100%" as const,
  };
  if (options) {
    return (
      <Selector
        {...shared}
        isDisabled={disabled || readOnly}
        disabledMessage={readOnly ? "请使用本页对应的专用操作修改" : undefined}
        description={description}
        label={label}
        onChange={(next) => onChange(field, next)}
        options={options.map(([optionValue, optionLabel]) => ({
          value: optionValue,
          label: optionLabel,
        }))}
        value={String(value ?? "")}
      />
    );
  }
  if (type === "checkbox") {
    return (
      <CheckboxInput
        {...shared}
        description={description}
        isDisabled={disabled}
        label={label}
        onChange={(checked) => onChange(field, checked)}
        value={Boolean(value)}
      />
    );
  }
  if (type === "number") {
    return (
      <NumberInput
        {...shared}
        description={description}
        isDisabled={disabled || readOnly}
        disabledMessage={readOnly ? "请使用本页对应的专用操作修改" : undefined}
        label={label}
        onChange={(next) => onChange(field, next)}
        placeholder={placeholder}
        step={step}
        value={value === "" || value == null ? null : Number(value)}
      />
    );
  }
  return (
    <TextInput
      {...shared}
      description={description}
      isDisabled={disabled || readOnly}
      disabledMessage={readOnly ? "请使用本页对应的专用操作修改" : undefined}
      label={label}
      onChange={(next) => onChange(field, next)}
      placeholder={placeholder}
      value={String(value ?? "")}
    />
  );
}

function InfoRows({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <>
      {rows.map(([label, value]) => (
        <div className="info-row" key={label}>
          <span>{label}</span>
          <strong className="technical-value" title={String(value || "-")}>
            {String(value || "-")}
          </strong>
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
  const [deleteModel, setDeleteModel] = useState<Model | null>(null);

  useEffect(() => {
    if (!models.some((model) => model.downloading || model.state === "downloading")) return;
    const timer = window.setTimeout(() => void refreshModels(), 2000);
    return () => window.clearTimeout(timer);
  }, [models, refreshModels]);

  async function act(model: Model, action: "download" | "select" | "delete") {
    setBusy(model.id);
    try {
      await post(`/api/asr/models/${action}`, { model: model.id });
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
      setDeleteModel(null);
    }
  }

  if (!models.length) return <p className="muted" role="status">加载中…</p>;
  return (
    <>
      <List className="asr-model-rows" density="compact" hasDividers>
        {models.map((model) => {
          const meta = [model.size_note, model.ram_note, model.speed_note, model.accuracy_note]
            .filter(Boolean)
            .join(" · ");
          const downloadSource = model.download_source || source;
          let status = "";
          if (model.state === "installed") {
            status = `${model.current ? "当前使用 · " : ""}已安装 · ${formatModelBytes(model.installed_bytes)}`;
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
            <ListItem
              className="asr-model-row"
              description={(
                <div className="asr-model-meta">
                  <span>{meta}</span>
                  <span className="asr-model-source">
                    将使用：{modelSourceLabel(downloadSource)}
                    {model.last_source ? ` · 上次：${modelSourceLabel(model.last_source)}` : ""}
                  </span>
                </div>
              )}
              endContent={(
                <div className="asr-model-side">
                  <div className={`asr-model-status ${model.state === "damaged" ? "error" : model.state === "installed" ? "ok" : ""}`}>
                    {status}
                  </div>
                  <div className="asr-model-actions">
                    {model.state === "installed" && !model.current && (
                      <>
                        <Button className="asr-model-action" isDisabled={disabled} label="设为当前模型" onClick={() => void act(model, "select")} size="sm" variant="primary" />
                        <Button className="asr-model-action asr-model-delete" isDisabled={disabled} label="删除" onClick={() => setDeleteModel(model)} size="sm" variant="destructive" />
                      </>
                    )}
                    {model.state !== "installed" && model.state !== "downloading" && (
                      <Button
                        className="asr-model-action"
                        isDisabled={disabled}
                        label={model.state === "damaged" ? "修复" : model.partial_bytes || model.last_error ? "继续下载" : "下载"}
                        onClick={() => void act(model, "download")}
                        size="sm"
                        variant="primary"
                      />
                    )}
                  </div>
                </div>
              )}
              key={model.id}
              label={<strong>{model.display_name} <span className="asr-model-tier">{model.tier_label}</span></strong>}
            />
          );
        })}
      </List>
      <AlertDialog
        actionLabel="确认删除"
        actionVariant="destructive"
        cancelLabel="取消"
        description="删除后需要重新下载才能继续使用本机识别。"
        isOpen={Boolean(deleteModel)}
        onAction={() => {
          const model = deleteModel;
          if (!model) return;
          setDeleteModel(null);
          void act(model, "delete");
        }}
        onOpenChange={(open) => { if (!open && !busy) setDeleteModel(null); }}
        title={`删除 ${deleteModel?.display_name || "本地模型"}？`}
      />
    </>
  );
}

function SettingsSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="settings-section full-span">
      <h4>{title}</h4>
      {description && <p className="muted field-note">{description}</p>}
      <FormLayout className="settings-field-grid">{children}</FormLayout>
    </section>
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
  const [notice, setNotice] = useState<{ messages: string[]; tone: "success" | "error" | "warning" | "info" } | null>(null);
  const [resetOpen, setResetOpen] = useState(false);

  useEffect(() => {
    if (!dirty && configPayload?.config) setDraft(structuredClone(configPayload.config));
  }, [configPayload, dirty]);

  const change = (field: ConfigField, value: unknown) => {
    setDraft((current) => setConfigValue(current, field, value));
    setDirty(true);
  };
  const control = (
    field: ConfigField,
    label: string,
    options: Omit<SettingsFieldProps, "config" | "field" | "label" | "onChange"> = {},
  ) => (
    <SettingsField config={draft} field={field} label={label} onChange={change} {...options} />
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
      setNotice({ messages, tone: result.ok ? (warnings.length ? "warning" : "success") : "error" });
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
      ["录播源", sourceDir ? "已配置" : "未配置", sourceDir || "未填写录播源目录", sourceDir ? "success" : "warning"],
      ["本地项目库", inputDir && outputRoot ? "正常" : "待配置", `输入：${inputDir || "-"} · 输出：${outputRoot || "-"}`, inputDir && outputRoot ? "success" : "warning"],
      ["LLM", envStatus[llmKey] ? "已配置" : "未配置", llmKey || "未填写 API key 环境变量名", envStatus[llmKey] ? "success" : "warning"],
      ["ASR", envStatus[asrKey] ? "已配置" : "未配置", `${config.asr?.backend || "-"} · ${asrKey || "未填写 API key 环境变量名"}`, envStatus[asrKey] ? "success" : "warning"],
      ["服务", service?.running ? "运行中" : "未运行", service?.service?.pid ? `PID ${service.service.pid}` : "可在自动化页启动", service?.running ? "success" : "neutral"],
      ["定时任务", scheduler?.scheduler?.enabled ? "已启用" : "未启用", scheduler?.scheduler?.next_due_at ? `下次：${formatLocalTime(scheduler.scheduler.next_due_at)}` : "暂无下一次任务", scheduler?.scheduler?.enabled ? "success" : "neutral"],
      ["AI 审阅", review.enabled ? (reviewAvailable ? "可用" : "不可用") : "未启用", `${review.mode || "-"} · ${review.provider || "-"}`, review.enabled ? (reviewAvailable ? "success" : "warning") : "neutral"],
    ] as const;
  }, [draft, envStatus, reviewAutomation, scheduler, service]);

  return (
    <>
      <div className="page-heading">
        <div>
          <h2>设置</h2>
          <p className="muted technical-value" id="configMeta" title={String(configPayload?.config_path || "live-clipper.toml")}>
            {String(configPayload?.config_path || "live-clipper.toml")} · {configPayload?.exists ? "已存在" : "尚未创建"} · API Key 只保存环境变量名{dirty ? " · 有未保存改动" : ""}
          </p>
        </div>
        <div className="button-row">
          <Button id="validateConfigBtn" label="检查配置" onClick={() => void validate()} />
          <Button id="saveConfigBtn" label="保存配置" onClick={() => void save()} variant="primary" />
          <Button id="reloadConfigBtn" label="重载配置" onClick={() => { setDirty(false); void reloadConfig(); notify("已重新读取配置文件"); }} />
          <Button id="resetConfigBtn" label="恢复默认" onClick={() => setResetOpen(true)} />
          <Button id="restartServiceBtn" label="重启服务" onClick={() => void action("/api/config/restart-service", "服务已重启。")} />
        </div>
      </div>
      {notice && (
        <div className="config-notice" id="configNotice" role={notice.tone === "error" || notice.tone === "warning" ? "alert" : "status"}>
          {(notice.messages.length ? notice.messages : ["配置状态"]).map((message) => (
            <Text as="div" key={message} type="supporting" xstyle={semanticToneStyles[notice.tone]}>
              {message}
            </Text>
          ))}
        </div>
      )}
      <form id="configForm" className="config-form" onSubmit={(event) => event.preventDefault()}>
        <section className="settings-group health-layer" data-config-layer="health">
          <div className="layer-heading"><div><h3>配置体检</h3><p className="muted">先看状态，再决定要改哪里。</p></div></div>
          <div id="configHealth" className="health-grid">
            {healthCards.map(([label, status, detail, tone]) => (
              <Card className="health-card" key={label} padding={3} variant="muted">
                <span>{label}</span>
                <strong><StatusDot label={status} variant={tone === "neutral" ? "neutral" : tone} /> {status}</strong>
                <small className="technical-value" title={detail}>{detail}</small>
              </Card>
            ))}
          </div>
        </section>

        <fieldset className="settings-group" data-config-layer="quick-start">
          <legend>基础设置</legend>
          <div className="full-span settings-guidance">
            <Text as="div" color="secondary" type="supporting">配好三件事就能用</Text>
            <Text as="div" color="secondary" type="supporting">录像在哪、成片放哪、AI 用哪家。API key 只填环境变量名，明文密钥保存在本机 .env 文件里。</Text>
          </div>
          <SettingsSection title="文件位置">
            {control("recording_source_default.source_dir", "录播文件夹", { placeholder: "例如 /Volumes/your-nas/recordings", description: "直播录像所在的文件夹（支持 NAS）。出现新录像会自动切片；留空则不自动扫描。" })}
            {control("paths.workspace_root", "任务工作区位置", { description: "每次处理都会在这里建立独立目录，并把本地录像副本和转写、切片产物放在一起。" })}
          </SettingsSection>
          <SettingsSection title="AI 服务">
            {control("llm.api_base", "AI 服务地址", { description: "负责选片、写标题的 AI（OpenAI 兼容接口，如 DeepSeek / 通义 / Kimi）。" })}
            {control("llm.model", "AI 模型名")}
            {control("llm.api_key_env", "API key 环境变量名", { description: "只保存变量名；真正的密钥写在本机 .env 文件里，永远不展示明文。" })}
            {control("asr.backend", "语音识别方式", { options: [["mlx_whisper", "本机识别（需另行安装本地模型）"], ["openai", "云端识别（桌面版默认）"]], description: "把直播声音转成文字。本机识别首次使用需下载模型；云端更快但按量计费。" })}
          </SettingsSection>
          <SettingsSection title="本地语音模型" description="选「本机识别」时使用。模型下载一次即可离线转写，存放在本机应用数据目录。">
            <div className="full-span" id="asrModelList">
              <ModelList models={models} source={draft.asr?.model_source} refreshModels={refreshModels} reloadConfig={reloadConfig} notify={notify} />
            </div>
          </SettingsSection>
          <SettingsSection title="出片">
            {control("service.auto_render_after_selection", "AI 选完片后自动生成成片", { type: "checkbox" })}
          </SettingsSection>
        </fieldset>

        <fieldset className="settings-group scheduler-fieldset" data-config-layer="automation">
          <legend>自动化</legend>
          <div className="full-span settings-guidance">
            <Text as="div" color="secondary" type="supporting">自动化引擎随 App 运行</Text>
            <Text as="div" color="secondary" type="supporting">AI 自动选片默认关闭，勾选下方开关并在「自动化」页点「测试 AI 审阅环境」确认可用后即可全自动出片。</Text>
          </div>
          <FormLayout className="settings-field-grid full-span">
            {control("scheduler.enabled", "按时间表自动扫描和检查（默认每周日）", { type: "checkbox" })}
            {control("review_automation.enabled", "让 AI 自动选片（不用人工挑）", { type: "checkbox" })}
            {control("review_automation.mode", "审阅方式", { options: [["local_agent", "本地 Agent"], ["model", "配置模型直连"]] })}
            {control("review_automation_local_agent.provider", "本地 Agent", { options: [["codex_cli", "Codex CLI"], ["claude_code", "Claude Code"]] })}
            {control("review_automation_model.model", "模型", { placeholder: "为空时复用 LLM 模型" })}
            {control("review_automation.max_runs_per_tick", "每次最多处理任务数", { type: "number" })}
            {control("review_automation.auto_render_after_selection", "选片后沿用自动渲染", { type: "checkbox" })}
          </FormLayout>
          <details className="scheduler-editor full-span">
            <summary>编辑高级定时任务</summary>
            <div className="settings-guidance">
              <Text as="div" color="secondary" type="supporting">高级定时任务</Text>
              <Text as="div" color="secondary" type="supporting">默认任务为每周录播扫描和每周审阅检查；需要自动选片时可将动作类型改为 AI 自动审阅。</Text>
            </div>
            <SchedulerEditor scheduler={scheduler} initialJob={schedulerDraft} refreshAll={refreshAll} setNotice={setNotice} />
          </details>
        </fieldset>

        <details className="settings-group advanced-layer" data-config-layer="advanced">
          <summary>高级设置（一般不需要改）</summary>
          <div className="advanced-grid">
            <fieldset>
              <legend>存储与扫描</legend>
              <FormLayout>
                {control("paths.work_dir", "应用内部状态目录")}
                {control("paths.glossary_path", "术语表路径")}
                {control("recording_source_default.since_hours", "只处理最近多少小时的录像", { type: "number" })}
                {control("recording_source_default.min_age_minutes", "录像多少分钟没变化才开始处理", { type: "number" })}
                {control("recording_source_default.stable_check_seconds", "稳定性检查秒数", { type: "number" })}
                {control("service.enabled", "启用自动处理引擎", { type: "checkbox" })}
                {control("service.cleanup_mode", "清理模式", { readOnly: true })}
              </FormLayout>
            </fieldset>
            <fieldset>
              <legend>语音识别（ASR）</legend>
              <FormLayout>
                {control("asr.model", "当前识别模型（请在上方模型列表切换）", { readOnly: true })}
                {control("asr.language", "识别语言")}
                {control("asr.api_base", "云端识别 API 地址")}
                {control("asr.api_key_env", "云端识别 key 环境变量名")}
                {control("asr.hf_token_env", "Hugging Face token 环境变量名")}
                {control("asr.model_source", "模型下载源", { options: [["modelscope", "ModelScope（中国大陆推荐）"], ["huggingface", "Hugging Face（国际官方）"]] })}
              </FormLayout>
            </fieldset>
            <fieldset>
              <legend>模型请求</legend>
              <FormLayout>
                {control("llm.provider_label", "模型服务名称")}
                {control("llm.timeout_seconds", "请求超时（秒）", { type: "number" })}
                {control("llm.request_attempts", "重试次数", { type: "number" })}
                {control("llm.retry_delay_seconds", "重试间隔（秒）", { type: "number", step: 0.1 })}
              </FormLayout>
            </fieldset>
            <fieldset>
              <legend>服务与调度</legend>
              <FormLayout>
                {control("service.scan_interval_minutes", "扫描间隔（分钟）", { type: "number" })}
                {control("scheduler.timezone", "调度时区")}
                {control("scheduler.tick_seconds", "tick 秒数", { type: "number" })}
                {control("scheduler.missed_policy", "错过执行策略", { options: [["run_once", "补跑一次"], ["skip", "跳过"]] })}
              </FormLayout>
            </fieldset>
            <fieldset>
              <legend>AI 审阅参数</legend>
              <FormLayout>
                {control("review_automation.timeout_minutes", "单任务超时时间（分钟）", { type: "number" })}
                {control("review_automation.on_failure", "失败后处理", { options: [["keep_needs_review", "保留待审阅"], ["mark_failed", "标记失败"]] })}
                {control("review_automation.prompt_template", "提示词模板")}
                {control("review_automation_local_agent.command_timeout_minutes", "Agent 超时（分钟）", { type: "number" })}
                {control("review_automation_local_agent.include_review_package_inline", "把审阅包放入 prompt", { type: "checkbox" })}
                {control("review_automation_local_agent.allow_agent_file_writes", "允许 Agent 直接写文件（安全边界固定关闭）", { type: "checkbox", disabled: true })}
                {control("review_automation_model.max_candidates", "最多候选数", { type: "number" })}
                {control("review_automation_model.temperature", "Temperature", { type: "number", step: 0.1 })}
                {control("review_automation_model.max_tokens", "Max tokens", { type: "number" })}
                {control("review_automation_model.retry_attempts", "重试次数", { type: "number" })}
              </FormLayout>
            </fieldset>
            <fieldset>
              <legend>Web 控制台</legend>
              <FormLayout>
                {control("web.host", "Web Host")}
                {control("web.port", "Web Port", { type: "number" })}
              </FormLayout>
              <Text as="div" color="secondary" type="supporting">修改 Web host/port 后，需要手动重启 Web 控制台命令本身才会生效。</Text>
              <div id="webAccessTokenStatus">
                <Text as="div" color="secondary" type="supporting">{`Web access token：${draft.web?.access_token_configured ? "已配置" : "未配置"}`}</Text>
                <Text as="div" color="secondary" type="supporting">这里只显示状态，不展示明文。</Text>
              </div>
            </fieldset>
          </div>
        </details>
      </form>
      <List className="env-grid" density="compact" hasDividers id="envStatus">
        {Object.entries(envStatus).length
          ? Object.entries(envStatus).map(([name, configured]) => (
            <ListItem
              endContent={<span>{configured ? "已配置" : "未配置"}</span>}
              key={name}
              label={<><StatusDot label={configured ? "已配置" : "未配置"} variant={configured ? "success" : "warning"} /> <strong>{name}</strong></>}
            />
          ))
          : <ListItem label="没有需要展示的 API key 环境变量。" />}
      </List>
      {dirty && (
        <div id="settingsDirtyBar" className="dirty-bar" role="status">
          <span>有未保存的更改</span>
          <Button id="discardConfigBtn" label="放弃" onClick={() => { setDraft(structuredClone(configPayload?.config ?? {})); setDirty(false); }} />
          <Button id="saveConfigStickyBtn" label="保存配置" onClick={() => void save()} variant="primary" />
        </div>
      )}
      <AlertDialog
        actionLabel="恢复默认"
        actionVariant="destructive"
        cancelLabel="取消"
        description="恢复默认只会修改当前表单，保存前不会写入文件。"
        isOpen={resetOpen}
        onAction={() => { setDraft(defaultConfig()); setDirty(true); setResetOpen(false); }}
        onOpenChange={setResetOpen}
        title="恢复默认配置？"
      />
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
  setNotice(value: { messages: string[]; tone: "success" | "error" | "warning" | "info" }): void;
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
    <FormLayout className="scheduler-form">
      <TextInput id="schedulerJobId" label="任务 id" onChange={(value) => update("id", value)} placeholder="weekly_recording_scan" value={String(job.id || "")} width="100%" />
      <TextInput id="schedulerJobName" label="任务名称" onChange={(value) => update("name", value)} value={String(job.name || "")} width="100%" />
      <CheckboxInput id="schedulerJobEnabled" label="启用任务" onChange={(value) => update("enabled", value)} value={Boolean(job.enabled)} />
      <Selector id="schedulerJobType" label="动作类型" onChange={(value) => update("type", value)} options={[["scan_recordings", "扫描录播"], ["review_due_check", "审阅检查"], ["ai_review", "AI 自动审阅"], ["maintenance_check", "维护检查"]].map(([value, label]) => ({ value, label }))} value={String(job.type || "scan_recordings")} width="100%" />
      <Selector id="schedulerJobSchedule" label="频率" onChange={(value) => update("schedule", value)} options={[["weekly", "每周"], ["daily", "每天"], ["interval_minutes", "每隔 N 分钟"]].map(([value, label]) => ({ value, label }))} value={String(job.schedule || "weekly")} width="100%" />
      <Selector id="schedulerJobDay" label="星期" onChange={(value) => update("day_of_week", value)} options={[["mon", "周一"], ["tue", "周二"], ["wed", "周三"], ["thu", "周四"], ["fri", "周五"], ["sat", "周六"], ["sun", "周日"]].map(([value, label]) => ({ value, label }))} value={String(job.day_of_week || "sun")} width="100%" />
      <TextInput id="schedulerJobTime" label="时间" onChange={(value) => update("time", value)} placeholder="HH:MM" value={String(job.time || "")} width="100%" />
      <NumberInput id="schedulerJobInterval" label="间隔分钟数" onChange={(value) => update("interval_minutes", value)} value={Number(job.interval_minutes || 60)} width="100%" />
      <CheckboxInput id="schedulerJobSkip" label="上次还在运行时跳过" onChange={(value) => update("skip_if_running", value)} value={job.skip_if_running !== false} />
      <Button id="saveSchedulerJobBtn" label="保存定时任务" onClick={() => void saveJob()} />
    </FormLayout>
  );
}
