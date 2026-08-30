from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .project_domain import (
    Project,
    normalize_utc,
    project_config_v2,
    stable_json,
    validate_project_config,
)
from .project_resources import compatibility_resources, resource_map
from .project_storage import ProjectRepository, database_path


class ProjectError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        fields: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        super().__init__(message)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class ProjectValidation:
    fatal: tuple[ValidationIssue, ...]
    blockers: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]
    normalized_config: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return not self.fatal and not self.blockers


def open_project_repository(
    service_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> ProjectRepository:
    from .first_run_detection import inspect_startup

    service = Path(service_dir).expanduser().resolve()
    existed = database_path(service).exists()
    decision = inspect_startup(
        config_path=config_path or service / ".first-run-config-not-provided",
        env_path=env_path or service / ".first-run-env-not-provided",
        service_dir=service,
    )
    if decision.entry == "migration_required":
        raise ProjectError("migration_required", "检测到旧版数据，需要先完成迁移确认", status=409)
    if decision.entry == "diagnostic_required":
        raise ProjectError("diagnostic_required", "启动数据存在冲突，需要先完成诊断", status=409)
    repository = ProjectRepository(service)
    if not existed:
        repository.set_data_mode("projects")
    return repository


def _request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def output_directory_is_writable(output: Path) -> bool:
    if not output.is_dir() or not os.access(output, os.W_OK):
        return False
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".venus-write-probe-", dir=output, delete=False) as probe:
            probe_path = Path(probe.name)
    except OSError:
        return False
    try:
        probe_path.unlink()
    except OSError:
        return False
    return True


def _path_contains_symlink(path: Path) -> bool:
    """Return whether any existing component of ``path`` is a symlink.

    Resolving a path before checking it would make a symlinked parent look like
    an ordinary directory.  Output paths are user-controlled, so the check is
    deliberately component-based and happens before canonicalisation.
    """
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    current = Path(candidate.anchor)
    trusted_aliases = {Path("/var"), Path("/tmp")}
    for component in candidate.parts[1:]:
        current /= component
        if current.is_symlink() and current not in trusted_aliases:
            return True
    return False


def output_directory_status(output: Path) -> str:
    """Classify an output directory without creating it."""
    raw_output = output.expanduser()
    if _path_contains_symlink(raw_output):
        return "blocked"
    output = raw_output.resolve(strict=False)
    if output.is_dir() and output_directory_is_writable(output):
        return "ready"
    if output.exists():
        return "blocked"
    parent = output.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return "blocked"
    return "creatable"


class ProjectManager:
    def __init__(self, repository: ProjectRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def _require_projects_mode(self) -> None:
        if self.repository.get_data_mode() != "projects":
            raise ProjectError("migration_required", "旧版数据尚未完成迁移确认", status=409)

    def form_options(self) -> dict[str, Any]:
        return {
            "data_mode": self.repository.get_data_mode(),
            "resources": [resource.__dict__ for resource in compatibility_resources(self.settings)],
            "first_scan_modes": ["new_only", "recent", "choose_existing"],
            "lookback_days": [3, 7, 30],
            "schedule_modes": ["daily", "interval"],
            "interval_minutes": [30, 60, 180, 360, 720],
            "intermediate_retention": ["remind_immediately", "remind_after_7_days", "keep"],
            "timezone": self.settings.scheduler.timezone,
            "defaults": {
                "first_scan_mode": "new_only",
                "lookback_days": None,
                "schedule_enabled": False,
                "schedule_mode": "daily",
                "daily_time": "22:00",
                "intermediate_retention": "remind_after_7_days",
            },
        }

    def validate_project(
        self,
        *,
        name: str,
        config: dict[str, Any],
        activation_state: str,
        exclude_project_id: str | None = None,
        allow_creatable_output: bool = False,
    ) -> ProjectValidation:
        fatal: list[ValidationIssue] = []
        blockers: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        normalized: dict[str, Any] | None = None
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 80 or any(ord(character) < 32 for character in clean_name):
            fatal.append(ValidationIssue("name", "invalid_name", "项目名称不能为空、包含控制字符或超过 80 个字符"))
        if activation_state not in {"inactive", "active"}:
            fatal.append(ValidationIssue("activation_state", "invalid_activation_state", "创建时只能选择未启用或启用"))
        try:
            normalized = validate_project_config(config)
            if normalized["schema_version"] == 1:
                normalized = project_config_v2(normalized)
        except (KeyError, TypeError, ValueError) as exc:
            fatal.append(ValidationIssue("config", "invalid_config", str(exc)))
        if normalized is None:
            return ProjectValidation(tuple(fatal), tuple(blockers), tuple(warnings), None)

        if normalized["processing"] != {
            "review_strategy": "ai_auto",
            "review_policy_version": "auto_review_v1",
            "material_policy_version": "publish_material_v1",
            "output_profile": "current_renderer",
            "naming_policy": "system_safe",
        }:
            fatal.append(ValidationIssue("processing", "unsupported_processing_policy", "当前版本只支持固定的 AI 审阅与渲染策略"))
        if normalized["output"]["original_media_policy"] != "never_delete":
            fatal.append(ValidationIssue("output.original_media_policy", "unsafe_media_policy", "原始录像必须永不自动删除"))
        if normalized["output"]["final_media_policy"] != "keep":
            fatal.append(ValidationIssue("output.final_media_policy", "unsafe_media_policy", "成片必须始终保留"))

        source_raw = Path(str(normalized["source"]["directory"])).expanduser()
        output_raw = Path(str(normalized["output"]["directory"])).expanduser()
        if not source_raw.is_absolute():
            fatal.append(ValidationIssue("source.directory", "path_not_absolute", "录像目录必须是绝对路径"))
        if not output_raw.is_absolute():
            fatal.append(ValidationIssue("output.directory", "path_not_absolute", "输出目录必须是绝对路径"))
        source = source_raw.resolve(strict=False)
        output = output_raw.resolve(strict=False)
        normalized["source"]["directory"] = str(source)
        normalized["output"]["directory"] = str(output)
        if not source.is_dir() or not os.access(source, os.R_OK):
            blockers.append(ValidationIssue("source.directory", "source_unavailable", "录像目录不存在或不可读"))
        # Check the user-supplied path before resolving it so a symlinked
        # component cannot be normalised into an apparently safe destination.
        output_status = output_directory_status(output_raw)
        if output_status == "blocked" or (output_status == "creatable" and not allow_creatable_output):
            blockers.append(ValidationIssue("output.directory", "output_unwritable", "输出目录不存在或不可写"))
        if source != output and _inside(output, source):
            blockers.append(ValidationIssue("output.directory", "output_inside_source", "输出目录不能位于录像目录内"))
        elif source == output:
            blockers.append(ValidationIssue("output.directory", "output_inside_source", "输出目录不能与录像目录相同"))

        resources = resource_map(self.settings)
        refs = normalized["resources"]
        for field in ("asr_ref", "analysis_ref", "review_ref"):
            resource_id = str(refs[field])
            resource = resources.get(resource_id)
            if resource is None or not resource.ready:
                blockers.append(ValidationIssue(f"resources.{field}", "resource_unavailable", f"资源不可用：{resource_id}"))
        for project in self.repository.list_projects():
            if project.project_id == exclude_project_id:
                continue
            if project.name == clean_name:
                warnings.append(ValidationIssue("name", "duplicate_name", "已有同名项目"))
            revision = self.repository.get_config_revision(project.project_id)
            if revision and revision.config["source"]["directory"] == str(source):
                warnings.append(ValidationIssue("source.directory", "source_shared", "其他项目正在使用同一录像目录"))
        if normalized["output"]["intermediate_retention"] == "remind_immediately":
            warnings.append(ValidationIssue("output.intermediate_retention", "cleanup_immediate", "完成后会立即提醒清理"))
        if normalized["source"]["first_scan_mode"] == "recent" and normalized["source"]["lookback_days"] == 30:
            warnings.append(ValidationIssue("source.lookback_days", "large_backfill", "回溯 30 天可能创建较多剪辑记录"))
        return ProjectValidation(tuple(fatal), tuple(blockers), tuple(warnings), normalized)

    def ensure_v2_config(self, project_id: str) -> int:
        """Upgrade only the current project config; historical revisions and Run snapshots stay immutable."""
        current = self.repository.get_config_revision(project_id)
        if current is None:
            raise ProjectError("project_not_found", "项目不存在", status=404)
        if current.schema_version == 2:
            return current.revision
        revision = self.repository.add_config_revision(
            project_id,
            project_config_v2(current.config),
            expected_revision=current.revision,
        )
        self.repository.append_workspace_event(
            "project_config_upgraded_v2",
            project_id=project_id,
            payload={"from_revision": current.revision, "to_revision": revision.revision},
        )
        return revision.revision

    def _idempotent_project(self, scope: str, request_id: str | None, payload: dict[str, Any]) -> Project | None:
        if not request_id:
            return None
        existing = self.repository.get_idempotency_key(scope, request_id)
        if existing is None:
            return None
        if existing["request_hash"] != _request_hash(payload):
            raise ProjectError("request_id_conflict", "同一 request_id 的请求内容不一致", status=409)
        project = self.repository.get_project(str(existing["object_id"]))
        if project is None:
            raise ProjectError("data_integrity_error", "幂等记录指向不存在的项目", status=500)
        return project

    def _save_idempotency(self, scope: str, request_id: str | None, payload: dict[str, Any], project_id: str) -> None:
        if request_id:
            self.repository.save_idempotency_key(
                scope,
                request_id,
                request_hash=_request_hash(payload),
                object_type="project",
                object_id=project_id,
            )

    def create_project(
        self,
        *,
        name: str,
        config: dict[str, Any],
        activation_state: str,
        description: str = "",
        request_id: str | None = None,
    ) -> Project:
        self._require_projects_mode()
        payload = {"name": name, "description": description, "config": config, "activation_state": activation_state}
        existing = self._idempotent_project("project.create", request_id, payload)
        if existing is not None:
            return existing
        validation = self.validate_project(name=name, config=config, activation_state=activation_state)
        if validation.fatal or (activation_state == "active" and validation.blockers):
            fields = {issue.field: issue.message for issue in (*validation.fatal, *validation.blockers)}
            raise ProjectError("validation_failed", "项目配置未通过校验", status=422, fields=fields)
        assert validation.normalized_config is not None
        runtime = self._runtime_values(activation_state, validation.normalized_config, validation=validation)
        project = self.repository.create_project_bundle(
            name.strip(),
            validation.normalized_config,
            description=description.strip(),
            activation_state=activation_state,
            runtime=runtime,
            event_payload={"activation_state": activation_state},
            idempotency=(
                {
                    "scope": "project.create",
                    "request_id": request_id,
                    "request_hash": _request_hash(payload),
                    "object_type": "project",
                }
                if request_id
                else None
            ),
        )
        result = self.repository.get_project(project.project_id)
        assert result is not None
        return result

    def _runtime_values(
        self,
        activation_state: str,
        config: dict[str, Any],
        *,
        validation: ProjectValidation,
    ) -> dict[str, Any]:
        mode = config["source"]["first_scan_mode"]
        first_scan_state = "not_required" if mode == "new_only" else "pending"
        next_scan_at = None
        auto_scan_state = "off"
        if activation_state == "active" and config["schedule"]["enabled"]:
            from .project_scheduler import next_project_scan_at

            next_scan_at = next_project_scan_at(config)
            auto_scan_state = "scheduled"
        return {
            "readiness_state": "blocked" if validation.blockers else "ready",
            "failure_code": validation.blockers[0].code if validation.blockers else None,
            "failure_summary": validation.blockers[0].message if validation.blockers else None,
            "discovery_baseline": normalize_utc(),
            "first_scan_state": first_scan_state,
            "auto_scan_state": auto_scan_state,
            "next_scan_at": next_scan_at,
        }

    def _initialize_runtime(
        self,
        project: Project,
        config: dict[str, Any],
        *,
        validation: ProjectValidation,
    ) -> None:
        self.repository.update_runtime(project.project_id, **self._runtime_values(project.activation_state, config, validation=validation))

    def update_project(
        self,
        project_id: str,
        *,
        name: str,
        description: str,
        config: dict[str, Any],
        expected_revision: int,
        request_id: str | None = None,
    ) -> Project:
        self._require_projects_mode()
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProjectError("project_not_found", "项目不存在", status=404)
        payload = {
            "project_id": project_id,
            "name": name,
            "description": description,
            "config": config,
            "expected_revision": expected_revision,
        }
        existing = self._idempotent_project(f"project.update:{project_id}", request_id, payload)
        if existing is not None:
            return existing
        validation = self.validate_project(
            name=name,
            config=config,
            activation_state="active" if project.activation_state == "active" else "inactive",
            exclude_project_id=project_id,
        )
        if validation.fatal or (project.activation_state == "active" and validation.blockers):
            raise ProjectError(
                "validation_failed",
                "项目配置未通过校验",
                status=422,
                fields={issue.field: issue.message for issue in (*validation.fatal, *validation.blockers)},
            )
        assert validation.normalized_config is not None
        try:
            self.repository.add_config_revision(
                project_id,
                validation.normalized_config,
                expected_revision=expected_revision,
            )
        except ValueError as exc:
            if str(exc) == "revision_conflict":
                current = self.repository.get_project(project_id)
                raise ProjectError(
                    "revision_conflict",
                    "项目配置版本已变化",
                    status=409,
                    fields={"current_revision": str(current.current_config_revision if current else "")},
                ) from exc
            raise
        result = self.repository.update_project_identity(project_id, name=name.strip(), description=description.strip())
        runtime_changes: dict[str, Any] = {
            "readiness_state": "blocked" if validation.blockers else "ready",
            "failure_code": validation.blockers[0].code if validation.blockers else None,
            "failure_summary": validation.blockers[0].message if validation.blockers else None,
        }
        if result.activation_state == "active":
            schedule = validation.normalized_config["schedule"]
            if schedule["enabled"]:
                from .project_scheduler import next_project_scan_at

                runtime_changes.update(
                    auto_scan_state="scheduled",
                    next_scan_at=next_project_scan_at(validation.normalized_config),
                )
            else:
                runtime_changes.update(auto_scan_state="off", next_scan_at=None)
        self.repository.update_runtime(project_id, **runtime_changes)
        self.repository.append_workspace_event("project_updated", project_id=project_id, payload={"revision": expected_revision + 1})
        self._save_idempotency(f"project.update:{project_id}", request_id, payload, project_id)
        return result

    def enable_project(self, project_id: str, *, request_id: str | None = None) -> Project:
        return self._activate(project_id, "active", request_id=request_id)

    def pause_project(self, project_id: str, *, request_id: str | None = None) -> Project:
        return self._activate(project_id, "paused", request_id=request_id)

    def resume_project(self, project_id: str, *, request_id: str | None = None) -> Project:
        return self._activate(project_id, "active", request_id=request_id)

    def _activate(self, project_id: str, state: str, *, request_id: str | None) -> Project:
        self._require_projects_mode()
        payload = {"project_id": project_id, "activation_state": state}
        scope = f"project.activation:{project_id}:{state}"
        existing = self._idempotent_project(scope, request_id, payload)
        if existing is not None:
            return existing
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProjectError("project_not_found", "项目不存在", status=404)
        config_revision = self.repository.get_config_revision(project_id)
        assert config_revision is not None
        if state == "active":
            validation = self.validate_project(
                name=project.name,
                config=config_revision.config,
                activation_state="active",
                exclude_project_id=project_id,
            )
            if validation.fatal or validation.blockers:
                raise ProjectError(
                    "project_not_ready",
                    "项目尚未就绪",
                    status=409,
                    fields={issue.field: issue.message for issue in (*validation.fatal, *validation.blockers)},
                )
            self.repository.update_runtime(
                project_id,
                readiness_state="ready",
                failure_code=None,
                failure_summary=None,
            )
        result = self.repository.update_project_activation(project_id, state)
        if state == "paused":
            self.repository.update_runtime(project_id, auto_scan_state="paused", next_scan_at=None)
        else:
            schedule = config_revision.config["schedule"]
            next_scan = None
            auto_state = "off"
            if schedule["enabled"]:
                from .project_scheduler import next_project_scan_at

                next_scan = next_project_scan_at(config_revision.config)
                auto_state = "scheduled"
            self.repository.update_runtime(project_id, auto_scan_state=auto_state, next_scan_at=next_scan)
        self._save_idempotency(scope, request_id, payload, project_id)
        return result
