"""First-run onboarding: status, connectivity checks, and config completion."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import config_editor
from .config import DEFAULT_CONFIG_PATH, Settings

MARKER_FILENAME = "onboarding.json"

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

PROVIDER_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
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


def _marker_path(service_dir: Path) -> Path:
    return service_dir / MARKER_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def onboarding_status(settings: Settings, service_dir: Path) -> dict[str, Any]:
    marker = _marker_path(service_dir)
    source_dir = settings.recording_source_default.source_dir or settings.recording_source.source_dir
    needs = not marker.exists() and source_dir is None
    return {
        "ok": True,
        "needs_onboarding": needs,
        "completed": marker.exists(),
        "source_dir": str(source_dir) if source_dir else "",
        "output_root": str(settings.paths.output_root),
        "llm_key_present": bool(settings.cheap_model_api_key),
        "asr_api_base": str(settings.asr.api_base or ""),
        "asr_model": settings.asr.model,
        "asr_key_present": bool(settings.asr.api_key),
        "presets": PROVIDER_PRESETS,
    }


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
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    asr_api_base = str(payload.get("asr_api_base") or "").strip().rstrip("/")
    asr_model = str(payload.get("asr_model") or "").strip()
    asr_api_key = str(payload.get("asr_api_key") or "").strip()

    source_check = test_recording_source(source_dir)
    if not source_check["ok"]:
        return source_check
    if not api_base or not model:
        return {"ok": False, "error_code": "llm_fields_missing", "message": "请先填写 AI 服务地址和模型"}
    if not asr_api_base or not asr_model or not asr_api_key:
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
    asr_section["backend"] = "openai"
    asr_section["api_base"] = asr_api_base
    asr_section["model"] = asr_model
    asr_section["api_key_env"] = "ASR_API_KEY"

    saved = config_editor.save_editable_config(draft, config_path=config_path)
    if not saved["ok"]:
        return saved

    if api_key:
        _write_env_key(env_path, "CHEAP_MODEL_API_KEY", api_key)
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
    }
