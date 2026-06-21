"""Client wrapper for the cheap model batch API."""

from __future__ import annotations

import json
from json import JSONDecodeError
import time
from typing import Any

import requests

from .config import Settings
from .utils import write_failure_log


DEFAULT_REQUEST_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 3.0


class CheapModelServiceError(RuntimeError):
    """Raised when the cheap model service cannot complete a request."""


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
                continue
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
    if isinstance(exc, requests.HTTPError):
        status_code = exc.response.status_code if exc.response is not None else None
        return status_code is None or status_code >= 500
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
        ),
    )


class CheapModelClient:
    def __init__(
        self,
        settings: Settings,
        timeout: int = 300,
        request_attempts: int = DEFAULT_REQUEST_ATTEMPTS,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
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

        self.api_base = settings.cheap_model_api_base.rstrip("/")
        self.api_key = settings.cheap_model_api_key
        self.model = settings.cheap_model_name
        self.timeout = timeout
        self.request_attempts = request_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def complete_json(
        self,
        system_prompt: str,
        user_payload: Any,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> Any:
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
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
                )
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
                raise CheapModelServiceError(
                    "Cheap model request failed after "
                    f"{attempt}/{self.request_attempts} attempt(s): {type(exc).__name__}: {exc}"
                ) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                self._write_failure_log(
                    system_prompt,
                    user_payload,
                    getattr(response, "text", ""),
                    exc,
                )
                raise
            try:
                last_content = _extract_message_content(payload)
            except ValueError as exc:
                self._write_failure_log(system_prompt, user_payload, "", exc, payload)
                raise
            try:
                return _loads_model_json(last_content)
            except JSONDecodeError:
                continue
        self._write_failure_log(system_prompt, user_payload, last_content)
        raise ValueError(f"Cheap model returned non-JSON content: {last_content[:200]}")

    def _write_failure_log(
        self,
        system_prompt: str,
        user_payload: Any,
        content: str,
        error: Exception | None = None,
        response_payload: Any | None = None,
        attempt: int | None = None,
    ) -> None:
        payload = {
            "model": self.model,
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "content": content,
        }
        if attempt is not None:
            payload["attempt"] = attempt
            payload["request_attempts"] = self.request_attempts
        if error is not None:
            payload["error_type"] = type(error).__name__
            payload["error"] = str(error)
        if response_payload is not None:
            payload["response_payload"] = response_payload
        write_failure_log("cheap_model_failure", payload)
