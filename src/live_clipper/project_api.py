from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import Settings
from .project_domain import stable_json
from .project_projection import project_projection, queue_positions
from .project_scan import ProjectScanError, list_source_files, scan_preview, scan_project
from .project_service import ProjectError, ProjectManager, open_project_repository


def _error(code: str, message: str, *, fields: dict[str, str] | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "fields": fields or {}}}


def _strict(body: dict[str, Any], allowed: set[str], required: set[str] = frozenset()) -> None:
    unknown = set(body) - allowed
    missing = required - set(body)
    if unknown or missing:
        fields = {field: "未知字段" for field in sorted(unknown)}
        fields.update({field: "必填字段" for field in sorted(missing)})
        raise ProjectError("validation_failed", "请求字段不完整或包含未知字段", status=422, fields=fields)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


class ProjectAPI:
    def __init__(self, service_dir: str | Path, settings: Settings) -> None:
        self.service_dir = Path(service_dir).expanduser().resolve()
        self.settings = settings
        self.repository = open_project_repository(self.service_dir)
        self.manager = ProjectManager(self.repository, settings)

    def close(self) -> None:
        self.repository.close()

    def handle(
        self,
        method: str,
        request_path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        parsed = urlparse(request_path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query, keep_blank_values=True)
        payload = body or {}
        try:
            if method == "GET" and parts == ["api", "project-form-options"]:
                return 200, {"ok": True, **self.manager.form_options()}
            if method == "POST" and parts == ["api", "projects", "scan-preview"]:
                _strict(
                    payload,
                    {"source_directory", "supported_extensions", "first_scan_mode", "lookback_days"},
                    {"source_directory", "first_scan_mode"},
                )
                extensions = payload.get("supported_extensions", [".m4v", ".mkv", ".mov", ".mp4", ".webm"])
                return 200, {
                    "ok": True,
                    **scan_preview(
                        str(payload["source_directory"]),
                        supported_extensions=extensions,
                        first_scan_mode=str(payload["first_scan_mode"]),
                        lookback_days=payload.get("lookback_days"),
                    ),
                }
            if method == "POST" and parts == ["api", "projects", "validate"]:
                _strict(payload, {"project", "activation_state"}, {"project", "activation_state"})
                project_payload = self._project_payload(payload["project"])
                result = self.manager.validate_project(
                    name=project_payload["name"],
                    config=project_payload["config"],
                    activation_state=str(payload["activation_state"]),
                )
                return 200, {
                    "ok": True,
                    "valid": result.ok,
                    "fatal": [asdict(item) for item in result.fatal],
                    "blockers": [asdict(item) for item in result.blockers],
                    "warnings": [asdict(item) for item in result.warnings],
                    "normalized_config": result.normalized_config,
                }
            if method == "POST" and parts == ["api", "projects"]:
                if self.repository.get_data_mode() != "projects":
                    raise ProjectError("migration_required", "旧版数据尚未完成迁移确认", status=409)
                _strict(payload, {"request_id", "project", "activation_state"}, {"request_id", "project", "activation_state"})
                project_payload = self._project_payload(payload["project"])
                project = self.manager.create_project(
                    **project_payload,
                    activation_state=str(payload["activation_state"]),
                    request_id=str(payload["request_id"]),
                )
                initial_scan = self._maybe_initial_scan(project.project_id)
                return 201, {
                    "ok": True,
                    "project": self._project_summary(project.project_id),
                    "initial_scan": initial_scan,
                }
            if method == "GET" and parts == ["api", "projects"]:
                return 200, {
                    "ok": True,
                    "projects": [self._project_summary(project.project_id) for project in self.repository.list_projects()],
                }
            if method == "GET" and parts == ["api", "studio"]:
                return 200, self._studio()
            if method == "POST" and parts == ["api", "studio", "seen"]:
                _strict(payload, {"through_event_id"}, {"through_event_id"})
                through = int(payload["through_event_id"])
                maximum = self.repository.max_workspace_event_id()
                if through > maximum:
                    raise ProjectError("validation_failed", "查看锚点超过服务器事件上界", status=422)
                current_view = self.repository.get_workspace_view("studio")
                if current_view is not None and through < int(current_view["last_seen_event_id"]):
                    raise ProjectError("validation_failed", "查看锚点不能倒退", status=422)
                self.repository.set_workspace_view("studio", through)
                return 200, {"ok": True, "last_seen_event_id": self.repository.get_workspace_view("studio")["last_seen_event_id"]}
            if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
                project_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    self._require_project(project_id)
                    return 200, {"ok": True, "project": self._project_summary(project_id, include_config=True)}
                if method == "PATCH" and len(parts) == 3:
                    _strict(payload, {"request_id", "expected_revision", "project"}, {"request_id", "expected_revision", "project"})
                    project_payload = self._project_payload(payload["project"])
                    project = self.manager.update_project(
                        project_id,
                        **project_payload,
                        expected_revision=int(payload["expected_revision"]),
                        request_id=str(payload["request_id"]),
                    )
                    return 200, {"ok": True, "project": self._project_summary(project.project_id, include_config=True)}
                if method == "POST" and len(parts) == 4 and parts[3] in {"enable", "pause", "resume"}:
                    _strict(payload, {"request_id"}, {"request_id"})
                    action = getattr(self.manager, f"{parts[3]}_project")
                    project = action(project_id, request_id=str(payload["request_id"]))
                    initial_scan = self._maybe_initial_scan(project.project_id) if parts[3] in {"enable", "resume"} else None
                    return 200, {
                        "ok": True,
                        "project": self._project_summary(project.project_id),
                        "initial_scan": initial_scan,
                    }
                if method == "POST" and len(parts) == 4 and parts[3] == "scans":
                    _strict(payload, {"request_id", "scope", "selected_relative_paths"}, {"request_id"})
                    request_id = str(payload["request_id"])
                    request_payload = {
                        "project_id": project_id,
                        "scope": payload.get("scope", "new"),
                        "selected_relative_paths": payload.get("selected_relative_paths", []),
                    }
                    existing = self.repository.get_idempotency_key(f"project.scan:{project_id}", request_id)
                    if existing is not None:
                        if existing["request_hash"] != _hash(request_payload):
                            raise ProjectError("request_id_conflict", "同一 request_id 的请求内容不一致")
                        scan = self.repository.get_scan_event(str(existing["object_id"]))
                        if scan is None:
                            raise ProjectError("data_integrity_error", "幂等记录指向不存在的扫描", status=500)
                        return 200, {"ok": True, "scan": asdict(scan), "reused": True}
                    report = scan_project(
                        self.repository,
                        project_id,
                        scope=str(request_payload["scope"]),
                        selected_relative_paths=list(request_payload["selected_relative_paths"]),
                        settings=self.settings,
                        service_dir=self.service_dir,
                    )
                    saved = self.repository.save_idempotency_key(
                        f"project.scan:{project_id}",
                        request_id,
                        request_hash=_hash(request_payload),
                        object_type="scan",
                        object_id=report.scan_id,
                    )
                    if not saved:
                        winner = self.repository.get_idempotency_key(f"project.scan:{project_id}", request_id)
                        if winner is None or winner["request_hash"] != _hash(request_payload):
                            raise ProjectError("request_id_conflict", "同一 request_id 的请求内容不一致")
                    return 200, {"ok": True, "scan": asdict(report)}
                if method == "GET" and len(parts) == 4 and parts[3] == "source-files":
                    return 200, {
                        "ok": True,
                        "files": [asdict(item) for item in list_source_files(self.repository, project_id)],
                    }
                if method == "GET" and len(parts) == 5 and parts[3:] == ["scans", "latest"]:
                    self._require_project(project_id)
                    scans = self.repository.list_scan_events(project_id)
                    return 200, {"ok": True, "scan": asdict(scans[0]) if scans else None}
                if method == "GET" and len(parts) == 4 and parts[3] == "runs":
                    self._require_project(project_id)
                    return 200, self._runs_page(project_id, query)
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run = self.repository.get_run(parts[2])
                if run is None:
                    raise ProjectError("run_not_found", "剪辑记录不存在", status=404)
                positions = queue_positions(self.repository.list_runs())
                return 200, {
                    "ok": True,
                    "run": {**asdict(run), "queue_position": positions.get(run.run_id)},
                    "stage_events": [asdict(item) for item in self.repository.list_stage_events(run.run_id)],
                }
            raise ProjectError("route_not_found", "API 路由不存在", status=404)
        except ProjectScanError as exc:
            return exc.status, _error(exc.code, exc.message)
        except ProjectError as exc:
            return exc.status, _error(exc.code, exc.message, fields=exc.fields)
        except (KeyError, TypeError, ValueError) as exc:
            return 422, _error("validation_failed", "请求参数无效", fields={"request": str(exc)})

    @staticmethod
    def _project_payload(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ProjectError("validation_failed", "project 必须是对象", status=422, fields={"project": "类型错误"})
        _strict(raw, {"name", "description", "config"}, {"name", "config"})
        if not isinstance(raw["config"], dict):
            raise ProjectError("validation_failed", "config 必须是对象", status=422, fields={"project.config": "类型错误"})
        return {"name": str(raw["name"]), "description": str(raw.get("description") or ""), "config": raw["config"]}

    def _require_project(self, project_id: str) -> None:
        if self.repository.get_project(project_id) is None:
            raise ProjectError("project_not_found", "项目不存在", status=404)

    def _maybe_initial_scan(self, project_id: str) -> dict[str, Any] | None:
        project = self.repository.get_project(project_id)
        runtime = self.repository.get_runtime(project_id)
        revision = self.repository.get_config_revision(project_id)
        if (
            project is None
            or runtime is None
            or revision is None
            or project.activation_state != "active"
            or runtime.first_scan_state != "pending"
            or revision.config["source"]["first_scan_mode"] != "recent"
        ):
            return None
        return asdict(
            scan_project(
                self.repository,
                project_id,
                trigger_source="initial",
                settings=self.settings,
                service_dir=self.service_dir,
            )
        )

    def _project_summary(self, project_id: str, *, include_config: bool = False) -> dict[str, Any]:
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProjectError("project_not_found", "项目不存在", status=404)
        runtime = self.repository.get_runtime(project_id)
        runs = self.repository.list_runs(project_id)
        projected = project_projection(
            activation_state=project.activation_state,
            runs=runs,
            blocked=bool(runtime and runtime.readiness_state == "blocked"),
        )
        scans = self.repository.list_scan_events(project_id)
        active_runs = [run for run in runs if run.status in {"processing", "queued", "awaiting_review"}]
        current_run = active_runs[0] if active_runs else None
        result_runs = sorted(
            (run for run in runs if run.status in {"completed", "failed"}),
            key=lambda run: (run.updated_at, run.run_id),
            reverse=True,
        )
        revision = self.repository.get_config_revision(project_id)
        result = {
            **asdict(project),
            "main_status": projected.main_status,
            "workload": projected.workload.as_dict(),
            "readiness_state": runtime.readiness_state if runtime else "blocked",
            "runtime": asdict(runtime) if runtime else None,
            "latest_scan": asdict(scans[0]) if scans else None,
            "current_run": asdict(current_run) if current_run else None,
            "recent_result": asdict(result_runs[0]) if result_runs else None,
            "blocking_issues": (
                [{"code": runtime.failure_code, "message": runtime.failure_summary}]
                if runtime and runtime.failure_code
                else []
            ),
            "schedule": (
                {
                    "enabled": revision.config["schedule"]["enabled"],
                    "timezone": revision.config["schedule"]["timezone"],
                    "next_scan_at": runtime.next_scan_at if runtime else None,
                }
                if revision
                else None
            ),
        }
        if include_config:
            result["config"] = asdict(revision) if revision else None
        return result

    def _runs_page(self, project_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        filter_name = (query.get("filter") or ["all"])[0]
        if filter_name not in {"all", "active", "attention", "completed"}:
            raise ProjectError("validation_failed", "无效的剪辑记录筛选", status=422)
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except ValueError as exc:
            raise ProjectError("validation_failed", "limit 必须是整数", status=422) from exc
        if not 1 <= limit <= 100:
            raise ProjectError("validation_failed", "limit 必须在 1 到 100 之间", status=422)
        statuses = {
            "active": {"queued", "processing"},
            "attention": {"awaiting_review", "failed"},
            "completed": {"completed"},
        }
        runs = self.repository.list_runs(project_id)
        if filter_name != "all":
            runs = [run for run in runs if run.status in statuses[filter_name]]
        cursor_value = (query.get("cursor") or [None])[0]
        if cursor_value:
            try:
                marker = base64.urlsafe_b64decode(cursor_value.encode("ascii")).decode("utf-8")
                queued_at, run_id = marker.split("\0", 1)
            except Exception as exc:  # noqa: BLE001 - malformed external cursor.
                raise ProjectError("validation_failed", "cursor 无效", status=422) from exc
            runs = [run for run in runs if (run.queued_at, run.run_id) > (queued_at, run_id)]
        page = runs[:limit]
        next_cursor = None
        if len(runs) > len(page) and page:
            marker = f"{page[-1].queued_at}\0{page[-1].run_id}".encode()
            next_cursor = base64.urlsafe_b64encode(marker).decode("ascii")
        positions = queue_positions(self.repository.list_runs())
        return {
            "ok": True,
            "runs": [{**asdict(run), "queue_position": positions.get(run.run_id)} for run in page],
            "cursor": next_cursor,
            "has_more": len(runs) > len(page),
        }

    def _studio(self) -> dict[str, Any]:
        view = self.repository.get_workspace_view("studio")
        last_seen = int(view["last_seen_event_id"]) if view else 0
        events = self.repository.list_workspace_events(after_event_id=last_seen)
        runs = self.repository.list_runs()
        counts = {status: sum(run.status == status for run in runs) for status in (
            "queued",
            "processing",
            "awaiting_review",
            "failed",
            "completed",
        )}
        run_by_id = {run.run_id: run for run in runs}

        def changed_runs(status: str | None = None, *, created: bool = False) -> list[dict[str, Any]]:
            selected: dict[str, Any] = {}
            for event in events:
                run = run_by_id.get(str(event.run_id)) if event.run_id else None
                if run is None:
                    continue
                if created and event.event_type == "run_queued":
                    selected[run.run_id] = run
                elif status is not None and event.payload.get("status") == status and run.status == status:
                    selected[run.run_id] = run
            return [asdict(run) for run in selected.values()]

        project_summaries = [self._project_summary(project.project_id) for project in self.repository.list_projects()]
        failed = [asdict(run) for run in runs if run.status == "failed"]
        processing = [asdict(run) for run in runs if run.status == "processing"]
        queued = [asdict(run) for run in runs if run.status == "queued"]
        completed = sorted(
            (run for run in runs if run.status == "completed"),
            key=lambda run: (run.completed_at or "", run.run_id),
            reverse=True,
        )[:20]
        return {
            "ok": True,
            "through_event_id": self.repository.max_workspace_event_id(),
            "changes": [asdict(event) for event in events],
            "pending_review_count": counts["awaiting_review"],
            "workload": counts,
            "unattended_changes": {
                "created": changed_runs(created=True),
                "completed": changed_runs("completed"),
                "awaiting_review": changed_runs("awaiting_review"),
                "failed": changed_runs("failed"),
            },
            "needs_attention": {
                "failed_runs": failed,
                "blocked_project_ids": [
                    project["project_id"] for project in project_summaries if project["main_status"] == "blocked"
                ],
            },
            "in_progress": {"processing": processing, "queued": queued},
            "recent_results": [asdict(run) for run in completed],
            "project_health": project_summaries,
            "projects": project_summaries,
        }
