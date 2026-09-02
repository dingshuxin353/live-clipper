"""The HTTP-facing M1 first-run state machine and finish saga."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import asr_models, config_editor, onboarding, onboarding_resources, service
from .config import Settings
from .first_run_detection import detect_first_run_environment, inspect_startup
from .first_run_state import FirstRunSession, FirstRunStateError, StartupDecision
from .project_domain import default_project_config, project_config_v2
from .project_result_domain import RequestConflictError, RevisionConflictError
from .project_service import (
    ProjectError,
    ProjectManager,
    _request_hash,
    open_project_repository,
    output_directory_status,
)
from .project_storage import ProjectRepository, database_path


class OnboardingError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 409, fields: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        super().__init__(message)


_COMMON_REQUEST_FIELDS = frozenset({"request_id", "expected_revision"})
_FINISH_LOCKS: dict[str, threading.Lock] = {}
_FINISH_LOCKS_GUARD = threading.Lock()


def _strict_body(body: Any, allowed: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise OnboardingError("validation_failed", "请求体必须是对象", status=422)
    unknown = set(body) - set(allowed)
    if unknown:
        raise OnboardingError(
            "validation_failed",
            "请求包含未知字段",
            status=422,
            fields={str(key): "未知字段" for key in sorted(unknown, key=str)},
        )
    return body


def _safe_session(session: FirstRunSession | None) -> dict[str, Any] | None:
    if session is None:
        return None
    failure = None
    if session.failure_code:
        failure = {"code": session.failure_code, "summary": session.failure_summary}
    return {
        "state": session.state,
        "current_step": session.current_step,
        "revision": session.revision,
        "draft": session.draft,
        "pending_finish_request_id": session.project_request_id,
        "failure": failure,
        "first_project": None,
    }


def _safe_session_from_repository(repository: ProjectRepository, session: FirstRunSession | None) -> dict[str, Any] | None:
    payload = _safe_session(session)
    if payload is None or session is None or not session.first_project_id:
        return payload
    project = repository.get_project(session.first_project_id)
    if project is None:
        return payload
    runtime = repository.get_runtime(project.project_id)
    payload["first_project"] = {
        "project_id": project.project_id,
        "name": project.name,
        "activation_state": project.activation_state,
        "readiness_state": runtime.readiness_state if runtime is not None else "blocked",
    }
    return payload


def _request_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128:
        raise OnboardingError("validation_failed", "操作信息无效，请重新打开首次设置", status=422, fields={"request_id": "无效"})
    return normalized


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OnboardingError("validation_failed", "首次设置状态无效，请重新打开", status=422, fields={"expected_revision": "无效"})
    return value


def _normalize_draft_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Normalize endpoint fields before they become durable, secret-free draft data."""
    normalized = deepcopy(patch)
    for section in ("asr", "ai"):
        values = normalized.get(section)
        if not isinstance(values, dict) or "api_base" not in values:
            continue
        try:
            values["api_base"] = onboarding_resources.normalize_api_base(
                str(values["api_base"]), allow_loopback=True
            )
        except onboarding_resources.ResourceError as exc:
            raise OnboardingError("validation_failed", "服务地址无效，无法保存首次设置草稿", status=422, fields={f"{section}.api_base": "URL 无效"}) from exc
    return normalized


class OnboardingCoordinator:
    def __init__(
        self,
        *,
        service_dir: str | Path,
        config_path: str | Path,
        env_path: str | Path | None = None,
        input_dir: str | Path = "input",
        output_root: str | Path = "output",
        settings_loader: Callable[[], Settings] | None = None,
    ) -> None:
        self.service_dir = Path(service_dir).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.env_path = Path(env_path).expanduser().resolve() if env_path else self.config_path.parent / ".env"
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self._settings_loader = settings_loader

    def settings(self) -> Settings:
        if self._settings_loader is not None:
            return self._settings_loader()
        return onboarding_resources.load_settings_explicit(self.config_path, self.env_path)

    def decision(self) -> tuple[StartupDecision, Any]:
        decision = inspect_startup(
            config_path=self.config_path,
            env_path=self.env_path,
            service_dir=self.service_dir,
        )
        detection = detect_first_run_environment(
            config_path=self.config_path,
            env_path=self.env_path,
            service_dir=self.service_dir,
        )
        return decision, detection

    def _repo(self) -> ProjectRepository:
        try:
            return open_project_repository(
                self.service_dir,
                config_path=self.config_path,
                env_path=self.env_path,
            )
        except ProjectError as exc:
            raise OnboardingError(exc.code, exc.message, status=exc.status, fields=exc.fields) from exc

    def _read_session(self, decision: StartupDecision) -> tuple[ProjectRepository | None, FirstRunSession | None]:
        if decision.entry in {"migration_required", "diagnostic_required"}:
            return None, None
        if decision.onboarding == "new" and not database_path(self.service_dir).exists():
            return None, None
        repo = self._repo()
        return repo, repo.get_first_run_session()

    def _environment_summary(self, *, run_probes: bool = False, settings: Settings | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        app_home = self.config_path.parent

        def check(name: str, ok: bool, problem: str | None = None) -> None:
            checks.append({"name": name, "status": "ready" if ok else "blocked", "problem": problem})

        def writable_target(path: Path) -> bool:
            target = path.expanduser()
            if not target.is_absolute():
                target = app_home / target
            parent = target
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                return False
            probe: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".venus-m1-probe-", dir=parent, delete=False) as handle:
                    probe = Path(handle.name)
                return True
            except OSError:
                return False
            finally:
                if probe is not None:
                    try:
                        probe.unlink(missing_ok=True)
                    except OSError:
                        pass

        app_home_ok = writable_target(app_home)
        service_dir_ok = writable_target(self.service_dir)
        check("app_home", app_home_ok, None if app_home_ok else "应用目录不可写")
        check("service_dir", service_dir_ok, None if service_dir_ok else "服务目录不可创建")
        if settings is None:
            try:
                settings = self.settings()
            except Exception:  # noqa: BLE001 - an unreadable config is reported by the startup classifier.
                settings = Settings()
        workspace_root = settings.paths.workspace_root
        if workspace_root is None:
            workspace_root = app_home / "workspace"
        workspace_root = workspace_root.expanduser()
        workspace_ok = writable_target(workspace_root)
        check("workspace_root", workspace_ok, None if workspace_ok else "任务工作区不可创建")
        db = database_path(self.service_dir)
        if db.exists():
            try:
                import sqlite3

                with sqlite3.connect(db) as connection:
                    quick = connection.execute("PRAGMA quick_check").fetchone()
                check("sqlite", bool(quick and quick[0] == "ok"), "SQLite 检查失败")
            except (OSError, sqlite3.DatabaseError):
                check("sqlite", False, "SQLite 不可读")
        else:
            check("sqlite", True)
        if run_probes:
            import shutil
            import subprocess

            ffmpeg = shutil.which(settings.render.ffmpeg_path)
            if ffmpeg:
                try:
                    subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=5, check=False)
                    check("ffmpeg", True)
                except (OSError, subprocess.SubprocessError):
                    check("ffmpeg", False, "FFmpeg 不可执行")
            else:
                check("ffmpeg", False, "找不到 FFmpeg")
            check("ffprobe", bool(shutil.which("ffprobe")), "找不到 ffprobe")
            if settings.asr.backend == "openai":
                check("asr_runtime", True)
            else:
                try:
                    import mlx_whisper  # type: ignore[import-not-found]  # noqa: F401
                except ImportError:
                    check("asr_runtime", False, "本地 ASR 运行时未安装")
                else:
                    check("asr_runtime", True)
        else:
            check("ffmpeg", True, "尚未执行可执行文件探测")
            check("ffprobe", True, "尚未执行可执行文件探测")
            check("asr_runtime", True, "尚未执行运行时探测")
        check("embedded_service", service.embedded_service_active() or service.get_service_status(service_dir=self.service_dir).get("running", False))
        return {"status": "ready" if all(item["status"] == "ready" for item in checks) else "blocked", "checks": checks}

    def snapshot(self) -> dict[str, Any]:
        decision, detection = self.decision()
        try:
            initial_local_model = asr_models.recommended_model()["id"]
        except ValueError as exc:
            raise OnboardingError("diagnostic_required", "本地 ASR 模型目录缺少唯一推荐项", status=409) from exc
        repo, session = self._read_session(decision)
        if repo is not None and session is not None and session.state == "in_progress" and session.project_request_id and session.project_request_hash and session.first_project_id is None:
            durable = repo.get_idempotency_key("project.create", session.project_request_id)
            if durable is not None:
                project = repo.get_project(str(durable.get("object_id") or ""))
                if (
                    durable.get("request_hash") != session.project_request_hash
                    or durable.get("object_type") != "project"
                    or project is None
                ):
                    repo.close()
                    raise OnboardingError("diagnostic_required", "无法恢复首次设置创建的项目，请记录问题编号并联系支持", status=409)
                try:
                    session = repo.bind_first_project(session.revision, session.project_request_id, project.project_id)
                except (FirstRunStateError, ValueError) as exc:
                    repo.close()
                    raise OnboardingError("diagnostic_required", "无法恢复首次设置创建的项目，请记录问题编号并联系支持", status=409) from exc
        session_payload = _safe_session_from_repository(repo, session) if repo is not None else _safe_session(session)
        if repo is not None:
            repo.close()
        try:
            settings = self.settings()
        except Exception:  # noqa: BLE001 - diagnostic startup must still return a safe DTO.
            settings = Settings()
        resources = onboarding_resources.resource_summaries(settings, self.service_dir)
        migration = None
        if decision.entry in {"migration_required", "diagnostic_required", "workbench"}:
            from .migration_coordinator import migration_summary_for_startup

            migration = migration_summary_for_startup(
                service_dir=self.service_dir,
                config_path=self.config_path,
                input_dir=self.input_dir,
                output_root=self.output_root,
                include_inspection=decision.entry in {"migration_required", "diagnostic_required"},
            )
        return {
            "ok": True,
            "entry": {
                "mode": decision.entry,
                "onboarding": decision.onboarding,
                "reason_code": decision.reason_code,
                "evidence_codes": list(detection.evidence_codes),
            },
            "session": session_payload,
            "environment": self._environment_summary(settings=settings),
            "resources": {"asr": resources["asr"], "ai": resources["ai"]},
            "model_catalog": resources["model_catalog"],
            "initial_local_model": initial_local_model,
            "provider_presets": [dict(item) for item in onboarding.PROVIDER_PRESETS],
            "suggestions": {"project_name": "我的第一个项目", "output_directory": str(self.output_root)},
            "migration": migration,
        }

    def _require_writable_session(
        self,
        *,
        expected_revision: int,
        allowed_states: set[str] | None = None,
        replay_scope: str | None = None,
        replay_request_id: str | None = None,
        replay_payload: dict[str, Any] | None = None,
    ) -> ProjectRepository:
        decision, _detection = self.decision()
        if decision.entry in {"migration_required", "diagnostic_required"}:
            raise OnboardingError(decision.entry, "当前数据需要先完成诊断或迁移", status=409)
        if decision.onboarding == "new" and not database_path(self.service_dir).exists():
            # Do not let a rejected write create the schema/data_mode as a
            # side effect of merely opening the repository.
            raise OnboardingError("onboarding_not_started", "首次设置尚未开始", status=409)
        repo = self._repo()
        session = repo.get_first_run_session()
        if session is None:
            repo.close()
            raise OnboardingError("onboarding_not_started", "首次设置尚未开始", status=409)
        if replay_scope is not None and replay_request_id is not None and replay_payload is not None:
            existing = repo.get_idempotency_key(replay_scope, replay_request_id)
            if existing is not None:
                if existing["request_hash"] != _request_hash(replay_payload):
                    repo.close()
                    raise OnboardingError("request_id_conflict", "这次操作与上次提交的内容不一致，请重新打开首次设置", status=409)
                # A durable idempotency record is authoritative for a replay;
                # the caller will project the current session without applying
                # the state transition a second time.
                return repo
        if allowed_states and session.state not in allowed_states:
            repo.close()
            raise OnboardingError("onboarding_state_conflict", "当前首次设置状态不允许此操作", status=409)
        if session.revision != expected_revision:
            repo.close()
            raise OnboardingError("onboarding_revision_conflict", "首次设置内容已更新，请重新读取", status=409)
        return repo

    def start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        decision, _ = self.decision()
        if decision.entry != "onboarding" or decision.onboarding not in {"new", "resume"}:
            raise OnboardingError("onboarding_state_conflict", "当前启动分流不允许建立新的首次设置", status=409)
        repo = self._repo()
        try:
            existing = repo.get_first_run_session()
            if existing is not None:
                if (
                    existing.state == "in_progress"
                    and existing.current_step == "welcome"
                    and existing.revision == 1
                    and not existing.draft
                    and existing.project_request_id is None
                ):
                    return 200, {"ok": True, "session": _safe_session(existing), "reused": True}
                raise OnboardingError("onboarding_state_conflict", "首次设置已经开始", status=409)
            session = repo.begin_first_run_session()
            return 201, {"ok": True, "session": _safe_session(session)}
        finally:
            repo.close()

    def patch_session(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS | {"current_step", "patch"})
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        patch = body.get("patch", {})
        current_step = body.get("current_step")
        if not isinstance(patch, dict):
            raise OnboardingError("validation_failed", "patch 必须是对象", status=422, fields={"patch": "类型错误"})
        patch = _normalize_draft_patch(patch)
        payload = {"patch": patch, "current_step": current_step}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"in_progress"},
            replay_scope="onboarding.session",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            existing = repo.get_idempotency_key("onboarding.session", request_id)
            if existing:
                session = repo.get_first_run_session()
                return 200, {"ok": True, "session": _safe_session(session), "reused": True}
            session = repo.update_first_run_draft(expected, patch, current_step=current_step)
            repo.save_idempotency_key("onboarding.session", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "session": _safe_session(session)}
        except (ValueError, KeyError) as exc:
            raise OnboardingError("validation_failed", "首次设置草稿无效", status=422, fields={"patch": str(exc)}) from exc
        finally:
            repo.close()

    def pause(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        payload = {"expected_revision": expected}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"in_progress"},
            replay_scope="onboarding.pause",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            existing = repo.get_idempotency_key("onboarding.pause", request_id)
            if existing:
                return 200, {"ok": True, "session": _safe_session(repo.get_first_run_session()), "reused": True}
            session = repo.pause_first_run(expected)
            repo.save_idempotency_key("onboarding.pause", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "session": _safe_session(session)}
        finally:
            repo.close()

    def resume(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        payload = {"expected_revision": expected}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"paused"},
            replay_scope="onboarding.resume",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            existing = repo.get_idempotency_key("onboarding.resume", request_id)
            if existing:
                return 200, {"ok": True, "session": _safe_session(repo.get_first_run_session()), "reused": True}
            session = repo.resume_first_run(expected)
            repo.save_idempotency_key("onboarding.resume", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "session": _safe_session(session)}
        finally:
            repo.close()

    def environment_check(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        repo = self._require_writable_session(expected_revision=expected, allowed_states={"in_progress"})
        try:
            try:
                settings = self.settings()
            except Exception as exc:  # noqa: BLE001 - report a stable readiness error, never parser details.
                raise OnboardingError("environment_not_ready", "当前配置无法用于环境检查", status=422) from exc
            return 200, {"ok": True, "environment": self._environment_summary(run_probes=True, settings=settings)}
        finally:
            repo.close()

    def _resource_commit_guard(self, repo: ProjectRepository, request_id: str, payload: dict[str, Any]) -> bool:
        existing = repo.get_idempotency_key("onboarding.resource", request_id)
        if existing:
            if existing["request_hash"] != _request_hash(payload):
                raise OnboardingError("request_id_conflict", "这次操作与上次提交的内容不一致，请重新打开首次设置", status=409)
            return True
        return False

    def asr_local(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS | {"model_id", "model_source"})
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        model_id = str(body.get("model_id") or "").strip()
        source = str(body.get("model_source") or asr_models.DEFAULT_MODEL_SOURCE).strip()
        if model_id not in asr_models.registry_ids() or source not in asr_models.source_ids():
            raise OnboardingError("model_not_ready", "模型或下载源不可用", status=409)
        payload = {"model_id": model_id, "model_source": source}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"in_progress"},
            replay_scope="onboarding.resource",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            if self._resource_commit_guard(repo, request_id, payload):
                return 200, {"ok": True, "reused": True, "session": _safe_session(repo.get_first_run_session())}
            try:
                try:
                    asr_models.recommended_model()
                except ValueError as exc:
                    raise OnboardingError("diagnostic_required", "本地 ASR 模型目录缺少唯一推荐项", status=409) from exc
                if os.getenv("ASR_BACKEND") is not None or os.getenv("ASR_MODEL") is not None:
                    raise OnboardingError("asr_overridden_by_environment", "ASR 配置正被环境变量覆盖，请先移除 ASR_MODEL / ASR_BACKEND", status=409)
                if asr_models.local_path_for(model_id) is None:
                    raise OnboardingError("model_not_ready", "所选本地模型尚未完整安装", status=409)
                entry = asr_models.model_entry(model_id)
                saved = config_editor.save_asr_model_selection(entry["backend"], model_id, model_source=source, config_path=self.config_path, backup_root=self.config_path.parent / "work" / "config_backups")
                if not saved.get("ok"):
                    raise OnboardingError("resource_commit_failed", "本地 ASR 配置保存失败", status=500)
                loaded = onboarding_resources.load_settings_explicit(self.config_path, self.env_path)
                if loaded.asr.backend != entry["backend"] or loaded.asr.model != model_id or loaded.asr.model_source != source:
                    raise OnboardingError("resource_commit_failed", "本地 ASR 配置回读不一致", status=500)
            except OnboardingError:
                raise
            except Exception as exc:
                raise OnboardingError("resource_commit_failed", "本地 ASR 配置保存失败", status=500) from exc
            session = repo.update_first_run_draft(expected, {"asr": {"mode": "local", "local_model_id": model_id, "model_source": source, "model": model_id}}, current_step="asr")
            repo.save_idempotency_key("onboarding.resource", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "resource": {"mode": "local", "model_id": model_id, "ready": True}, "session": _safe_session(session)}
        finally:
            repo.close()

    def asr_cloud(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS | {"api_base", "model", "api_key"})
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        api_base = str(body.get("api_base") or "").strip()
        model = str(body.get("model") or "").strip()
        api_key = str(body.get("api_key") or "").strip()
        payload = {"api_base": api_base, "model": model}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"in_progress"},
            replay_scope="onboarding.resource",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            if self._resource_commit_guard(repo, request_id, payload):
                return 200, {"ok": True, "reused": True, "session": _safe_session(repo.get_first_run_session())}
            result = onboarding_resources.test_asr_service(api_base, model, api_key, allow_loopback=urlsplit_host_is_loopback(api_base))
            if not result.get("ok"):
                return 422, result
            try:
                committed = onboarding_resources.commit_asr_cloud_configuration(config_path=self.config_path, env_path=self.env_path, api_base=api_base, model=model, api_key=api_key, allow_loopback=urlsplit_host_is_loopback(api_base))
                normalized_endpoint = onboarding_resources.normalize_api_base(api_base, allow_loopback=urlsplit_host_is_loopback(api_base))
                loaded = onboarding_resources.load_settings_explicit(self.config_path, self.env_path)
                if loaded.asr.backend != "openai" or loaded.asr.model != model or loaded.asr.api_base != normalized_endpoint or not loaded.asr.api_key:
                    raise OnboardingError("resource_commit_failed", "云端 ASR 配置回读不一致", status=500)
            except onboarding_resources.ResourceError as exc:
                raise OnboardingError(exc.code, exc.message, status=500, fields=exc.fields) from exc
            except Exception as exc:  # noqa: BLE001 - config/readback failures use one stable public contract.
                raise OnboardingError("resource_commit_failed", "云端 ASR 配置保存失败", status=500) from exc
            normalized_endpoint = onboarding_resources.normalize_api_base(
                api_base, allow_loopback=urlsplit_host_is_loopback(api_base)
            )
            session = repo.update_first_run_draft(
                expected,
                {"asr": {"mode": "cloud", "api_base": normalized_endpoint, "model": model}},
                current_step="asr",
            )
            repo.save_idempotency_key("onboarding.resource", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "resource": committed, "session": _safe_session(session)}
        finally:
            repo.close()

    def ai(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS | {"provider_id", "provider_label", "api_base", "model", "api_key"})
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        provider_id = str(body.get("provider_id") or "custom").strip()
        provider_label = str(body.get("provider_label") or provider_id).strip()
        api_base = str(body.get("api_base") or "").strip()
        model = str(body.get("model") or "").strip()
        api_key = str(body.get("api_key") or "").strip()
        payload = {"provider_id": provider_id, "provider_label": provider_label, "api_base": api_base, "model": model}
        request_hash = _request_hash(payload)
        repo = self._require_writable_session(
            expected_revision=expected,
            allowed_states={"in_progress"},
            replay_scope="onboarding.resource",
            replay_request_id=request_id,
            replay_payload=payload,
        )
        try:
            if self._resource_commit_guard(repo, request_id, payload):
                return 200, {"ok": True, "reused": True, "session": _safe_session(repo.get_first_run_session())}
            result = onboarding_resources.test_ai_service(api_base, model, api_key, allow_loopback=urlsplit_host_is_loopback(api_base))
            if not result.get("ok"):
                return 422, result
            try:
                committed = onboarding_resources.commit_llm_configuration(config_path=self.config_path, env_path=self.env_path, provider_label=provider_label, api_base=api_base, model=model, api_key=api_key, allow_loopback=urlsplit_host_is_loopback(api_base))
                normalized_endpoint = onboarding_resources.normalize_api_base(api_base, allow_loopback=urlsplit_host_is_loopback(api_base))
                loaded = onboarding_resources.load_settings_explicit(self.config_path, self.env_path)
                if loaded.llm.model != model or loaded.llm.api_base != normalized_endpoint or not loaded.llm.api_key:
                    raise OnboardingError("resource_commit_failed", "AI 配置回读不一致", status=500)
            except onboarding_resources.ResourceError as exc:
                raise OnboardingError(exc.code, exc.message, status=500, fields=exc.fields) from exc
            except Exception as exc:  # noqa: BLE001 - config/readback failures use one stable public contract.
                raise OnboardingError("resource_commit_failed", "AI 配置保存失败", status=500) from exc
            normalized_endpoint = onboarding_resources.normalize_api_base(
                api_base, allow_loopback=urlsplit_host_is_loopback(api_base)
            )
            session = repo.update_first_run_draft(
                expected,
                {"ai": {"provider_id": provider_id, "api_base": normalized_endpoint, "model": model}},
                current_step="ai",
            )
            repo.save_idempotency_key("onboarding.resource", request_id, request_hash=request_hash, object_type="session", object_id="primary")
            return 200, {"ok": True, "resource": committed, "session": _safe_session(session)}
        finally:
            repo.close()

    def _project_from_draft(self, draft: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        project = draft.get("project")
        if not isinstance(project, dict):
            raise OnboardingError("project_validation_failed", "请先填写首项目配置", status=422, fields={"project": "必填"})
        allowed = {"name", "source_directory", "trigger_mode", "schedule_mode", "daily_time", "interval_minutes", "output_directory"}
        unknown = set(project) - allowed
        if unknown:
            raise OnboardingError("project_validation_failed", "首项目包含未知字段", status=422, fields={key: "未知字段" for key in unknown})
        name = str(project.get("name") or "").strip()
        source = str(project.get("source_directory") or "").strip()
        output = str(project.get("output_directory") or "").strip()
        trigger = str(project.get("trigger_mode") or "manual").strip()
        schedule_mode = project.get("schedule_mode")
        if trigger not in {"manual", "scheduled"}:
            raise OnboardingError("project_validation_failed", "触发方式无效", status=422, fields={"trigger_mode": "无效"})
        if not name or not source or not output:
            raise OnboardingError("project_validation_failed", "项目名称、录像目录和输出目录均为必填", status=422)
        if not Path(source).expanduser().is_absolute():
            raise OnboardingError("project_validation_failed", "录像目录必须是绝对路径", status=422, fields={"source_directory": "必须是绝对路径"})
        if not Path(output).expanduser().is_absolute():
            raise OnboardingError("project_validation_failed", "输出目录必须是绝对路径", status=422, fields={"output_directory": "必须是绝对路径"})
        if schedule_mode is not None and schedule_mode not in {"daily", "interval"}:
            raise OnboardingError("project_validation_failed", "定时方式无效", status=422, fields={"schedule_mode": "无效"})
        if trigger == "scheduled" and schedule_mode not in {"daily", "interval"}:
            raise OnboardingError("project_validation_failed", "定时项目必须选择 daily 或 interval", status=422)
        if trigger == "manual":
            schedule_mode = "daily"
            daily_time = "09:00"
            interval_minutes = None
        elif schedule_mode == "daily":
            daily_time = project.get("daily_time")
            if not isinstance(daily_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
                raise OnboardingError("project_validation_failed", "daily 定时项目必须填写有效时间", status=422, fields={"daily_time": "格式必须为 HH:MM"})
            interval_minutes = None
        else:
            interval_minutes = project.get("interval_minutes")
            if interval_minutes not in {30, 60, 180, 360, 720}:
                raise OnboardingError("project_validation_failed", "interval 定时项目必须选择受支持的间隔", status=422, fields={"interval_minutes": "无效"})
            daily_time = None
        timezone = self.settings().scheduler.timezone
        base = default_project_config(source, output)
        base["source"]["first_scan_mode"] = "new_only"
        base["source"]["lookback_days"] = None
        base["schedule"].update(
            {
                "enabled": trigger == "scheduled",
                "mode": schedule_mode,
                "daily_time": daily_time if schedule_mode == "daily" else None,
                "interval_minutes": interval_minutes if schedule_mode == "interval" else None,
                "timezone": timezone,
            }
        )
        base["resources"].update({"asr_ref": "legacy.asr.default", "analysis_ref": "legacy.analysis.default"})
        config = project_config_v2(base)
        return name, config, {"source_directory": source, "output_directory": output, "trigger_mode": trigger, "schedule_mode": schedule_mode}

    def validate_project(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        del request_id
        repo = self._require_writable_session(expected_revision=expected, allowed_states={"in_progress"})
        try:
            settings = self.settings()
            name, config, raw = self._project_from_draft(repo.get_first_run_session().draft)
            manager = ProjectManager(repo, settings)
            result = manager.validate_project(name=name, config=config, activation_state="active", allow_creatable_output=True)
            output_status = output_directory_status(Path(raw["output_directory"]))
            checks = {
                "asr": {"ready": bool(onboarding_resources.resource_summaries(settings, self.service_dir)["asr"]["ready"])},
                "ai": {"ready": bool(onboarding_resources.resource_summaries(settings, self.service_dir)["ai"]["ready"])},
                "source_directory": {"status": "ready" if Path(raw["source_directory"]).is_dir() else "blocked"},
                "output_directory": {"status": output_status},
            }
            summary = {"recording_source": raw["source_directory"], "discovery": "new_only", "processing": "ai_auto", "output": raw["output_directory"]}
            return 200, {"ok": True, "valid": result.ok, "fatal": [asdict(item) for item in result.fatal], "blockers": [asdict(item) for item in result.blockers], "warnings": [asdict(item) for item in result.warnings], "checks": checks, "summary": summary, "existing_video_count": _count_videos(Path(raw["source_directory"])), "normalized_config": result.normalized_config}
        except OnboardingError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise OnboardingError("project_validation_failed", "首项目配置无效", status=422, fields={"project": str(exc)}) from exc
        finally:
            repo.close()

    def finish(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with _FINISH_LOCKS_GUARD:
            lock = _FINISH_LOCKS.setdefault(str(self.service_dir), threading.RLock())
        with lock:
            return self._finish(body)

    def _finish(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        decision, _ = self.decision()
        if decision.entry in {"migration_required", "diagnostic_required"}:
            raise OnboardingError(decision.entry, "当前数据需要先完成诊断或迁移", status=409)
        effective_expected = expected
        if database_path(self.service_dir).exists():
            probe = self._repo()
            current = probe.get_first_run_session()
            if current is not None and current.state == "completed":
                project = probe.get_project(current.first_project_id) if current.first_project_id else None
                session_payload = _safe_session_from_repository(probe, current)
                probe.close()
                if current.project_request_id == request_id:
                    return 200, {"ok": True, "reused": True, "project": asdict(project) if project else None, "session": session_payload}
                raise OnboardingError("onboarding_state_conflict", "首次设置已经完成", status=409)
            if current is not None and current.project_request_id == request_id and current.project_request_hash:
                # A process may have exited after reserving the request or
                # after persisting the project.  Continue from the durable
                # revision rather than creating a second request identity.
                effective_expected = current.revision
            elif current is not None and current.state == "activation_pending":
                probe.close()
                raise OnboardingError("request_id_conflict", "无法继续上次操作，请重新打开首次设置后再试", status=409)
            probe.close()
        repo = self._require_writable_session(expected_revision=effective_expected, allowed_states={"in_progress", "activation_pending"})
        created_output: Path | None = None
        try:
            session = repo.get_first_run_session()
            assert session is not None
            if session.state == "activation_pending":
                if session.project_request_id != request_id:
                    raise OnboardingError("request_id_conflict", "无法继续上次操作，请重新打开首次设置后再试", status=409)
                return self._retry({"request_id": request_id, "expected_revision": session.revision}, repository=repo)
            settings = self.settings()
            self._revalidate_resources(settings, draft=session.draft)
            name, config, raw = self._project_from_draft(session.draft)
            manager = ProjectManager(repo, settings)
            validation = manager.validate_project(name=name, config=config, activation_state="active", allow_creatable_output=True)
            if validation.fatal or validation.blockers:
                raise OnboardingError("project_validation_failed", "首项目配置未通过校验", status=422, fields={item.field: item.message for item in (*validation.fatal, *validation.blockers)})
            raw_output = Path(raw["output_directory"]).expanduser()
            status = output_directory_status(raw_output)
            if status == "blocked":
                raise OnboardingError("project_validation_failed", "输出目录不可用", status=422, fields={"output_directory": "blocked"})
            if status == "creatable":
                output = raw_output.resolve(strict=False)
                output.mkdir(parents=True, exist_ok=False)
                created_output = output
            request_payload = {"name": name, "description": "", "config": config, "activation_state": "active"}
            request_hash = _request_hash(request_payload)
            reserved = repo.reserve_first_project_request(effective_expected, request_id, request_hash)
            try:
                project = manager.create_project(name=name, config=config, activation_state="active", request_id=request_id)
            except ProjectError:
                raise
            except Exception as exc:
                raise OnboardingError("project_creation_uncertain", "项目创建结果暂不可确认，请重试同一请求", status=500) from exc
            bound = repo.bind_first_project(reserved.revision, request_id, project.project_id)
            readiness = service.ensure_service_ready(lambda: self.settings(), service_dir=self.service_dir, project_id=project.project_id)
            if not readiness.get("ok"):
                failed = repo.record_activation_failure(bound.revision, str(readiness.get("error_code") or "service_not_ready"), str(readiness.get("message") or "服务尚未就绪"))
                return 202, {"ok": True, "project": asdict(project), "session": _safe_session_from_repository(repo, failed), "reused": False}
            try:
                completed = repo.complete_first_run(bound.revision)
            except FirstRunStateError:
                latest = repo.get_first_run_session()
                if latest is None or latest.state != "completed" or latest.project_request_id != request_id:
                    raise
                return 200, {"ok": True, "reused": True, "project": asdict(project), "session": _safe_session_from_repository(repo, latest)}
            return (200 if session.project_request_id == request_id else 201), {"ok": True, "project": asdict(project), "session": _safe_session_from_repository(repo, completed), "reused": session.project_request_id == request_id}
        except OnboardingError:
            if created_output is not None and created_output.is_dir() and not any(created_output.iterdir()):
                created_output.rmdir()
            raise
        except RevisionConflictError as exc:
            if created_output is not None and created_output.is_dir() and not any(created_output.iterdir()):
                created_output.rmdir()
            raise OnboardingError("onboarding_revision_conflict", "首次设置内容已更新，请重新读取", status=409) from exc
        except RequestConflictError as exc:
            if created_output is not None and created_output.is_dir() and not any(created_output.iterdir()):
                created_output.rmdir()
            raise OnboardingError("request_id_conflict", "这次操作与上次提交的内容不一致，请重新打开首次设置", status=409) from exc
        except FirstRunStateError as exc:
            if created_output is not None and created_output.is_dir() and not any(created_output.iterdir()):
                created_output.rmdir()
            raise OnboardingError("onboarding_state_conflict", "当前首次设置状态不允许此操作", status=409) from exc
        except ProjectError as exc:
            if created_output is not None and created_output.is_dir() and not any(created_output.iterdir()):
                created_output.rmdir()
            raise OnboardingError("project_creation_uncertain" if exc.status >= 500 else "project_validation_failed", exc.message, status=exc.status, fields=exc.fields) from exc
        finally:
            repo.close()

    def retry(self, body: dict[str, Any], *, repository: ProjectRepository | None = None) -> tuple[int, dict[str, Any]]:
        _strict_body(body, _COMMON_REQUEST_FIELDS)
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        with _FINISH_LOCKS_GUARD:
            lock = _FINISH_LOCKS.setdefault(str(self.service_dir), threading.RLock())
        with lock:
            return self._retry({"request_id": request_id, "expected_revision": expected}, repository=repository)

    def _retry(self, body: dict[str, Any], *, repository: ProjectRepository | None = None) -> tuple[int, dict[str, Any]]:
        request_id = _request_id(body.get("request_id"))
        expected = _revision(body.get("expected_revision"))
        if repository is None:
            decision, _detection = self.decision()
            if decision.entry in {"migration_required", "diagnostic_required"}:
                raise OnboardingError(decision.entry, "当前数据需要先完成诊断或迁移", status=409)
            if decision.onboarding == "new" and not database_path(self.service_dir).exists():
                raise OnboardingError("onboarding_not_started", "首次设置尚未开始", status=409)
            repo = self._repo()
            should_close = True
        else:
            repo = repository
            should_close = False
        try:
            session = repo.get_first_run_session()
            if session is None:
                raise OnboardingError("onboarding_not_started", "首次设置尚未开始", status=409)
            if session.state == "completed" and session.project_request_id == request_id:
                return 200, {"ok": True, "reused": True, "session": _safe_session_from_repository(repo, session)}
            if session.state != "activation_pending" or session.first_project_id is None:
                raise OnboardingError("onboarding_state_conflict", "当前没有可重试的首项目服务", status=409)
            if session.project_request_id != request_id:
                raise OnboardingError("request_id_conflict", "无法继续上次操作，请重新打开首次设置后再试", status=409)
            # Reusing the reserved project request is safe across a lost
            # response: the durable session revision is the current CAS point.
            effective_expected = session.revision if expected <= session.revision else expected
            if effective_expected != session.revision:
                raise OnboardingError("onboarding_revision_conflict", "首次设置内容已更新，请重新读取", status=409)
            readiness = service.ensure_service_ready(
                lambda: self.settings(), service_dir=self.service_dir, project_id=session.first_project_id
            )
            if not readiness.get("ok"):
                failed = repo.record_activation_failure(
                    session.revision,
                    str(readiness.get("error_code") or "service_not_ready"),
                    str(readiness.get("message") or "服务尚未就绪"),
                )
                return 202, {"ok": True, "session": _safe_session_from_repository(repo, failed)}
            try:
                completed = repo.complete_first_run(session.revision)
            except FirstRunStateError:
                latest = repo.get_first_run_session()
                if latest is None or latest.state != "completed" or latest.project_request_id != request_id:
                    raise
                return 200, {"ok": True, "reused": True, "session": _safe_session_from_repository(repo, latest)}
            return 200, {"ok": True, "reused": True, "session": _safe_session_from_repository(repo, completed)}
        finally:
            if should_close:
                repo.close()

    def _revalidate_resources(self, settings: Settings, *, draft: dict[str, Any] | None = None) -> None:
        asr = settings.asr
        if asr is None or not asr.model:
            raise OnboardingError("model_not_ready", "语音识别资源尚未就绪", status=422)
        if os.getenv("ASR_BACKEND") is not None or os.getenv("ASR_MODEL") is not None:
            raise OnboardingError("asr_overridden_by_environment", "ASR 配置正被环境变量覆盖，请先移除 ASR_MODEL / ASR_BACKEND", status=422)
        asr_draft = draft.get("asr") if isinstance(draft, dict) else None
        if isinstance(asr_draft, dict):
            mode = asr_draft.get("mode")
            if mode == "local":
                try:
                    entry = asr_models.model_entry(asr.model)
                except ValueError as exc:
                    raise OnboardingError("model_not_ready", "已保存的本地 ASR 配置无法识别", status=422) from exc
                if (
                    asr.model != asr_draft.get("local_model_id")
                    or asr.backend != entry["backend"]
                    or asr.model_source != asr_draft.get("model_source")
                ):
                    raise OnboardingError("model_not_ready", "已保存的本地 ASR 配置与当前选择不一致", status=422)
            if mode == "cloud" and (
                asr.backend != "openai"
                or asr.model != asr_draft.get("model")
                or asr.api_base != asr_draft.get("api_base")
            ):
                raise OnboardingError("resource_commit_failed", "已保存的云端 ASR 配置与当前选择不一致", status=422)
        if asr.backend == "openai":
            result = onboarding_resources.test_asr_service(asr.api_base or "", asr.model, asr.api_key or "", allow_loopback=urlsplit_host_is_loopback(asr.api_base or ""))
        elif asr_models.local_path_for(asr.model) is None:
            raise OnboardingError("model_not_ready", "本地 ASR 模型尚未完整安装", status=422)
        else:
            result = {"ok": True}
        if not result.get("ok"):
            raise OnboardingError(str(result.get("error_code") or "asr_request_failed"), str(result.get("message") or "语音识别资源测试失败"), status=422)
        llm = settings.llm
        ai_draft = draft.get("ai") if isinstance(draft, dict) else None
        if isinstance(ai_draft, dict) and (llm.model != ai_draft.get("model") or llm.api_base != ai_draft.get("api_base")):
            raise OnboardingError("resource_commit_failed", "已保存的 AI 配置与当前选择不一致", status=422)
        ai = onboarding_resources.test_ai_service(llm.api_base, llm.model, llm.api_key or "", allow_loopback=urlsplit_host_is_loopback(llm.api_base))
        if not ai.get("ok"):
            raise OnboardingError(str(ai.get("error_code") or "ai_request_failed"), str(ai.get("message") or "AI 资源测试失败"), status=422)

    def dispatch(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]] | None:
        parts = [item for item in path.split("?")[0].split("/") if item]
        if parts[:2] != ["api", "onboarding"]:
            return None
        body = body or {}
        if method == "GET" and parts == ["api", "onboarding"]:
            return 200, self.snapshot()
        handlers: dict[tuple[str, tuple[str, ...]], Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]] = {
            ("POST", ("api", "onboarding", "start")): self.start,
            ("PATCH", ("api", "onboarding", "session")): self.patch_session,
            ("POST", ("api", "onboarding", "pause")): self.pause,
            ("POST", ("api", "onboarding", "resume")): self.resume,
            ("POST", ("api", "onboarding", "environment-check")): self.environment_check,
            ("POST", ("api", "onboarding", "resources", "asr", "local")): self.asr_local,
            ("POST", ("api", "onboarding", "resources", "asr", "cloud")): self.asr_cloud,
            ("POST", ("api", "onboarding", "resources", "ai")): self.ai,
            ("POST", ("api", "onboarding", "project", "validate")): self.validate_project,
            ("POST", ("api", "onboarding", "finish")): self.finish,
            ("POST", ("api", "onboarding", "service", "retry")): self.retry,
        }
        handler = handlers.get((method, tuple(parts)))
        if handler is None:
            return None
        return handler(body)


def _count_videos(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for item in path.iterdir() if item.is_file() and item.suffix.lower() in {".m4v", ".mkv", ".mov", ".mp4", ".webm"})


def urlsplit_host_is_loopback(value: str) -> bool:
    from urllib.parse import urlsplit

    return (urlsplit(value).hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
