"""Client wrapper for the cheap model batch API."""

from __future__ import annotations

import json
import time
from json import JSONDecodeError
from typing import Any

import requests

from .config import Settings
from .utils import write_failure_log

DEFAULT_REQUEST_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 3.0


class CheapModelServiceError(RuntimeError):
    """A stable, secret-free failure category shared by probes and real calls."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def request_error_code(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return 'connection_timeout'
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return 'result_unknown'
    status = exc.response.status_code if exc.response is not None else None
    return {401: 'credential_invalid', 403: 'permission_or_quota', 402: 'permission_or_quota',
            429: 'rate_limited', 404: 'model_not_found', 400: 'parameter_unsupported', 422: 'parameter_unsupported'}.get(status, 'result_unknown')


def emit_progress(message: str) -> None:
    print(message, flush=True)


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _loads_model_json(content: str) -> Any:
    stripped = _strip_json_fence(content)
    try:
        return json.loads(stripped)
    except JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(stripped):
            if char not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[index:])
                return parsed
            except JSONDecodeError:
                break
        raise


def _extract_message_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Cheap model response must be OpenAI-compatible choices[0].message.content") from exc
    if not isinstance(content, str):
        raise ValueError("Cheap model response content must be a string")
    return content


def _is_retryable_request_exception(exc: requests.RequestException) -> bool:
    # Only a failed connection establishment proves the request was never accepted.
    return isinstance(exc, requests.exceptions.ConnectTimeout)


class CheapModelClient:
    def __init__(
        self,
        settings: Settings,
        timeout: int | None = None,
        request_attempts: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> None:
        timeout = settings.llm.timeout_seconds if timeout is None else timeout
        request_attempts = settings.llm.request_attempts if request_attempts is None else request_attempts
        retry_delay_seconds = settings.llm.retry_delay_seconds if retry_delay_seconds is None else retry_delay_seconds
        if not settings.cheap_model_api_base:
            raise ValueError("CHEAP_MODEL_API_BASE is required")
        if not settings.cheap_model_api_key:
            raise ValueError("CHEAP_MODEL_API_KEY is required")
        if not settings.cheap_model_name:
            raise ValueError("CHEAP_MODEL_NAME is required")
        if request_attempts <= 0:
            raise ValueError("request_attempts must be greater than 0")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")

        self.request_profile = settings.llm.request_profile
        self.api_base = settings.cheap_model_api_base.rstrip("/")
        self.api_key = settings.cheap_model_api_key
        self.model = settings.cheap_model_name
        self.timeout = timeout
        self.request_attempts = request_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.failure_log_mode = settings.privacy.failure_log_mode
        self.failure_log_max_chars = settings.privacy.failure_log_max_chars

    def complete_json(
        self,
        system_prompt: str,
        user_payload: Any,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> Any:
        from .resource_providers import request_parameters

        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            **request_parameters(self.request_profile, temperature=temperature, max_tokens=max_tokens),
            "stream": False,
        }
        last_content = ""
        for attempt in range(1, self.request_attempts + 1):
            try:
                response = requests.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if 300 <= response.status_code < 400:
                    raise CheapModelServiceError("redirect_not_allowed")
                response.raise_for_status()
            except requests.RequestException as exc:
                retryable = _is_retryable_request_exception(exc)
                if retryable and attempt < self.request_attempts:
                    delay = self.retry_delay_seconds * attempt
                    emit_progress(
                        f"[Agnes] 请求失败 ({type(exc).__name__}), "
                        f"{delay:g} 秒后重试 {attempt}/{self.request_attempts - 1}"
                    )
                    if delay:
                        time.sleep(delay)
                    continue
                self._write_failure_log(system_prompt, user_payload, "", exc, attempt=attempt)
                raise CheapModelServiceError(request_error_code(exc)) from None
            try:
                payload = response.json()
            except ValueError as exc:
                self._write_failure_log(
                    system_prompt,
                    user_payload,
                    getattr(response, "text", ""),
                    exc,
                )
                raise CheapModelServiceError("output_format_invalid") from None
            try:
                last_content = _extract_message_content(payload)
            except ValueError as exc:
                self._write_failure_log(system_prompt, user_payload, "", exc, payload)
                raise CheapModelServiceError("output_format_invalid") from None
            try:
                return _loads_model_json(last_content)
            except JSONDecodeError:
                break
        self._write_failure_log(system_prompt, user_payload, last_content)
        raise CheapModelServiceError("output_format_invalid")

    def _write_failure_log(
        self,
        system_prompt: str,
        user_payload: Any,
        content: str,
        error: Exception | None = None,
        response_payload: Any | None = None,
        attempt: int | None = None,
    ) -> None:
        if self.failure_log_mode == "disabled":
            return
        payload = {"model": self.model, "error_type": type(error).__name__ if error else "InvalidOutput",
                   "error_code": request_error_code(error) if isinstance(error, requests.RequestException) else "output_format_invalid"}
        if attempt is not None:
            payload["attempt"] = attempt
            payload["request_attempts"] = self.request_attempts
        write_failure_log("cheap_model_failure", payload)


def discover_models(api_base: str, api_key: str, *, timeout: int = 30) -> list[str]:
    """Explicit metadata lookup; results do not confer capability validation."""
    try:
        response = requests.get(f"{api_base.rstrip('/')}/models", headers={'Authorization': f'Bearer {api_key}'}, timeout=timeout, allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise CheapModelServiceError('redirect_not_allowed')
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise CheapModelServiceError('model_list_unavailable')
        return sorted({item['id'] for item in payload['data'][:10000] if isinstance(item, dict) and isinstance(item.get('id'), str) and 0 < len(item['id']) <= 512 and not any(ord(c) < 32 for c in item['id'])})
    except requests.RequestException as exc:
        raise CheapModelServiceError(request_error_code(exc)) from None
    except ValueError:
        raise CheapModelServiceError('model_list_unavailable') from None
