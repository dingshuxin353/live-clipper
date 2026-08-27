"""AI review automation executor for validated clip selection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .cheap_model_client import CheapModelClient
from .codex_selection import validate_selected_clips_file
from .config import Settings
from .models import ProjectReviewResult
from .prompt_loader import load_prompt
from .utils import ensure_dir, read_json, write_json

Runner = Callable[..., dict[str, Any]]
ClientFactory = Callable[..., Any]
CommandResolver = Callable[[str], str | None]


class ReviewAutomationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _summary_path(service_dir: Path) -> Path:
    return service_dir / "review_automation.json"


def _events_path(service_dir: Path) -> Path:
    return service_dir / "review_automation_events.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def append_review_event(service_dir: Path, event_type: str, **payload: Any) -> None:
    ensure_dir(service_dir)
    event = {"type": event_type, "created_at": _now(), **payload}
    with _events_path(service_dir).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        from . import service

        service.append_event(service_dir, event_type, **payload)
    except Exception:
        pass


def read_review_automation_events(service_dir: Path, *, max_events: int = 50) -> list[dict[str, Any]]:
    path = _events_path(service_dir)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events[-max_events:]


def read_last_review_summary(service_dir: Path) -> dict[str, Any]:
    """读取最近一次 AI 审阅的结果摘要（不存在时返回空字典）。"""
    path = _summary_path(service_dir)
    return read_json(path) if path.exists() else {}


def get_review_automation_status(settings: Settings, *, service_dir: Path) -> dict[str, Any]:
    summary = read_json(_summary_path(service_dir)) if _summary_path(service_dir).exists() else {}
    return {
        "ok": True,
        "review_automation": {
            "enabled": settings.review_automation.enabled,
            "mode": settings.review_automation.mode,
            "provider": _provider(settings),
            "max_runs_per_tick": settings.review_automation.max_runs_per_tick,
            "auto_render_after_selection": settings.service.auto_render_after_selection,
            "legacy_auto_render_after_selection": settings.review_automation.auto_render_after_selection,
            "last_run_at": summary.get("last_run_at"),
            "last_status": summary.get("last_status"),
            "last_run_id": summary.get("last_run_id"),
            "last_error": summary.get("last_error"),
        },
        "environment": check_environment(settings),
        "events": read_review_automation_events(service_dir),
    }


def check_environment(
    settings: Settings,
    *,
    command_resolver: CommandResolver | None = None,
) -> dict[str, Any]:
    command_resolver = command_resolver or shutil.which
    codex_path = command_resolver("codex")
    claude_path = command_resolver("claude")
    llm_env = settings.llm.api_key_env if settings.llm else "CHEAP_MODEL_API_KEY"
    llm_configured = bool((settings.llm and settings.llm.api_key) or os.getenv(llm_env))
    mode = settings.review_automation.mode
    current_ok = (
        (mode == "local_agent" and bool(codex_path if settings.review_automation.local_agent.provider == "codex_cli" else claude_path))
        or (mode == "model" and llm_configured)
    )
    result = {
        "ok": True,
        "mode": mode,
        "current_mode_available": current_ok,
        "codex_cli": {"available": bool(codex_path), "path": codex_path},
        "claude_code": {"available": bool(claude_path), "path": claude_path},
        "llm": {
            "provider": settings.llm.provider_label if settings.llm else "OpenAI-compatible LLM",
            "api_key_env": llm_env,
            "api_key_configured": llm_configured,
            "model": _model_name(settings),
        },
    }
    return result


def build_review_payload(run: dict[str, Any], *, max_candidates: int = 40) -> dict[str, Any]:
    run_dir = Path(str(run["run_dir"]))
    brief_path = run_dir / "codex_brief.json"
    review_path = run_dir / "codex_review.md"
    template_path = run_dir / "selected_clips.template.json"
    candidates_path = _candidates_path(run_dir)
    candidates = _read_json_list(candidates_path)
    sorted_candidates = sorted(candidates, key=lambda item: float(item.get("score", 0)), reverse=True)
    limited_candidates = sorted_candidates[:max_candidates]
    return {
        "run_id": run.get("run_id"),
        "run_dir": str(run_dir),
        "source_name": Path(str(run.get("source_path") or run.get("run_id"))).name,
        "brief": {"path": brief_path.name, "content": read_json(brief_path) if brief_path.exists() else {}},
        "review_markdown": {"path": review_path.name, "text": review_path.read_text(encoding="utf-8") if review_path.exists() else ""},
        "selection_template": {"path": template_path.name, "content": read_json(template_path) if template_path.exists() else []},
        "refined_candidates": {"path": candidates_path.name, "content": limited_candidates},
        "truncated": len(limited_candidates) < len(candidates),
        "payload_size_bytes": 0,
        "output_contract": {
            "type": "array",
            "path": "selected_clips.json",
            "must_reference_existing_clip_ids": True,
        },
    }


def extract_selection_json(content: str) -> list[dict[str, Any]]:
    text = _strip_json_fence(content)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "[":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise ValueError("AI 输出中没有找到 JSON 数组。")


def extract_review_result_json(content: str) -> dict[str, Any]:
    text = _strip_json_fence(content)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("AI 输出中没有找到 review_result JSON 对象。")


def run_structured_review_adapter(
    settings: Settings,
    payload: dict[str, Any],
    *,
    local_runner: Runner | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Invoke the configured model without granting it any project file access."""
    prompt = load_prompt(
        "project_auto_review.md",
        "Return one versioned review_result JSON object and no hidden reasoning.",
        prompt_dir=settings.prompts.directory,
    )
    if settings.review_automation.mode == "model":
        if not settings.cheap_model_api_key or not settings.cheap_model_name:
            raise ReviewAutomationError("ai_resource_unavailable", "项目审阅资源尚未就绪。")
        factory = client_factory or _cheap_model_client
        client = factory(
            _settings_for_review_model(settings),
            timeout=settings.review_automation.timeout_minutes * 60,
            request_attempts=1,
        )
        result = client.complete_json(
            prompt,
            payload,
            max_tokens=settings.review_automation.model.max_tokens,
            temperature=settings.review_automation.model.temperature,
        )
    else:
        if settings.review_automation.local_agent.allow_agent_file_writes:
            raise ReviewAutomationError("agent_file_writes_disabled", "项目审阅不允许 Agent 直接写文件。")
        runner = local_runner or _default_local_runner
        provider = settings.review_automation.local_agent.provider
        command_name = "codex" if provider == "codex_cli" else "claude"
        if local_runner is None and shutil.which(command_name) is None:
            raise ReviewAutomationError("ai_resource_unavailable", "项目审阅命令资源尚未就绪。")
        with tempfile.TemporaryDirectory(prefix="live-clipper-project-review-") as isolated_dir:
            response = runner(
                f"{prompt}\n\n{json.dumps(payload, ensure_ascii=False)}",
                provider=provider,
                cwd=Path(isolated_dir),
                timeout_seconds=settings.review_automation.local_agent.command_timeout_minutes * 60,
            )
        if not response.get("ok"):
            raise RuntimeError("项目 AI 审阅执行失败")
        result = extract_review_result_json(str(response.get("stdout") or ""))
    return ProjectReviewResult.model_validate(result).model_dump(mode="json")


def run_ai_review_for_run(
    run_id: str,
    settings: Settings,
    *,
    service_dir: Path,
    local_runner: Runner | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    from . import service

    run = service.find_run(run_id, service_dir)
    if run is None:
        return _error("run_not_found", f"任务不存在: {run_id}")
    if run.get("phase") != "needs_review":
        return _error("invalid_phase", "只有 needs_review 任务可以执行 AI 审阅。")
    run_dir = Path(str(run["run_dir"]))
    final_path = run_dir / "selected_clips.json"
    if final_path.exists() and service.selected_clips_status(run_dir)["status"] != "empty":
        return _error("selected_clips_exists", "selected_clips.json 已存在，不重复执行 AI 审阅。")

    append_review_event(service_dir, "ai_review_started", run_id=run_id, mode=settings.review_automation.mode)
    try:
        payload = build_review_payload(run, max_candidates=settings.review_automation.model.max_candidates)
        selection = _run_adapter(settings, payload, run_dir=run_dir, local_runner=local_runner, client_factory=client_factory)
        result = _write_validated_selection(run, selection, service_dir=service_dir)
    except Exception as exc:  # noqa: BLE001 - convert adapter and validation errors into observable local state.
        result = _handle_failure(run, settings, service_dir=service_dir, error=exc)
    _write_summary(settings, service_dir, result, run_id=run_id)
    return result


def run_due_ai_reviews(
    settings: Settings,
    *,
    service_dir: Path,
    local_runner: Runner | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    from . import service

    if not settings.review_automation.enabled:
        append_review_event(service_dir, "ai_review_skipped", reason="review_automation_disabled")
        return {"ok": True, "processed_runs": [], "results": [], "skipped_reason": "review_automation_disabled"}
    processed = []
    results = []
    for run in service.load_runs(service_dir):
        if len(processed) >= settings.review_automation.max_runs_per_tick:
            break
        if run.get("phase") != "needs_review":
            continue
        run_dir = Path(str(run["run_dir"]))
        if (run_dir / "selected_clips.json").exists() and service.selected_clips_status(run_dir)["status"] != "empty":
            continue
        result = run_ai_review_for_run(
            str(run["run_id"]),
            settings,
            service_dir=service_dir,
            local_runner=local_runner,
            client_factory=client_factory,
        )
        results.append(result)
        if result.get("ok"):
            processed.append(str(run["run_id"]))
    return {"ok": all(result.get("ok") for result in results), "processed_runs": processed, "results": results}


def _run_adapter(
    settings: Settings,
    payload: dict[str, Any],
    *,
    run_dir: Path,
    local_runner: Runner | None,
    client_factory: ClientFactory | None,
) -> list[dict[str, Any]]:
    if settings.review_automation.mode == "model":
        return _run_model_adapter(settings, payload, client_factory=client_factory)
    return _run_local_agent_adapter(settings, payload, run_dir=run_dir, local_runner=local_runner)


def _run_local_agent_adapter(
    settings: Settings,
    payload: dict[str, Any],
    *,
    run_dir: Path,
    local_runner: Runner | None,
) -> list[dict[str, Any]]:
    if settings.review_automation.local_agent.allow_agent_file_writes:
        raise ReviewAutomationError(
            "agent_file_writes_disabled",
            "P0 不允许本地 Agent 直接写文件，请关闭 allow_agent_file_writes。",
        )
    provider = settings.review_automation.local_agent.provider
    prompt = _review_prompt(_sanitize_payload_for_local_agent(payload, run_dir=run_dir))
    runner = local_runner or _default_local_runner
    with tempfile.TemporaryDirectory(prefix="live-clipper-ai-review-") as isolated_dir:
        result = runner(
            prompt,
            provider=provider,
            cwd=Path(isolated_dir),
            timeout_seconds=settings.review_automation.local_agent.command_timeout_minutes * 60,
        )
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or result.get("stderr") or "本地 Agent 执行失败。"))
    return extract_selection_json(str(result.get("stdout") or ""))


def _run_model_adapter(
    settings: Settings,
    payload: dict[str, Any],
    *,
    client_factory: ClientFactory | None,
) -> list[dict[str, Any]]:
    factory = client_factory or _cheap_model_client
    client_settings = _settings_for_review_model(settings)
    client = factory(
        client_settings,
        timeout=settings.review_automation.timeout_minutes * 60,
        request_attempts=settings.review_automation.model.retry_attempts + 1,
    )
    selection = client.complete_json(
        "你是短视频选片助手。只返回 JSON 数组，不要删除、移动或清理任何文件。",
        payload,
        max_tokens=settings.review_automation.model.max_tokens,
        temperature=settings.review_automation.model.temperature,
    )
    if not isinstance(selection, list):
        raise ValueError("模型没有返回 JSON 数组。")
    return [item for item in selection if isinstance(item, dict)]


def _write_validated_selection(run: dict[str, Any], selection: list[dict[str, Any]], *, service_dir: Path) -> dict[str, Any]:
    from . import service

    run_dir = Path(str(run["run_dir"]))
    temp_path = run_dir / "selected_clips.tmp.json"
    final_path = run_dir / "selected_clips.json"
    candidates_path = _candidates_path(run_dir)
    if not selection:
        updated = dict(run)
        updated["phase"] = "needs_review"
        updated["selection_result"] = {
            "status": "selection_empty",
            "selected_count": 0,
            "message": "未选出可用片段，可重新执行 AI 审阅或手工选片。",
        }
        updated["last_error"] = None
        updated["updated_at"] = _now()
        service.replace_run(updated, service_dir)
        append_review_event(service_dir, "ai_review_selection_empty", run_id=run.get("run_id"), selected_count=0)
        return {
            "ok": True,
            "run_id": run.get("run_id"),
            "status": "selection_empty",
            "selected_count": 0,
            "message": "未选出可用片段，未创建 selected_clips.json；任务可重新审阅。",
        }
    write_json(temp_path, selection)
    try:
        selected = validate_selected_clips_file(temp_path, candidates_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        append_review_event(service_dir, "ai_review_validation_failed", run_id=run.get("run_id"), error=str(exc))
        raise
    temp_path.replace(final_path)
    append_review_event(
        service_dir,
        "ai_review_selection_written",
        run_id=run.get("run_id"),
        selection_path=str(final_path),
        selected_count=len(selected),
    )
    append_review_event(service_dir, "ai_review_completed", run_id=run.get("run_id"), selection_path=str(final_path))
    return {
        "ok": True,
        "run_id": run.get("run_id"),
        "selection_path": str(final_path),
        "selected_count": len(selected),
        "message": "AI 审阅已生成选片，并通过系统校验。",
    }


def _handle_failure(run: dict[str, Any], settings: Settings, *, service_dir: Path, error: Exception) -> dict[str, Any]:
    from . import service

    run_dir = Path(str(run["run_dir"]))
    (run_dir / "selected_clips.tmp.json").unlink(missing_ok=True)
    if settings.review_automation.on_failure == "mark_failed":
        updated = dict(run)
        updated["phase"] = "failed"
        updated["last_error"] = f"AI 审阅失败: {error}"
        updated["updated_at"] = _now()
        service.replace_run(updated, service_dir)
    if isinstance(error, ReviewAutomationError):
        error_code = error.error_code
    elif "Unknown clip_id" in str(error) or "clip_id" in str(error):
        error_code = "selection_validation_failed"
    else:
        error_code = "ai_review_failed"
    append_review_event(service_dir, "ai_review_failed", run_id=run.get("run_id"), error=str(error), error_code=error_code)
    return _error(error_code, f"AI 审阅失败，未进入渲染：{error}", run_id=run.get("run_id"))


def _write_summary(settings: Settings, service_dir: Path, result: dict[str, Any], *, run_id: str) -> None:
    write_json(
        _summary_path(ensure_dir(service_dir)),
        {
            "enabled": settings.review_automation.enabled,
            "mode": settings.review_automation.mode,
            "provider": _provider(settings),
            "last_run_at": _now(),
            "last_status": "success" if result.get("ok") else "failed",
            "last_run_id": run_id,
            "last_error": None if result.get("ok") else result.get("message"),
        },
    )


def _default_local_runner(prompt: str, *, provider: str, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    if provider == "codex_cli":
        # --skip-git-repo-check: 审阅在隔离的临时目录里执行，该目录不是 git 仓库，
        # 缺少此参数 codex 会以 "Not inside a trusted directory" 拒绝执行。
        command = ["codex", "exec", "--skip-git-repo-check", prompt]
    elif provider == "claude_code":
        command = ["claude", "-p", prompt]
    else:
        return {"ok": False, "error": f"Unsupported local Agent provider: {provider}"}
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            # stdin 必须关闭：常驻/后台服务的 stdin 是不会收到 EOF 的管道，
            # codex/claude 会卡在 "Reading additional input from stdin..." 永久阻塞。
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"命令不存在: {command[0]}", "stderr": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "本地 Agent 执行超时", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": _summarize(completed.stderr),
        "returncode": completed.returncode,
    }


def _cheap_model_client(settings: Settings, *, timeout: int, request_attempts: int) -> CheapModelClient:
    return CheapModelClient(settings, timeout=timeout, request_attempts=request_attempts, retry_delay_seconds=settings.llm.retry_delay_seconds)


def _settings_for_review_model(settings: Settings) -> Settings:
    model_name = settings.review_automation.model.model
    if not model_name:
        return settings
    return Settings(
        cheap_model_api_base=settings.cheap_model_api_base,
        cheap_model_api_key=settings.cheap_model_api_key,
        cheap_model_name=model_name,
        asr=settings.asr,
        llm=settings.llm,
        prompts=settings.prompts,
        privacy=settings.privacy,
        review_automation=settings.review_automation,
    )


def _review_prompt(payload: dict[str, Any]) -> str:
    return (
        "你是 live-clipper 的 AI 审阅助手。请只根据下面的审阅包生成 selected_clips.json 需要的 JSON 数组。\n"
        "禁止删除、移动、清理任何文件；禁止 approve/reject confirmation；不要写文件，只返回 JSON 数组。\n"
        "字段 remove_ranges 必须是 [开始秒, 结束秒] 的数组，例如 [[12.5, 18.0]]；不要用 {\"start\":...} 这样的对象。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _sanitize_payload_for_local_agent(payload: dict[str, Any], *, run_dir: Path) -> dict[str, Any]:
    redacted = _redact_value(payload, str(run_dir))
    if isinstance(redacted, dict):
        redacted["run_dir"] = "[isolated-run-dir]"
        return redacted
    return {"run_dir": "[isolated-run-dir]", "payload": redacted}


def _redact_value(value: Any, needle: str) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item, needle) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, needle) for item in value]
    if isinstance(value, str):
        return value.replace(needle, "[isolated-run-dir]")
    return value


def _candidates_path(run_dir: Path) -> Path:
    refined = run_dir / "refined_candidates.json"
    return refined if refined.exists() else run_dir / "merged_candidates.json"


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    data = read_json(path) if path.exists() else []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        return [item for item in data["candidates"] if isinstance(item, dict)]
    return []


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _summarize(text: str, *, max_chars: int = 2000) -> str:
    return text[:max_chars]


def _provider(settings: Settings) -> str:
    if settings.review_automation.mode == "model":
        return settings.review_automation.model.provider
    return settings.review_automation.local_agent.provider


def _model_name(settings: Settings) -> str:
    configured = settings.review_automation.model.model
    return configured or (settings.llm.model if settings.llm else "")


def _error(error_code: str, message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, "error": message, **payload}
