"""Shared compatibility adapters retained for the M1 resource contract."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

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
    {"id": "custom", "label": "自定义（OpenAI 兼容）", "api_base": "", "model": "", "signup_url": ""},
]

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def test_llm(api_base: str, api_key: str, model: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
    """Compatibility adapter for M8's internal resource-repair call."""
    from .onboarding_resources import test_llm as shared_test_llm

    return shared_test_llm(api_base, api_key, model, timeout_seconds=timeout_seconds)


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = replacement
            continue
    else:
        lines.append(replacement)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=env_path.parent, prefix=f".{env_path.name}.") as handle:
            temporary = handle.name
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path = Path(temporary)
        temporary_path.chmod(0o600)
        temporary_path.replace(env_path)
        env_path.chmod(0o600)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def save_llm_api_key(
    api_key: str,
    *,
    api_key_env: str = "CHEAP_MODEL_API_KEY",
    env_path: Path = Path(".env"),
) -> dict[str, Any]:
    """Persist one LLM secret without returning or logging its value."""
    normalized_env = (api_key_env or "").strip()
    normalized_key = (api_key or "").strip()
    if not ENV_VAR_RE.fullmatch(normalized_env):
        return {"ok": False, "saved": False, "error_code": "invalid_api_key_env", "message": "API key 环境变量名不合法"}
    if not normalized_key:
        return {"ok": False, "saved": False, "error_code": "empty_api_key", "message": "请粘贴或输入 AI API key"}
    if any(character in normalized_key for character in ("\r", "\n", "\0")):
        return {"ok": False, "saved": False, "error_code": "invalid_api_key", "message": "API key 包含无效字符"}
    env_path.parent.mkdir(parents=True, exist_ok=True)
    _write_env_key(env_path, normalized_env, normalized_key)
    os.environ[normalized_env] = normalized_key
    return {"ok": True, "saved": True, "api_key_env": normalized_env, "message": "AI API key 已保存"}
