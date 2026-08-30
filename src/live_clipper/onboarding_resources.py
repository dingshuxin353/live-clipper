"""M1 resource checks and secret-safe configuration commits.

This module deliberately keeps credentials in process memory and the explicit
``.env`` path only.  DTO helpers never return request or provider bodies.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import struct
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from dotenv import dotenv_values

from . import asr_models, config_editor
from . import config as config_module
from .config import Settings, load_settings

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_RESPONSE_BYTES = 256 * 1024


class ResourceError(ValueError):
    def __init__(self, code: str, message: str, *, fields: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.fields = fields or {}
        super().__init__(message)


def _display_endpoint(api_base: str | None) -> str | None:
    parsed = urlsplit(str(api_base or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    return f"{parsed.scheme}://{host}{port}{parsed.path.rstrip('/')}"


def normalize_api_base(api_base: str, *, allow_loopback: bool = False) -> str:
    value = str(api_base or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        _port = parsed.port
    except ValueError as exc:
        raise ResourceError("invalid_api_base", "服务地址必须是完整的 HTTPS 地址") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ResourceError("invalid_api_base", "服务地址必须是完整的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ResourceError("invalid_api_base", "服务地址不能包含凭据、查询参数或片段")
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (allow_loopback and loopback):
        raise ResourceError("invalid_api_base", "远程服务必须使用 HTTPS")
    if not loopback:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if (ip is not None and (ip.is_private or ip.is_link_local or ip.is_reserved)) or host.endswith(".internal"):
            raise ResourceError("invalid_api_base", "服务地址不在允许范围内")
    return value


def _bounded_json(response: requests.Response, *, kind: str) -> dict[str, Any]:
    error_code = f"{kind}_request_failed"
    declared_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    try:
        if declared_length is not None and int(declared_length) > MAX_RESPONSE_BYTES:
            raise ResourceError(error_code, "服务响应过大，无法安全处理")
    except (TypeError, ValueError):
        pass
    chunks: list[bytes] = []
    total = 0
    try:
        iterator = response.iter_content(chunk_size=16 * 1024)
        for chunk in iterator:
            if not isinstance(chunk, (bytes, bytearray)):
                raise ResourceError(error_code, "服务响应格式不正确")
            chunk_bytes = bytes(chunk)
            total += len(chunk_bytes)
            if total > MAX_RESPONSE_BYTES:
                raise ResourceError(error_code, "服务响应过大，无法安全处理")
            chunks.append(chunk_bytes)
    except AttributeError:
        content = getattr(response, "content", b"")
        if not isinstance(content, (bytes, bytearray)):
            raise ResourceError(error_code, "服务响应格式不正确") from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise ResourceError(error_code, "服务响应过大，无法安全处理") from None
        chunks.append(bytes(content))
    content = b"".join(chunks)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, requests.RequestException) as exc:
        raise ResourceError(error_code, "服务返回了无法解析的响应") from exc
    if not isinstance(payload, dict):
        raise ResourceError(error_code, "服务返回格式不正确")
    return payload


def _classify_http_error(status: int, *, kind: str) -> str:
    if status in {401, 403}:
        return f"{kind}_auth_failed"
    if status == 404:
        return f"{kind}_model_unavailable"
    return f"{kind}_request_failed"


def _classify_response_error(response: requests.Response, *, kind: str) -> str:
    code = _classify_http_error(response.status_code, kind=kind)
    if code != f"{kind}_request_failed":
        return code
    try:
        payload = _bounded_json(response, kind=kind)
    except ResourceError:
        return code
    details = json.dumps(payload, ensure_ascii=False).lower()
    if any(token in details for token in ("model_not_found", "model not found", "unknown model", "model does not exist")):
        return f"{kind}_model_unavailable"
    return code


def _error_message(code: str) -> str:
    return {
        "asr_unreachable": "无法连接语音识别服务，请检查地址和网络",
        "asr_auth_failed": "语音识别服务认证失败，请检查 API key",
        "asr_model_unavailable": "语音识别模型不可用，请检查模型名称",
        "asr_request_failed": "语音识别服务请求失败，请稍后重试",
        "ai_unreachable": "无法连接 AI 服务，请检查地址和网络",
        "ai_auth_failed": "AI 服务认证失败，请检查 API key",
        "ai_model_unavailable": "AI 模型不可用，请检查模型名称",
        "ai_request_failed": "AI 服务请求失败，请稍后重试",
    }.get(code, "资源测试未通过，请检查设置")


def _result_ok(kind: str, *, api_base: str, model: str) -> dict[str, Any]:
    return {"ok": True, "kind": kind, "api_base_display": _display_endpoint(api_base), "model": model}


def test_ai_service(
    api_base: str,
    model: str,
    api_key: str,
    *,
    timeout_seconds: float = 15,
    allow_loopback: bool = False,
) -> dict[str, Any]:
    """Make a bounded OpenAI-compatible chat request without retaining its body."""
    try:
        endpoint = normalize_api_base(api_base, allow_loopback=allow_loopback)
    except ResourceError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message}
    model_value = str(model or "").strip()
    key_value = str(api_key or "").strip()
    if not model_value or not key_value or any(char in key_value for char in "\r\n\0"):
        return {"ok": False, "error_code": "ai_request_failed", "message": "请填写 AI 服务地址、模型和 API key"}
    try:
        timeout_value = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout_value = 0
    if timeout_value <= 0:
        return {"ok": False, "error_code": "ai_request_failed", "message": "服务请求超时时间无效"}
    response: requests.Response | None = None
    try:
        response = requests.post(
            f"{endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {key_value}", "Content-Type": "application/json"},
            json={
                "model": model_value,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "stream": False,
            },
            timeout=(min(timeout_value, 10), min(timeout_value, 30)),
            allow_redirects=False,
            stream=True,
        )
        if not 200 <= response.status_code < 300:
            code = _classify_response_error(response, kind="ai")
            return {"ok": False, "error_code": code, "message": _error_message(code)}
        _bounded_json(response, kind="ai")
    except ResourceError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message}
    except (requests.RequestException, OSError):
        return {"ok": False, "error_code": "ai_unreachable", "message": _error_message("ai_unreachable")}
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    return _result_ok("ai", api_base=endpoint, model=model_value)


def _minimal_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(struct.pack("<160h", *([0] * 160)))
    return buffer.getvalue()


def test_asr_service(
    api_base: str,
    model: str,
    api_key: str,
    *,
    timeout_seconds: float = 15,
    allow_loopback: bool = False,
) -> dict[str, Any]:
    """Send a tiny in-memory WAV to an OpenAI-compatible transcription endpoint."""
    try:
        endpoint = normalize_api_base(api_base, allow_loopback=allow_loopback)
    except ResourceError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message}
    model_value = str(model or "").strip()
    key_value = str(api_key or "").strip()
    if not model_value or not key_value or any(char in key_value for char in "\r\n\0"):
        return {"ok": False, "error_code": "asr_request_failed", "message": "请填写语音识别服务地址、模型和 API key"}
    try:
        timeout_value = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout_value = 0
    if timeout_value <= 0:
        return {"ok": False, "error_code": "asr_request_failed", "message": "服务请求超时时间无效"}
    response: requests.Response | None = None
    try:
        response = requests.post(
            f"{endpoint}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key_value}"},
            files={"file": ("venus-m1-check.wav", _minimal_wav(), "audio/wav")},
            data={"model": model_value},
            timeout=(min(timeout_value, 10), min(timeout_value, 30)),
            allow_redirects=False,
            stream=True,
        )
        if not 200 <= response.status_code < 300:
            code = _classify_response_error(response, kind="asr")
            return {"ok": False, "error_code": code, "message": _error_message(code)}
        _bounded_json(response, kind="asr")
    except ResourceError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message}
    except (requests.RequestException, OSError):
        return {"ok": False, "error_code": "asr_unreachable", "message": _error_message("asr_unreachable")}
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    return _result_ok("asr", api_base=endpoint, model=model_value)


def test_llm(api_base: str, api_key: str, model: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
    """Compatibility adapter retained for M8's internal resource-repair call."""
    host = (urlsplit(str(api_base or "")).hostname or "").lower()
    result = test_ai_service(
        api_base,
        model,
        api_key,
        timeout_seconds=timeout_seconds,
        allow_loopback=host in {"127.0.0.1", "localhost", "::1"},
    )
    if result.get("ok"):
        return {"ok": True, "message": "连接成功"}
    code = str(result.get("error_code") or "ai_request_failed")
    legacy_code = {"ai_unreachable": "llm_unreachable", "ai_auth_failed": "llm_auth_failed", "ai_model_unavailable": "llm_request_failed", "ai_request_failed": "llm_request_failed"}.get(code, code)
    return {"ok": False, "error_code": legacy_code, "message": result.get("message", "AI 测试失败")}


def _read_env_lines(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    return env_path.read_text(encoding="utf-8").splitlines()


def write_env_secret(env_path: Path, key: str, value: str) -> None:
    if not ENV_VAR_RE.fullmatch(key):
        raise ResourceError("resource_commit_failed", "环境变量名不合法")
    normalized = str(value or "").strip()
    if not normalized or any(char in normalized for char in "\r\n\0"):
        raise ResourceError("resource_commit_failed", "凭据为空或包含无效字符")
    lines = _read_env_lines(env_path)
    replacement = f"{key}={normalized}"
    found = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = replacement
            found = True
    if not found:
        lines.append(replacement)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=env_path.parent, prefix=f".{env_path.name}.") as handle:
            temp_name = handle.name
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp = Path(temp_name)
        temp.chmod(0o600)
        temp.replace(env_path)
        env_path.chmod(0o600)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def load_settings_explicit(config_path: Path, env_path: Path) -> Settings:
    """Load settings using an explicit env file without changing process cwd."""
    # ``config.load_settings`` historically loads ``Path.cwd() / .env``.  M1
    # receives an explicit App-home path, so suppress that implicit lookup for
    # this read and only apply the requested env file below.  Restore the
    # module function even when TOML parsing fails.
    load_dotenv = config_module.load_dotenv
    config_module.load_dotenv = lambda **_kwargs: False
    try:
        settings = load_settings(config_path)
    finally:
        config_module.load_dotenv = load_dotenv
    values = {key: value for key, value in dotenv_values(env_path).items() if value is not None}
    asr_key = settings.asr.api_key_env
    llm_key = settings.llm.api_key_env
    asr_api_key = values.get(asr_key)
    llm_api_key = values.get(llm_key)
    asr = replace(settings.asr, api_key=asr_api_key)
    llm = replace(settings.llm, api_key=llm_api_key)
    return replace(
        settings,
        cheap_model_api_key=llm_api_key,
        asr_api_key=asr_api_key,
        asr=asr,
        llm=llm,
    )


def commit_llm_configuration(
    *,
    config_path: Path,
    env_path: Path,
    provider_label: str,
    api_base: str,
    model: str,
    api_key: str,
    api_key_env: str = "CHEAP_MODEL_API_KEY",
    allow_loopback: bool = False,
) -> dict[str, Any]:
    endpoint = normalize_api_base(api_base, allow_loopback=allow_loopback)
    loaded = config_editor.load_editable_config(config_path=config_path)
    if not loaded.get("ok"):
        raise ResourceError("resource_commit_failed", "无法读取当前配置")
    original = config_path.read_bytes() if config_path.exists() else None
    draft = loaded["config"]
    draft.setdefault("llm", {}).update({"provider_label": str(provider_label or "OpenAI-compatible LLM").strip(), "api_base": endpoint, "model": str(model).strip(), "api_key_env": api_key_env})
    saved = config_editor.save_editable_config(draft, config_path=config_path, backup_root=config_path.parent / "work" / "config_backups", base_dir=config_path.parent)
    if not saved.get("ok"):
        raise ResourceError("resource_commit_failed", "AI 配置保存失败")
    try:
        write_env_secret(env_path, api_key_env, api_key)
    except ResourceError:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
        raise
    except OSError as exc:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
        raise ResourceError("resource_commit_failed", "AI 凭据保存失败") from exc
    os.environ[api_key_env] = str(api_key).strip()
    return {"ok": True, "configured": True, "api_base_display": _display_endpoint(endpoint), "model": str(model).strip(), "provider_label": str(provider_label or "OpenAI-compatible LLM").strip()}


def commit_asr_cloud_configuration(
    *,
    config_path: Path,
    env_path: Path,
    api_base: str,
    model: str,
    api_key: str,
    api_key_env: str = "ASR_API_KEY",
    allow_loopback: bool = False,
) -> dict[str, Any]:
    endpoint = normalize_api_base(api_base, allow_loopback=allow_loopback)
    loaded = config_editor.load_editable_config(config_path=config_path)
    if not loaded.get("ok"):
        raise ResourceError("resource_commit_failed", "无法读取当前配置")
    original = config_path.read_bytes() if config_path.exists() else None
    draft = loaded["config"]
    draft.setdefault("asr", {}).update({"backend": "openai", "api_base": endpoint, "model": str(model).strip(), "api_key_env": api_key_env})
    saved = config_editor.save_editable_config(draft, config_path=config_path, backup_root=config_path.parent / "work" / "config_backups", base_dir=config_path.parent)
    if not saved.get("ok"):
        raise ResourceError("resource_commit_failed", "语音识别配置保存失败")
    try:
        write_env_secret(env_path, api_key_env, api_key)
    except ResourceError:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
        raise
    except OSError as exc:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
        raise ResourceError("resource_commit_failed", "语音识别凭据保存失败") from exc
    os.environ[api_key_env] = str(api_key).strip()
    return {"ok": True, "configured": True, "api_base_display": _display_endpoint(endpoint), "model": str(model).strip(), "mode": "cloud"}


def resource_summaries(settings: Settings, service_dir: Path) -> dict[str, Any]:
    asr = settings.asr
    asr_local_ready = bool(asr and asr.backend != "openai" and asr_models.local_path_for(asr.model))
    asr_cloud_ready = bool(asr and asr.backend == "openai" and asr.api_base and asr.api_key)
    ai_ready = bool(settings.llm and settings.llm.api_base and settings.llm.model and settings.llm.api_key)
    catalog = asr_models.list_models(service_dir)
    safe_catalog = [
        {
            key: item.get(key)
            for key in (
                "id",
                "display_name",
                "backend",
                "tier",
                "tier_label",
                "size_note",
                "ram_note",
                "speed_note",
                "accuracy_note",
                "recommended",
                "state",
                "state_reason",
                "installed",
                "downloading",
                "job_id",
                "installed_bytes",
                "partial_bytes",
                "bytes_downloaded",
                "bytes_total",
                "download_source",
                "current",
            )
        }
        for item in catalog
    ]
    return {
        "asr": {
            "mode": "cloud" if asr and asr.backend == "openai" else "local",
            "configured": bool(asr and asr.model),
            "ready": asr_local_ready or asr_cloud_ready,
            "model_id": asr.model if asr else None,
            "model_label": next((entry["display_name"] for entry in asr_models.REGISTRY if entry["id"] == asr.model), asr.model if asr else None),
            "credential_present": bool(asr and asr.api_key),
            "problem": None if asr_local_ready or asr_cloud_ready else "语音识别资源尚未就绪",
        },
        "ai": {
            "configured": bool(settings.llm and settings.llm.model),
            "ready": ai_ready,
            "provider_label": settings.llm.provider_label if settings.llm else None,
            "api_base_display": _display_endpoint(settings.llm.api_base if settings.llm else None),
            "model": settings.llm.model if settings.llm else None,
            "credential_present": bool(settings.llm and settings.llm.api_key),
            "problem": None if ai_ready else "内容分析资源尚未就绪",
        },
        "model_catalog": safe_catalog,
    }
