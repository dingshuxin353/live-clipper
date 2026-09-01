from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import service
from .config import Settings
from .project_domain import Run, stable_json
from .project_resources import ResourceUnavailableError, resolve_parameter_snapshot
from .project_result_domain import RequestConflictError, RevisionConflictError
from .project_service import ProjectError, ProjectManager
from .project_storage import ProjectRepository

_TERMINAL = {"completed", "failed"}
_SOURCE_BLOCKERS = {"source_missing", "source_identity_mismatch"}


def _hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def _blocker(code: str, action: str, related_id: str) -> dict[str, str]:
    return {"code": code, "action": action, "related_id": related_id}


def _settings_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    resources = snapshot.get("resources", {})
    processing = snapshot.get("processing", {})
    output = snapshot.get("output", {})
    return {
        "asr": resources.get("asr"),
        "analysis": resources.get("analysis"),
        "ai_review": resources.get("review"),
        "render": processing.get("output_profile"),
        "naming": processing.get("naming_policy"),
        "output_directory": output.get("directory"),
        "retention": output.get("intermediate_retention"),
    }


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in after
        if before.get(field) != after.get(field)
    ]


def _existing_volume(path: Path) -> Path:
    current = path.expanduser().resolve(strict=False)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


class ProjectReprocess:
    def __init__(self, repository: ProjectRepository, settings: Settings, service_dir: Path) -> None:
        self.repository = repository
        self.settings = settings
        self.service_dir = service_dir

    def _run(self, run_id: str) -> Run:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ProjectError("run_not_found", "剪辑记录不存在", status=404)
        return run

    def preflight(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        blockers: list[dict[str, str]] = []
        if run.status not in _TERMINAL:
            blockers.append(_blocker("run_not_terminal", "active_run", run.run_id))
        project = self.repository.get_project(run.project_id)
        runtime = self.repository.get_runtime(run.project_id)
        revision = self.repository.get_config_revision(run.project_id)
        if project is None or runtime is None or revision is None:
            raise ProjectError("data_integrity_error", "项目运行数据不完整", status=500)
        if project.activation_state == "inactive":
            blockers.append(_blocker("project_inactive", "project_settings", project.project_id))
        if runtime.readiness_state != "ready" or runtime.failure_code:
            blockers.append(_blocker("project_not_ready", "project_settings", project.project_id))

        try:
            snapshot = resolve_parameter_snapshot(revision.config, self.settings)
        except ResourceUnavailableError as exc:
            snapshot = {"schema_version": revision.config["schema_version"], "resources": {}, "processing": {}, "output": revision.config["output"]}
            action = "asr_settings" if exc.resource_id == revision.config["resources"]["asr_ref"] else "ai_settings"
            blockers.append(_blocker("resource_unavailable", action, exc.resource_id))
        validation = ProjectManager(self.repository, self.settings).validate_project(
            name=project.name,
            config=revision.config,
            activation_state="active",
            exclude_project_id=project.project_id,
        )
        for item in (*validation.fatal, *validation.blockers):
            action = "asr_settings" if item.field == "resources.asr_ref" else (
                "ai_settings" if item.field.startswith("resources.") else "project_settings"
            )
            blocker = _blocker(item.code, action, project.project_id)
            if blocker not in blockers:
                blockers.append(blocker)

        source_path = Path(run.latest_seen_path).expanduser().resolve(strict=False)
        source: dict[str, Any] = {
            "path": str(source_path),
            "name": source_path.name,
            "expected_content_id": run.content_id,
            "content_id": None,
            "bytes": None,
            "mtime_ns": None,
            "state": "missing",
        }
        try:
            before = source_path.stat()
            identity = service.content_identity(source_path, service_dir=self.service_dir)
            stat = source_path.stat()
            if (
                before.st_size != stat.st_size
                or before.st_mtime_ns != stat.st_mtime_ns
                or int(identity["bytes"]) != stat.st_size
            ):
                raise service.SourceChangedDuringHash(source_path)
            source.update(
                content_id=identity["content_id"],
                bytes=int(identity["bytes"]),
                mtime_ns=stat.st_mtime_ns,
                state="ready" if identity["content_id"] == run.content_id else "identity_mismatch",
            )
        except service.SourceChangedDuringHash:
            source["state"] = "identity_mismatch"
        except OSError:
            pass
        if source["state"] == "missing":
            blockers.append(_blocker("source_missing", "source_repair", run.run_id))
        elif source["state"] != "ready":
            blockers.append(_blocker("source_identity_mismatch", "source_repair", run.run_id))

        work_root = Path(self.settings.paths.work_dir)
        usage = shutil.disk_usage(_existing_volume(work_root))
        required = int(source["bytes"] or 0)
        space = {
            "work_directory": str(work_root.expanduser().resolve(strict=False)),
            "required_bytes": required,
            "available_bytes": usage.free,
            "additional_estimate_bytes": None,
            "sufficient": not required or usage.free >= required,
        }
        if required and not space["sufficient"]:
            blockers.append(_blocker("storage_full", "project_settings", project.project_id))

        if source["state"] == "ready":
            snapshot = {**snapshot, "source": {"bytes": source["bytes"], "mtime_ns": source["mtime_ns"], "content_id": source["content_id"]}}
        current_summary = _settings_summary(snapshot)
        origin_summary = _settings_summary(run.parameter_snapshot)
        active = self.repository.find_active_run(run.project_id, run.content_id)
        versions = self.repository.list_content_runs(run.project_id, run.content_id)
        revision_facts = {
            "run_id": run.run_id,
            "run_status": run.status,
            "project_revision": revision.revision,
            "runtime": [runtime.readiness_state, runtime.failure_code],
            "source": [source["path"], source["content_id"], source["bytes"], source["mtime_ns"]],
            "settings": current_summary,
            "work_directory": space["work_directory"],
            "space_sufficient": space["sufficient"],
            "active_run_id": active.run_id if active else None,
            "next_processing_sequence": max((item.processing_sequence for item in versions), default=0) + 1,
            "blockers": blockers,
        }
        return {
            "ok": True,
            "run": {"run_id": run.run_id, "project_id": run.project_id, "status": run.status, "processing_sequence": run.processing_sequence},
            "source": source,
            "current_settings": {"config_revision": revision.revision, "summary": current_summary, "snapshot": snapshot},
            "changes": _changes(origin_summary, current_summary),
            "space": space,
            "active_run": self._version_identity(active) if active else None,
            "next_processing_sequence": revision_facts["next_processing_sequence"],
            "blockers": blockers,
            "can_reprocess": not blockers,
            "preflight_revision": _hash(revision_facts),
        }

    def create(self, run_id: str, *, request_id: str, expected_preflight_revision: str) -> tuple[dict[str, Any], int]:
        request_hash = _hash({"origin_run_id": run_id})
        existing = self.repository.get_idempotency_key(f"run_reprocess:{run_id}", request_id)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ProjectError("request_id_conflict", "同一 request_id 不能用于不同操作")
            run = self._run(str(existing["object_id"]))
            return {"ok": True, "run": self._version_identity(run), "created": False, "reuse_reason": "idempotent_request"}, 200
        preflight = self.preflight(run_id)
        if preflight["preflight_revision"] != expected_preflight_revision:
            raise ProjectError("preflight_changed", "预检状态已变化，请重新确认", status=409)
        if preflight["blockers"]:
            raise ProjectError("reprocess_blocked", "当前记录无法重新处理", status=422)
        try:
            run, reason = self.repository.create_reprocess_run(
                run_id,
                request_id=request_id,
                request_hash=request_hash,
                config_revision=int(preflight["current_settings"]["config_revision"]),
                parameter_snapshot=preflight["current_settings"]["snapshot"],
                source_path=str(preflight["source"]["path"]),
            )
        except RequestConflictError as exc:
            raise ProjectError("request_id_conflict", "同一 request_id 不能用于不同操作") from exc
        except RevisionConflictError as exc:
            raise ProjectError("preflight_changed", "预检状态已变化，请重新确认", status=409) from exc
        return {
            "ok": True,
            "run": self._version_identity(run),
            "created": reason == "created",
            "reuse_reason": None if reason == "created" else reason,
        }, 201 if reason == "created" else 200

    def source_repair(self, run_id: str) -> dict[str, Any]:
        preflight = self.preflight(run_id)
        source_blocker = next((item for item in preflight["blockers"] if item["code"] in _SOURCE_BLOCKERS), None)
        if source_blocker is None:
            raise ProjectError("source_repair_not_required", "当前记录没有来源问题", status=409)
        run = self._run(run_id)
        group = f"reprocess-source:{run_id}"
        existing = next(
            (item for item in self.repository.list_issues(run_id=run_id, active_only=True) if item.issue_group_key == group),
            None,
        )
        issue = existing or self.repository.discover_issue(
            issue_code=source_blocker["code"],
            category="recording",
            scope_type="run",
            project_id=run.project_id,
            run_id=run.run_id,
            issue_group_key=group,
            title="原始录像需要重新定位",
            summary=(
                "原始录像不存在或不可读"
                if source_blocker["code"] == "source_missing"
                else "当前录像与原始内容不一致"
            ),
            impact="重新处理已阻止",
            preserved_content="既有版本与成片保持不变",
            next_step="选择与原始内容一致的录像文件",
            recovery_capability="operational_repair",
            safe_checkpoint="source_identity",
        )
        return {"ok": True, "issue": asdict(issue), "reused": existing is not None}

    def versions(self, run_id: str) -> dict[str, Any]:
        current = self._run(run_id)
        current_settings = _settings_summary(current.parameter_snapshot)
        current_result = self._result_summary(current.run_id)
        versions = []
        for run in self.repository.list_content_runs(current.project_id, current.content_id):
            summary = _settings_summary(run.parameter_snapshot)
            result = self._result_summary(run.run_id)
            changed = [item["field"] for item in _changes(current_settings, summary)]
            if result != current_result:
                changed.append("result_summary")
            versions.append({
                **self._version_identity(run),
                "settings_summary": summary,
                "result_summary": result,
                "changed_fields": changed,
            })
        return {"ok": True, "run_id": run_id, "project_id": current.project_id, "content_id": current.content_id, "versions": versions}

    def _result_summary(self, run_id: str) -> dict[str, Any] | None:
        result = self.repository.get_run_result(run_id)
        if result is None:
            return None
        return {
            "result_type": result.result_type,
            "selected_count": result.selected_count,
            "available_output_count": result.available_output_count,
            "failed_output_count": result.failed_output_count,
            "total_duration_ms": result.total_duration_ms,
            "result_revision": result.result_revision,
            "completed_at": result.completed_at,
        }

    @staticmethod
    def _version_identity(run: Run) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "status": run.status,
            "current_stage": run.current_stage,
            "processing_sequence": run.processing_sequence,
            "origin_run_id": run.origin_run_id,
            "config_revision": run.config_revision,
            "queued_at": run.queued_at,
            "started_at": run.started_at,
            "review_at": run.review_at,
            "completed_at": run.completed_at,
            "updated_at": run.updated_at,
            "error_code": run.error_code,
        }
