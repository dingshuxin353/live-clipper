"""First-run onboarding: status, connectivity checks, and config completion."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import asr_models, config_editor
from .config import DEFAULT_CONFIG_PATH, Settings

MARKER_FILENAME = "onboarding.json"
FIRST_RUN_LOCAL_MODEL_ID = "mlx-community/whisper-small-mlx-q4"

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

PROVIDER_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "api_base": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "signup_url": "https://platform.deepseek.com",
    },
    {
        "id": "qwen",
        "label": "通义千问（阿里云百炼）",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "signup_url": "https://bailian.console.aliyun.com",
    },
    {
        "id": "kimi",
        "label": "Kimi（月之暗面）",
        "api_base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "signup_url": "https://platform.moonshot.cn",
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "api_base": "",
        "model": "",
        "signup_url": "",
    },
]

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _marker_path(service_dir: Path) -> Path:
    return service_dir / MARKER_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _marker_state(marker: Path) -> tuple[bool, bool]:
    if not marker.exists():
        return False, False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, False
    if isinstance(payload, dict) and payload.get("skipped_at") and not payload.get("completed_at"):
        return False, True
    return True, False


def onboarding_status(settings: Settings, service_dir: Path) -> dict[str, Any]:
    marker = _marker_path(service_dir)
    source_dir = settings.recording_source_default.source_dir or settings.recording_source.source_dir
    completed, skipped = _marker_state(marker)
    needs = not marker.exists() and source_dir is None
    return {
        "ok": True,
        "needs_onboarding": needs,
        "completed": completed,
        "skipped": skipped,
        "source_dir": str(source_dir) if source_dir else "",
        "output_root": str(settings.paths.output_root),
        "llm_key_present": bool(settings.cheap_model_api_key),
        "asr_api_base": str(settings.asr.api_base or ""),
        "asr_model": settings.asr.model,
        "asr_key_present": bool(settings.asr.api_key),
        "asr_backend": settings.asr.backend,
        "initial_asr_mode": "local",
        "initial_local_model": FIRST_RUN_LOCAL_MODEL_ID,
        "presets": PROVIDER_PRESETS,
    }


def skip_onboarding(*, service_dir: Path = Path("work") / "service") -> dict[str, Any]:
    marker = _marker_path(service_dir)
    if marker.exists():
        completed, skipped = _marker_state(marker)
        if skipped:
            return {"ok": True, "skipped": True, "completed": False}
        if completed:
            return {
                "ok": True,
                "skipped": False,
                "completed": True,
                "already_finished": True,
            }
    service_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"skipped_at": _now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "skipped": True, "completed": False}


def test_recording_source(source_dir: str) -> dict[str, Any]:
    value = (source_dir or "").strip()
    if not value:
        return {"ok": False, "error_code": "empty_source_dir", "message": "请填写录播文件夹路径"}
    path = Path(value).expanduser()
    if not path.exists():
        return {
            "ok": False,
            "error_code": "source_dir_missing",
            "message": f"找不到该文件夹：{path}。如果是 NAS，请先在访达中连接服务器。",
        }
    if not path.is_dir():
        return {"ok": False, "error_code": "source_dir_not_directory", "message": f"该路径不是文件夹：{path}"}
    try:
        video_count = sum(
            1 for item in path.iterdir() if item.is_file() and item.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        )
    except OSError as exc:
        return {"ok": False, "error_code": "source_dir_unreadable", "message": f"无法读取该文件夹：{exc}"}
    return {"ok": True, "path": str(path), "video_count": video_count}


def test_llm(api_base: str, api_key: str, model: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
    api_base = (api_base or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not api_base or not api_key or not model:
        return {"ok": False, "error_code": "llm_fields_missing", "message": "请先填写服务地址、模型和 API key"}
    try:
        response = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "回复：OK"}],
                "max_tokens": 8,
                "stream": False,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error_code": "llm_unreachable", "message": f"无法连接到 {api_base}：{type(exc).__name__}"}
    if response.status_code in (401, 403):
        return {"ok": False, "error_code": "llm_auth_failed", "message": "API key 无效或没有权限，请检查后重试"}
    if response.status_code >= 400:
        return {
            "ok": False,
            "error_code": "llm_request_failed",
            "message": f"服务返回错误（HTTP {response.status_code}），请检查模型名称是否正确",
        }
    return {"ok": True, "message": "连接成功"}


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    encoded = "\n".join(lines) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(env_path.parent),
            prefix=f".{env_path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).chmod(0o600)
        Path(temp_name).replace(env_path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def save_llm_api_key(
    api_key: str,
    *,
    api_key_env: str = "CHEAP_MODEL_API_KEY",
    env_path: Path = Path(".env"),
) -> dict[str, Any]:
    """Persist one LLM secret without returning or logging its value."""
    api_key_env = (api_key_env or "").strip()
    raw_api_key = api_key or ""
    normalized_api_key = raw_api_key.strip()
    if not ENV_VAR_RE.fullmatch(api_key_env):
        return {
            "ok": False,
            "saved": False,
            "error_code": "invalid_api_key_env",
            "message": "API key 环境变量名不合法，请先检查 AI 设置",
        }
    if not normalized_api_key:
        return {
            "ok": False,
            "saved": False,
            "error_code": "empty_api_key",
            "message": "请粘贴或输入 AI API key",
        }
    if any(character in normalized_api_key for character in ("\r", "\n", "\0")):
        return {
            "ok": False,
            "saved": False,
            "error_code": "invalid_api_key",
            "message": "API key 包含无效换行，请重新复制后粘贴",
        }

    env_path.parent.mkdir(parents=True, exist_ok=True)
    _write_env_key(env_path, api_key_env, normalized_api_key)
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    os.environ[api_key_env] = normalized_api_key
    return {
        "ok": True,
        "saved": True,
        "api_key_env": api_key_env,
        "message": "AI API key 已保存",
    }


def complete_onboarding(
    payload: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = Path(".env"),
    service_dir: Path = Path("work") / "service",
) -> dict[str, Any]:
    source_dir = str(payload.get("source_dir") or "").strip()
    api_base = str(payload.get("llm_api_base") or "").strip().rstrip("/")
    model = str(payload.get("llm_model") or "").strip()
    api_key = str(payload.get("llm_api_key") or "").strip()
    asr_mode = str(payload.get("asr_mode") or "").strip()
    asr_api_base = str(payload.get("asr_api_base") or "").strip().rstrip("/")
    asr_model = str(payload.get("asr_model") or "").strip()
    asr_api_key = str(payload.get("asr_api_key") or "").strip()
    asr_model_source = str(payload.get("asr_model_source") or "").strip()

    source_check = test_recording_source(source_dir)
    if not source_check["ok"]:
        return source_check
    if not api_base or not model:
        return {"ok": False, "error_code": "llm_fields_missing", "message": "请先填写 AI 服务地址和模型"}
    if "ASR_BACKEND" in os.environ or "ASR_MODEL" in os.environ:
        return {
            "ok": False,
            "error_code": "asr_overridden_by_environment",
            "message": "当前环境变量覆盖了语音识别配置，请移除 ASR_BACKEND / ASR_MODEL 后重试",
        }
    if asr_mode not in {"local", "cloud"}:
        return {"ok": False, "error_code": "invalid_asr_mode", "message": "未知的语音识别模式"}

    local_entry: dict[str, Any] | None = None
    if asr_mode == "local":
        if asr_model not in asr_models.registry_ids():
            return {"ok": False, "error_code": "unknown_model", "message": "未知的本地语音识别模型"}
        if asr_model_source == "hf-mirror":
            return {
                "ok": False,
                "error_code": "unsupported_model_source",
                "message": asr_models.HF_MIRROR_REMOVED_MESSAGE,
            }
        if asr_model_source not in asr_models.source_ids():
            return {"ok": False, "error_code": "unknown_model_source", "message": "未知的模型下载源"}
        if asr_models.local_path_for(asr_model) is None:
            return {
                "ok": False,
                "error_code": "model_not_ready",
                "message": "所选本地模型尚未完整安装，请先完成下载",
            }
        local_entry = asr_models.model_entry(asr_model)
    elif not asr_api_base or not asr_model or not asr_api_key:
        return {
            "ok": False,
            "error_code": "asr_fields_missing",
            "message": "请填写语音识别服务地址、模型和 API key",
        }

    loaded = config_editor.load_editable_config(config_path=config_path)
    if not loaded["ok"]:
        return loaded
    draft = loaded["config"]
    draft.setdefault("recording_source_default", {})["source_dir"] = source_check["path"]
    llm_section = draft.setdefault("llm", {})
    llm_section["api_base"] = api_base
    llm_section["model"] = model
    asr_section = draft.setdefault("asr", {})
    if asr_mode == "local":
        assert local_entry is not None
        asr_section["backend"] = local_entry["backend"]
        asr_section["model"] = local_entry["id"]
        asr_section["model_source"] = asr_model_source
    else:
        asr_section["backend"] = "openai"
        asr_section["api_base"] = asr_api_base
        asr_section["model"] = asr_model
        asr_section["api_key_env"] = "ASR_API_KEY"

    saved = config_editor.save_editable_config(draft, config_path=config_path)
    if not saved["ok"]:
        return saved

    if api_key:
        _write_env_key(env_path, "CHEAP_MODEL_API_KEY", api_key)
    if asr_mode == "cloud":
        _write_env_key(env_path, "ASR_API_KEY", asr_api_key)

    service_dir.mkdir(parents=True, exist_ok=True)
    _marker_path(service_dir).write_text(
        json.dumps({"completed_at": _now_iso(), "source_dir": source_check["path"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "message": "初始设置完成",
        "config_path": str(config_path),
        "requires_service_restart": bool(saved.get("requires_service_restart")),
        "asr_mode": asr_mode,
        "current_backend": asr_section["backend"],
        "current_model": asr_section["model"],
        "model_source": asr_section.get("model_source"),
    }
