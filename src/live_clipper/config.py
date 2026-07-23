from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .utils import ensure_dir

DEFAULT_CONFIG_PATH = Path("live-clipper.toml")
DEFAULT_LLM_API_BASE = "https://apihub.agnes-ai.com/v1"
DEFAULT_LLM_MODEL = "agnes-2.0-flash"
DEFAULT_ASR_BACKEND = "mlx_whisper"
DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_OPENAI_ASR_API_BASE = "https://api.openai.com/v1"
DEFAULT_OPENAI_ASR_MODEL = "whisper-1"
DEFAULT_ASR_LANGUAGE = "zh"

DEFAULT_CHEAP_MODEL_API_BASE = DEFAULT_LLM_API_BASE
DEFAULT_CHEAP_MODEL_NAME = DEFAULT_LLM_MODEL

DEFAULT_CONFIG_TEMPLATE = """# live-clipper configuration
#
# Keep API keys in environment variables or .env. This file should contain
# paths, provider choices, and non-secret defaults.

[paths]
input_dir = "input"
output_root = "output"
work_dir = "work"
cache_dir = "work/cache"
log_dir = "work/logs"
state_dir = "work/automation_state"
glossary_path = "glossary/common_terms.json"

[recording_source]
source_dir = ""
since_hours = 36
min_age_minutes = 10

[service]
enabled = true
scan_interval_minutes = 30
auto_render_after_selection = true
cleanup_mode = "preview_only"
# 任务停在「处理中」超过该分钟数且产物已生成时，服务会判定其卡死并强制推进。设为 0 可关闭此兜底。
stuck_after_minutes = 180

[recording_source.default]
source_dir = ""
input_dir = "input"
output_root = "output"
since_hours = 168
min_age_minutes = 10
stable_check_seconds = 60

[asr]
backend = "mlx_whisper"
model = "mlx-community/whisper-large-v3-turbo"
language = "zh"
api_base = "https://api.openai.com/v1"
api_key_env = "ASR_API_KEY"
hf_token_env = "HF_TOKEN"

[llm]
provider_label = "OpenAI-compatible LLM"
api_base = "https://apihub.agnes-ai.com/v1"
api_key_env = "CHEAP_MODEL_API_KEY"
model = "agnes-2.0-flash"
timeout_seconds = 300
request_attempts = 5
retry_delay_seconds = 3.0

[prompts]
# Set this to a directory exported by `live-clipper prompts export`.
directory = ""
profile = "default"

[privacy]
# redacted: write errors but hide prompt/payload/content bodies.
# full: write full debug payloads.
# disabled: skip model failure payload logs where supported.
failure_log_mode = "redacted"
failure_log_max_chars = 2000

[web]
host = "127.0.0.1"
port = 8765
access_token = ""

[review]
reviewer_label = "Codex"
brief_filename = "codex_brief.json"
task_filename = "codex_task.md"
selection_filename = "selected_clips.json"

[render]
ffmpeg_path = "ffmpeg"
output_format = "mp4"

[scheduler]
enabled = true
timezone = "Asia/Shanghai"
tick_seconds = 30
missed_policy = "run_once"
state_dir = "work/service"

[[scheduler.jobs]]
id = "weekly_recording_scan"
name = "每周录播扫描"
enabled = true
type = "scan_recordings"
schedule = "weekly"
day_of_week = "sun"
time = "00:00"
skip_if_running = true

[[scheduler.jobs]]
id = "weekly_review_due"
name = "每周审阅检查"
enabled = true
type = "review_due_check"
schedule = "weekly"
day_of_week = "sun"
time = "12:00"
skip_if_running = true

[review_automation]
enabled = false
mode = "local_agent"
max_runs_per_tick = 1
auto_render_after_selection = true
on_failure = "keep_needs_review"
timeout_minutes = 60
prompt_template = "default_clip_review"

[review_automation.local_agent]
provider = "codex_cli"
command_timeout_minutes = 60
include_review_package_inline = true
allow_agent_file_writes = false

[review_automation.model]
provider = "openai_compatible"
use_llm_config = true
model = ""
max_candidates = 40
temperature = 0.2
max_tokens = 4096
retry_attempts = 2
"""


@dataclass(frozen=True)
class PathsConfig:
    input_dir: Path = Path("input")
    output_root: Path = Path("output")
    work_dir: Path = Path("work")
    cache_dir: Path = Path("work") / "cache"
    log_dir: Path = Path("work") / "logs"
    state_dir: Path = Path("work") / "automation_state"
    glossary_path: Path = Path("glossary") / "common_terms.json"


@dataclass(frozen=True)
class RecordingSourceConfig:
    source_dir: Path | None = None
    since_hours: int = 36
    min_age_minutes: int = 10


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    scan_interval_minutes: int = 30
    auto_render_after_selection: bool = True
    cleanup_mode: str = "preview_only"
    stuck_after_minutes: int = 180


@dataclass(frozen=True)
class RecordingSourceDefaultConfig:
    source_id: str = "default"
    source_dir: Path | None = None
    input_dir: Path = Path("input")
    output_root: Path = Path("output")
    since_hours: int = 168
    min_age_minutes: int = 10
    stable_check_seconds: int = 60


@dataclass(frozen=True)
class ASRConfig:
    backend: str = DEFAULT_ASR_BACKEND
    model: str = DEFAULT_ASR_MODEL
    language: str = DEFAULT_ASR_LANGUAGE
    api_base: str | None = None
    api_key_env: str = "ASR_API_KEY"
    api_key: str | None = None
    hf_token_env: str = "HF_TOKEN"
    hf_token: str | None = None


@dataclass(frozen=True)
class LLMConfig:
    provider_label: str = "OpenAI-compatible LLM"
    api_base: str = DEFAULT_LLM_API_BASE
    api_key_env: str = "CHEAP_MODEL_API_KEY"
    api_key: str | None = None
    model: str = DEFAULT_LLM_MODEL
    timeout_seconds: int = 300
    request_attempts: int = 5
    retry_delay_seconds: float = 3.0


@dataclass(frozen=True)
class PromptsConfig:
    directory: Path | None = None
    profile: str = "default"


@dataclass(frozen=True)
class PrivacyConfig:
    failure_log_mode: str = "redacted"
    failure_log_max_chars: int = 2000


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    access_token: str | None = None


@dataclass(frozen=True)
class ReviewConfig:
    reviewer_label: str = "Codex"
    brief_filename: str = "codex_brief.json"
    task_filename: str = "codex_task.md"
    selection_filename: str = "selected_clips.json"


@dataclass(frozen=True)
class RenderConfig:
    ffmpeg_path: str = "ffmpeg"
    output_format: str = "mp4"


@dataclass(frozen=True)
class SchedulerJobConfig:
    id: str
    name: str
    enabled: bool
    type: str
    schedule: str
    day_of_week: str | None = None
    time: str | None = None
    interval_minutes: int | None = None
    skip_if_running: bool = True


def default_scheduler_jobs() -> list[SchedulerJobConfig]:
    return [
        SchedulerJobConfig(
            id="weekly_recording_scan",
            name="每周录播扫描",
            enabled=True,
            type="scan_recordings",
            schedule="weekly",
            day_of_week="sun",
            time="00:00",
            skip_if_running=True,
        ),
        SchedulerJobConfig(
            id="weekly_review_due",
            name="每周审阅检查",
            enabled=True,
            type="review_due_check",
            schedule="weekly",
            day_of_week="sun",
            time="12:00",
            skip_if_running=True,
        ),
    ]


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    tick_seconds: int = 30
    missed_policy: str = "run_once"
    state_dir: Path = Path("work") / "service"
    jobs: list[SchedulerJobConfig] = field(default_factory=default_scheduler_jobs)


@dataclass(frozen=True)
class ReviewAutomationLocalAgentConfig:
    provider: str = "codex_cli"
    command_timeout_minutes: int = 60
    include_review_package_inline: bool = True
    allow_agent_file_writes: bool = False


@dataclass(frozen=True)
class ReviewAutomationModelConfig:
    provider: str = "openai_compatible"
    use_llm_config: bool = True
    model: str = ""
    max_candidates: int = 40
    temperature: float = 0.2
    max_tokens: int = 4096
    retry_attempts: int = 2


@dataclass(frozen=True)
class ReviewAutomationConfig:
    enabled: bool = False
    mode: str = "local_agent"
    max_runs_per_tick: int = 1
    auto_render_after_selection: bool = True
    on_failure: str = "keep_needs_review"
    timeout_minutes: int = 60
    prompt_template: str = "default_clip_review"
    local_agent: ReviewAutomationLocalAgentConfig = field(default_factory=ReviewAutomationLocalAgentConfig)
    model: ReviewAutomationModelConfig = field(default_factory=ReviewAutomationModelConfig)


@dataclass(frozen=True)
class Settings:
    cheap_model_api_base: str | None = None
    cheap_model_api_key: str | None = None
    cheap_model_name: str | None = None
    asr_backend: str | None = None
    asr_api_base: str | None = None
    asr_api_key: str | None = None
    asr_model: str | None = None
    asr_language: str | None = None
    hf_token: str | None = None
    paths: PathsConfig = field(default_factory=PathsConfig)
    recording_source: RecordingSourceConfig = field(default_factory=RecordingSourceConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    recording_source_default: RecordingSourceDefaultConfig = field(default_factory=RecordingSourceDefaultConfig)
    asr: ASRConfig | None = None
    llm: LLMConfig | None = None
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    web: WebConfig = field(default_factory=WebConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    review_automation: ReviewAutomationConfig = field(default_factory=ReviewAutomationConfig)

    def __post_init__(self) -> None:
        asr_backend = self.asr_backend or (self.asr.backend if self.asr else DEFAULT_ASR_BACKEND)
        asr_model = self.asr_model or (self.asr.model if self.asr else None)
        if asr_model is None:
            asr_model = DEFAULT_OPENAI_ASR_MODEL if asr_backend == "openai" else DEFAULT_ASR_MODEL
        asr_api_base = self.asr_api_base if self.asr_api_base is not None else (self.asr.api_base if self.asr else None)
        if asr_api_base is None and asr_backend == "openai":
            asr_api_base = DEFAULT_OPENAI_ASR_API_BASE
        asr_language = self.asr_language or (self.asr.language if self.asr else DEFAULT_ASR_LANGUAGE)
        asr_api_key = self.asr_api_key if self.asr_api_key is not None else (self.asr.api_key if self.asr else None)
        hf_token = self.hf_token if self.hf_token is not None else (self.asr.hf_token if self.asr else None)

        llm_api_base = self.cheap_model_api_base or (self.llm.api_base if self.llm else DEFAULT_LLM_API_BASE)
        llm_api_key = self.cheap_model_api_key if self.cheap_model_api_key is not None else (self.llm.api_key if self.llm else None)
        llm_model = self.cheap_model_name or (self.llm.model if self.llm else DEFAULT_LLM_MODEL)

        asr = ASRConfig(
            backend=asr_backend,
            model=asr_model,
            language=asr_language,
            api_base=asr_api_base,
            api_key_env=self.asr.api_key_env if self.asr else "ASR_API_KEY",
            api_key=asr_api_key,
            hf_token_env=self.asr.hf_token_env if self.asr else "HF_TOKEN",
            hf_token=hf_token,
        )
        llm = LLMConfig(
            provider_label=self.llm.provider_label if self.llm else "OpenAI-compatible LLM",
            api_base=llm_api_base,
            api_key_env=self.llm.api_key_env if self.llm else "CHEAP_MODEL_API_KEY",
            api_key=llm_api_key,
            model=llm_model,
            timeout_seconds=self.llm.timeout_seconds if self.llm else 300,
            request_attempts=self.llm.request_attempts if self.llm else 5,
            retry_delay_seconds=self.llm.retry_delay_seconds if self.llm else 3.0,
        )

        object.__setattr__(self, "asr", asr)
        object.__setattr__(self, "llm", llm)
        object.__setattr__(self, "asr_backend", asr.backend)
        object.__setattr__(self, "asr_api_base", asr.api_base)
        object.__setattr__(self, "asr_api_key", asr.api_key)
        object.__setattr__(self, "asr_model", asr.model)
        object.__setattr__(self, "asr_language", asr.language)
        object.__setattr__(self, "hf_token", asr.hf_token)
        object.__setattr__(self, "cheap_model_api_base", llm.api_base)
        object.__setattr__(self, "cheap_model_api_key", llm.api_key)
        object.__setattr__(self, "cheap_model_name", llm.model)


def _path_or_none(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    return Path(str(value))


def _path_value(mapping: dict[str, Any], key: str, default: Path) -> Path:
    value = mapping.get(key)
    return Path(str(value)) if value not in (None, "") else default


def _load_config_data(config_path: Path | None) -> dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=True)
    config = _load_config_data(config_path)

    paths_data = config.get("paths", {})
    recording_data = config.get("recording_source", {})
    recording_default_data = {}
    if isinstance(recording_data.get("default"), dict):
        recording_default_data = recording_data.get("default", {})
    service_data = config.get("service", {})
    asr_data = config.get("asr", {})
    llm_data = config.get("llm", {})
    prompts_data = config.get("prompts", {})
    privacy_data = config.get("privacy", {})
    web_data = config.get("web", {})
    review_data = config.get("review", {})
    render_data = config.get("render", {})
    scheduler_data = config.get("scheduler", {})
    review_automation_data = config.get("review_automation", {})
    local_agent_data = (
        review_automation_data.get("local_agent", {})
        if isinstance(review_automation_data, dict) and isinstance(review_automation_data.get("local_agent"), dict)
        else {}
    )
    model_data = (
        review_automation_data.get("model", {})
        if isinstance(review_automation_data, dict) and isinstance(review_automation_data.get("model"), dict)
        else {}
    )

    asr_backend = os.getenv("ASR_BACKEND", str(asr_data.get("backend", DEFAULT_ASR_BACKEND)))
    asr_model = os.getenv("ASR_MODEL", str(asr_data.get("model", "")) or None)
    if asr_model is None:
        asr_model = DEFAULT_OPENAI_ASR_MODEL if asr_backend == "openai" else DEFAULT_ASR_MODEL
    asr_api_key_env = str(asr_data.get("api_key_env", "ASR_API_KEY"))
    hf_token_env = str(asr_data.get("hf_token_env", "HF_TOKEN"))
    asr_api_base_default = DEFAULT_OPENAI_ASR_API_BASE if asr_backend == "openai" else None
    asr = ASRConfig(
        backend=asr_backend,
        model=asr_model,
        language=os.getenv("ASR_LANGUAGE", str(asr_data.get("language", DEFAULT_ASR_LANGUAGE))),
        api_base=os.getenv("ASR_API_BASE", str(asr_data.get("api_base") or asr_api_base_default or "")) or None,
        api_key_env=asr_api_key_env,
        api_key=os.getenv(asr_api_key_env) or os.getenv("ASR_API_KEY"),
        hf_token_env=hf_token_env,
        hf_token=os.getenv(hf_token_env) or os.getenv("HF_TOKEN"),
    )

    llm_api_key_env = str(llm_data.get("api_key_env", "CHEAP_MODEL_API_KEY"))
    llm = LLMConfig(
        provider_label=str(llm_data.get("provider_label", "OpenAI-compatible LLM")),
        api_base=os.getenv("LLM_API_BASE", os.getenv("CHEAP_MODEL_API_BASE", str(llm_data.get("api_base", DEFAULT_LLM_API_BASE)))),
        api_key_env=llm_api_key_env,
        api_key=os.getenv(llm_api_key_env) or os.getenv("CHEAP_MODEL_API_KEY"),
        model=os.getenv("LLM_MODEL", os.getenv("CHEAP_MODEL_NAME", str(llm_data.get("model", DEFAULT_LLM_MODEL)))),
        timeout_seconds=int(llm_data.get("timeout_seconds", 300)),
        request_attempts=int(llm_data.get("request_attempts", 5)),
        retry_delay_seconds=float(llm_data.get("retry_delay_seconds", 3.0)),
    )

    return Settings(
        paths=PathsConfig(
            input_dir=_path_value(paths_data, "input_dir", Path("input")),
            output_root=_path_value(paths_data, "output_root", Path("output")),
            work_dir=_path_value(paths_data, "work_dir", Path("work")),
            cache_dir=_path_value(paths_data, "cache_dir", Path("work") / "cache"),
            log_dir=_path_value(paths_data, "log_dir", Path("work") / "logs"),
            state_dir=_path_value(paths_data, "state_dir", Path("work") / "automation_state"),
            glossary_path=_path_value(paths_data, "glossary_path", Path("glossary") / "common_terms.json"),
        ),
        recording_source=RecordingSourceConfig(
            source_dir=_path_or_none(recording_data.get("source_dir")),
            since_hours=int(recording_data.get("since_hours", 36)),
            min_age_minutes=int(recording_data.get("min_age_minutes", 10)),
        ),
        service=ServiceConfig(
            enabled=bool(service_data.get("enabled", True)),
            scan_interval_minutes=int(service_data.get("scan_interval_minutes", 30)),
            auto_render_after_selection=bool(service_data.get("auto_render_after_selection", True)),
            cleanup_mode=str(service_data.get("cleanup_mode", "preview_only")),
            stuck_after_minutes=int(service_data.get("stuck_after_minutes", 180)),
        ),
        recording_source_default=RecordingSourceDefaultConfig(
            source_id="default",
            source_dir=_path_or_none(recording_default_data.get("source_dir")),
            input_dir=_path_value(recording_default_data, "input_dir", Path("input")),
            output_root=_path_value(recording_default_data, "output_root", Path("output")),
            since_hours=int(recording_default_data.get("since_hours", 168)),
            min_age_minutes=int(recording_default_data.get("min_age_minutes", 10)),
            stable_check_seconds=int(recording_default_data.get("stable_check_seconds", 60)),
        ),
        asr=asr,
        llm=llm,
        prompts=PromptsConfig(
            directory=_path_or_none(prompts_data.get("directory")),
            profile=str(prompts_data.get("profile", "default")),
        ),
        privacy=PrivacyConfig(
            failure_log_mode=os.getenv("FAILURE_LOG_MODE", str(privacy_data.get("failure_log_mode", "redacted"))),
            failure_log_max_chars=int(os.getenv("FAILURE_LOG_MAX_CHARS", str(privacy_data.get("failure_log_max_chars", 2000)))),
        ),
        web=WebConfig(
            host=str(web_data.get("host", "127.0.0.1")),
            port=int(web_data.get("port", 8765)),
            access_token=str(web_data.get("access_token") or "") or None,
        ),
        review=ReviewConfig(
            reviewer_label=str(review_data.get("reviewer_label", "Codex")),
            brief_filename=str(review_data.get("brief_filename", "codex_brief.json")),
            task_filename=str(review_data.get("task_filename", "codex_task.md")),
            selection_filename=str(review_data.get("selection_filename", "selected_clips.json")),
        ),
        render=RenderConfig(
            ffmpeg_path=str(render_data.get("ffmpeg_path", "ffmpeg")),
            output_format=str(render_data.get("output_format", "mp4")),
        ),
        scheduler=SchedulerConfig(
            enabled=bool(scheduler_data.get("enabled", True)),
            timezone=str(scheduler_data.get("timezone", "Asia/Shanghai")),
            tick_seconds=int(scheduler_data.get("tick_seconds", 30)),
            missed_policy=str(scheduler_data.get("missed_policy", "run_once")),
            state_dir=_path_value(scheduler_data, "state_dir", Path("work") / "service"),
            jobs=_scheduler_jobs(scheduler_data.get("jobs")),
        ),
        review_automation=ReviewAutomationConfig(
            enabled=bool(review_automation_data.get("enabled", False)),
            mode=str(review_automation_data.get("mode", "local_agent")),
            max_runs_per_tick=int(review_automation_data.get("max_runs_per_tick", 1)),
            auto_render_after_selection=bool(review_automation_data.get("auto_render_after_selection", True)),
            on_failure=str(review_automation_data.get("on_failure", "keep_needs_review")),
            timeout_minutes=int(review_automation_data.get("timeout_minutes", 60)),
            prompt_template=str(review_automation_data.get("prompt_template", "default_clip_review")),
            local_agent=ReviewAutomationLocalAgentConfig(
                provider=str(local_agent_data.get("provider", "codex_cli")),
                command_timeout_minutes=int(local_agent_data.get("command_timeout_minutes", 60)),
                include_review_package_inline=bool(local_agent_data.get("include_review_package_inline", True)),
                allow_agent_file_writes=bool(local_agent_data.get("allow_agent_file_writes", False)),
            ),
            model=ReviewAutomationModelConfig(
                provider=str(model_data.get("provider", "openai_compatible")),
                use_llm_config=bool(model_data.get("use_llm_config", True)),
                model=str(model_data.get("model", "")),
                max_candidates=int(model_data.get("max_candidates", 40)),
                temperature=float(model_data.get("temperature", 0.2)),
                max_tokens=int(model_data.get("max_tokens", 4096)),
                retry_attempts=int(model_data.get("retry_attempts", 2)),
            ),
        ),
    )


def write_default_config(path: Path = DEFAULT_CONFIG_PATH, *, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    ensure_dir(path.parent)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path


def render_app_config_template(output_root: Path) -> str:
    """Return the cloud-first config used only for a new desktop App home.

    CLI/source installs keep DEFAULT_CONFIG_TEMPLATE unchanged and therefore
    remain local-MLX-first. The slim desktop release does not bundle MLX, so a
    newly created App config must start on the OpenAI-compatible ASR backend.
    """
    template = DEFAULT_CONFIG_TEMPLATE.replace('output_root = "output"', f'output_root = "{output_root}"')
    return template.replace(
        'backend = "mlx_whisper"\nmodel = "mlx-community/whisper-large-v3-turbo"',
        'backend = "openai"\nmodel = "whisper-1"',
        1,
    )


def _scheduler_jobs(raw_jobs: Any) -> list[SchedulerJobConfig]:
    if not isinstance(raw_jobs, list) or not raw_jobs:
        return default_scheduler_jobs()
    jobs = []
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        jobs.append(
            SchedulerJobConfig(
                id=str(raw_job.get("id", "")),
                name=str(raw_job.get("name", "")),
                enabled=bool(raw_job.get("enabled", True)),
                type=str(raw_job.get("type", "")),
                schedule=str(raw_job.get("schedule", "")),
                day_of_week=str(raw_job["day_of_week"]) if raw_job.get("day_of_week") is not None else None,
                time=str(raw_job["time"]) if raw_job.get("time") is not None else None,
                interval_minutes=int(raw_job["interval_minutes"]) if raw_job.get("interval_minutes") is not None else None,
                skip_if_running=bool(raw_job.get("skip_if_running", True)),
            )
        )
    return jobs or default_scheduler_jobs()
