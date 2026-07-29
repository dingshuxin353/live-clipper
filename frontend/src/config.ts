import type { GenericRecord } from "./types";

export const CONFIG_FIELDS = [
  "recording_source_default.source_dir",
  "paths.workspace_root",
  "llm.api_base",
  "llm.model",
  "llm.api_key_env",
  "asr.backend",
  "service.auto_render_after_selection",
  "scheduler.enabled",
  "review_automation.enabled",
  "review_automation.mode",
  "review_automation_local_agent.provider",
  "review_automation_model.model",
  "review_automation.max_runs_per_tick",
  "review_automation.auto_render_after_selection",
  "paths.work_dir",
  "paths.glossary_path",
  "recording_source_default.since_hours",
  "recording_source_default.min_age_minutes",
  "recording_source_default.stable_check_seconds",
  "service.enabled",
  "service.cleanup_mode",
  "asr.model",
  "asr.language",
  "asr.api_base",
  "asr.api_key_env",
  "asr.hf_token_env",
  "asr.model_source",
  "llm.provider_label",
  "llm.timeout_seconds",
  "llm.request_attempts",
  "llm.retry_delay_seconds",
  "service.scan_interval_minutes",
  "scheduler.timezone",
  "scheduler.tick_seconds",
  "scheduler.missed_policy",
  "review_automation.timeout_minutes",
  "review_automation.on_failure",
  "review_automation.prompt_template",
  "review_automation_local_agent.command_timeout_minutes",
  "review_automation_local_agent.include_review_package_inline",
  "review_automation_local_agent.allow_agent_file_writes",
  "review_automation_model.max_candidates",
  "review_automation_model.temperature",
  "review_automation_model.max_tokens",
  "review_automation_model.retry_attempts",
  "web.host",
  "web.port",
] as const;

export type ConfigField = (typeof CONFIG_FIELDS)[number];

export function getConfigValue(config: GenericRecord, field: ConfigField): unknown {
  const [section, key] = field.split(".");
  return config[section]?.[key];
}

export function setConfigValue(
  config: GenericRecord,
  field: ConfigField,
  value: unknown,
): GenericRecord {
  const [section, key] = field.split(".");
  return {
    ...config,
    [section]: {
      ...(config[section] ?? {}),
      [key]: value,
    },
  };
}

export function defaultConfig(): GenericRecord {
  return {
    paths: {
      input_dir: "input",
      output_root: "output",
      workspace_root: "output",
      work_dir: "work",
      glossary_path: "glossary/common_terms.json",
    },
    recording_source_default: {
      source_dir: "",
      input_dir: "input",
      output_root: "output",
      since_hours: 168,
      min_age_minutes: 10,
      stable_check_seconds: 60,
    },
    llm: {
      provider_label: "OpenAI-compatible LLM",
      api_base: "https://apihub.agnes-ai.com/v1",
      api_key_env: "CHEAP_MODEL_API_KEY",
      model: "agnes-2.0-flash",
      timeout_seconds: 300,
      request_attempts: 5,
      retry_delay_seconds: 3,
    },
    asr: {
      backend: "mlx_whisper",
      model: "mlx-community/whisper-large-v3-turbo",
      language: "zh",
      api_base: "https://api.openai.com/v1",
      api_key_env: "ASR_API_KEY",
      hf_token_env: "HF_TOKEN",
      model_source: "modelscope",
    },
    service: {
      enabled: true,
      scan_interval_minutes: 30,
      auto_render_after_selection: true,
      cleanup_mode: "preview_only",
    },
    scheduler: {
      enabled: true,
      timezone: "Asia/Shanghai",
      tick_seconds: 30,
      missed_policy: "run_once",
      state_dir: "work/service",
    },
    scheduler_jobs: [
      {
        id: "weekly_recording_scan",
        name: "每周录播扫描",
        enabled: true,
        type: "scan_recordings",
        schedule: "weekly",
        day_of_week: "sun",
        time: "00:00",
        skip_if_running: true,
      },
      {
        id: "weekly_review_due",
        name: "每周审阅检查",
        enabled: true,
        type: "review_due_check",
        schedule: "weekly",
        day_of_week: "sun",
        time: "12:00",
        skip_if_running: true,
      },
    ],
    review_automation: {
      enabled: false,
      mode: "local_agent",
      max_runs_per_tick: 1,
      auto_render_after_selection: true,
      on_failure: "keep_needs_review",
      timeout_minutes: 60,
      prompt_template: "default_clip_review",
    },
    review_automation_local_agent: {
      provider: "codex_cli",
      command_timeout_minutes: 60,
      include_review_package_inline: true,
      allow_agent_file_writes: false,
    },
    review_automation_model: {
      provider: "openai_compatible",
      use_llm_config: true,
      model: "",
      max_candidates: 40,
      temperature: 0.2,
      max_tokens: 4096,
      retry_attempts: 2,
    },
    web: { host: "127.0.0.1", port: 8765 },
  };
}
