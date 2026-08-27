from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from . import config_editor, onboarding
from .config import Settings
from .project_domain import stable_json
from .project_file_grants import (
    FileSelectionGrantError,
    FileSelectionGrantStore,
    process_file_selection_grants,
)
from .project_recovery import continue_run, recheck_issue, retry_material, retry_output
from .project_resources import resource_map, resource_repair_context
from .project_result_domain import RequestConflictError, RevisionConflictError
from .project_storage import ProjectRepository

_ACTIVE_ISSUE_STATUSES = {"retrying", "action_required", "checking", "ready_to_recover", "recovering"}
_RESULT_LIST_TYPES = {"clips_ready", "no_clip", "partial"}
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class ResultAPIError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        fields: Mapping[str, str] | None = None,
        current: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.fields = dict(fields or {})
        self.current = current
        super().__init__(message)


@dataclass(frozen=True)
class MediaResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _error(error: ResultAPIError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": error.code, "message": error.message, "fields": error.fields},
    }
    if error.current is not None:
        payload["current"] = error.current
    return payload


def _strict(body: Mapping[str, Any], allowed: set[str], required: set[str] = frozenset()) -> None:
    unknown = set(body) - allowed
    missing = required - set(body)
    if unknown or missing:
        fields = {field: "未知字段" for field in sorted(unknown)}
        fields.update({field: "必填字段" for field in sorted(missing)})
        raise ResultAPIError("validation_failed", "请求字段不完整或包含未知字段", status=422, fields=fields)


def _clean_string(value: Any, field: str, *, minimum: int = 1, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise ResultAPIError("validation_failed", f"{field} 必须是字符串", status=422, fields={field: "类型错误"})
    normalized = value.strip()
    if _CONTROL_CHARACTER.search(normalized) or not minimum <= len(normalized) <= maximum:
        raise ResultAPIError(
            "validation_failed",
            f"{field} 长度或内容无效",
            status=422,
            fields={field: f"长度必须为 {minimum}～{maximum} 且不能包含控制字符"},
        )
    return normalized


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ResultAPIError("validation_failed", f"{field} 必须是整数", status=422, fields={field: "类型错误"})
    return value


def _request_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(dict(payload)).encode("utf-8")).hexdigest()


class ProjectResultAPI:
    def __init__(
        self,
        repository: ProjectRepository,
        settings: Settings,
        *,
        service_dir: str | Path,
        grants: FileSelectionGrantStore | None = None,
        auth_context: str = "browser",
        config_path: str | Path = "live-clipper.toml",
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.service_dir = Path(service_dir).expanduser().resolve()
        self.grants = grants or process_file_selection_grants()
        self.auth_context = auth_context
        self.config_path = Path(config_path).expanduser().resolve()

    def handle(
        self,
        method: str,
        request_path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        parsed = urlparse(request_path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query, keep_blank_values=True)
        payload = body or {}
        try:
            if method == "GET" and parts == ["api", "clips"]:
                return 200, self.clips(query)
            if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                run_id = parts[2]
                if method == "GET" and parts[3:] == ["result"]:
                    return 200, self.run_result(run_id)
                if method == "POST" and parts[3:] == ["result", "seen"]:
                    return 200, self.mark_seen(run_id, payload)
            if len(parts) >= 3 and parts[:2] == ["api", "outputs"]:
                output_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    return 200, {"ok": True, "output": self.output_dto(output_id, include_path=True)}
                if method == "GET" and parts[3:] == ["material"]:
                    return 200, {"ok": True, "material": self.material_dto(output_id)}
                if method == "PATCH" and parts[3:] == ["material"]:
                    return 200, self.update_material(output_id, payload)
            if len(parts) >= 3 and parts[:2] == ["api", "issues"]:
                issue_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    return 200, {"ok": True, "issue": self.issue_dto(self._issue(issue_id), include_events=True)}
                if method == "POST" and len(parts) == 4:
                    return 200, self.issue_action(issue_id, parts[3], payload)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "issue-groups"] and parts[3] == "recheck":
                return 200, self.group_recheck(parts[2], payload)
            if len(parts) >= 4 and parts[:2] == ["api", "resources"]:
                resource_id = parts[2]
                if method == "GET" and parts[3:] == ["repair-context"]:
                    return 200, self.repair_context(resource_id, query)
                if method == "PATCH" and parts[3:] == ["connection"]:
                    return 200, self.update_connection(resource_id, payload)
                if method == "POST" and parts[3:] == ["connection-test"]:
                    return 200, self.connection_test(resource_id, payload)
            if method == "POST" and parts == ["api", "desktop", "file-selections"]:
                return 201, self.create_file_selection(payload)
            if method == "GET" and len(parts) == 5 and parts[:3] == ["api", "desktop", "outputs"] and parts[4] == "path":
                return 200, self.desktop_output_path(parts[3])
            if method == "GET" and parts == ["api", "legacy", "awaiting-review"]:
                return 200, self.legacy_awaiting_review()
            return None
        except ResultAPIError as exc:
            return exc.status, _error(exc)
        except FileSelectionGrantError as exc:
            return 409, _error(ResultAPIError(exc.code, "文件选择授权无效或已失效"))
        except RevisionConflictError:
            return 409, _error(ResultAPIError("revision_conflict", "服务器内容已更新，请刷新后重试"))
        except RequestConflictError:
            return 409, _error(ResultAPIError("request_id_conflict", "同一 request_id 不能用于不同操作"))
        except (KeyError, TypeError, ValueError):
            return 422, _error(ResultAPIError("validation_failed", "请求参数无效", status=422))
        except Exception:  # noqa: BLE001 - external responses must never expose raw exceptions.
            return 500, _error(ResultAPIError("internal_error", "服务暂时无法完成请求", status=500))

    def clips(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        unknown = set(query) - {"view", "limit", "cursor"}
        repeated = {key for key, values in query.items() if len(values) != 1}
        if unknown or repeated:
            fields = {key: "未知查询参数" for key in sorted(unknown)}
            fields.update({key: "查询参数不能重复" for key in sorted(repeated)})
            raise ResultAPIError("validation_failed", "查询参数无效", status=422, fields=fields)
        view = (query.get("view") or ["new"])[0]
        if view not in {"new", "all"}:
            raise ResultAPIError("validation_failed", "view 无效", status=422, fields={"view": "仅支持 new 或 all"})
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except ValueError as exc:
            raise ResultAPIError("validation_failed", "limit 必须是整数", status=422) from exc
        if not 1 <= limit <= 100:
            raise ResultAPIError("validation_failed", "limit 必须在 1 到 100 之间", status=422)
        results = [
            result
            for result in self.repository.list_run_results(unseen_only=view == "new")
            if result.result_type in _RESULT_LIST_TYPES
        ]
        cursor = (query.get("cursor") or [None])[0]
        if cursor:
            completed_at, run_id = self._decode_cursor(cursor)
            results = [item for item in results if (item.completed_at, item.run_id) < (completed_at, run_id)]
        page = results[:limit]
        has_more = len(results) > len(page)
        return {
            "ok": True,
            "view": view,
            "unseen_result_count": self.unseen_result_count(),
            "results": [self.result_summary(item) for item in page],
            "cursor": self._encode_cursor(page[-1].completed_at, page[-1].run_id) if has_more and page else None,
            "has_more": has_more,
        }

    @staticmethod
    def _encode_cursor(completed_at: str, run_id: str) -> str:
        return base64.urlsafe_b64encode(f"{completed_at}\0{run_id}".encode()).decode("ascii")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[str, str]:
        try:
            marker = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            completed_at, run_id = marker.split("\0", 1)
            if not completed_at or not run_id:
                raise ValueError
            return completed_at, run_id
        except Exception as exc:  # noqa: BLE001 - external opaque cursor.
            raise ResultAPIError("validation_failed", "cursor 无效", status=422, fields={"cursor": "格式错误"}) from exc

    def result_summary(self, result: Any) -> dict[str, Any]:
        run = self.repository.get_run(result.run_id)
        if run is None:
            raise ResultAPIError("data_integrity_error", "结果关联的剪辑记录不存在", status=500)
        project = self.repository.get_project(run.project_id)
        ready = [item for item in self.repository.list_run_outputs(run.run_id) if item.status == "ready"]
        issues = self.repository.list_issues(run_id=run.run_id, active_only=True)
        return {
            "run_id": run.run_id,
            "project": {"project_id": run.project_id, "name": project.name if project else ""},
            "source_name": Path(run.latest_seen_path).name,
            "result_type": result.result_type,
            "result_revision": result.result_revision,
            "seen": not result.unseen,
            "overall_summary": result.overall_summary,
            "available_output_count": result.available_output_count,
            "failed_output_count": result.failed_output_count,
            "total_duration_ms": result.total_duration_ms,
            "primary_output_id": ready[0].output_id if ready else None,
            "completed_at": result.completed_at,
            "issue_summary": self.issue_summary(issues[0]) if issues else None,
        }

    def run_result(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ResultAPIError("run_not_found", "剪辑记录不存在", status=404)
        result = self.repository.get_run_result(run_id)
        if result is None:
            raise ResultAPIError(
                "result_not_ready",
                "剪辑结果尚未形成",
                current={"run_id": run.run_id, "status": run.status, "current_stage": run.current_stage},
            )
        session = self.repository.get_ai_review_session(result.review_session_id)
        decisions = self.repository.get_candidate_decisions(result.review_session_id)
        issues = self.repository.list_issues(run_id=run_id, active_only=True)
        return {
            "ok": True,
            "result": self.result_dto(result),
            "review_session": self.review_session_dto(session) if session else None,
            "decisions": [self.decision_dto(item) for item in decisions],
            "outputs": [self.output_dto(item.output_id) for item in self.repository.list_run_outputs(run_id)],
            "issues": [self.issue_summary(item) for item in issues],
            "available_actions": ["mark_seen"] if result.unseen else [],
        }

    @staticmethod
    def result_dto(result: Any) -> dict[str, Any]:
        return {
            "run_id": result.run_id,
            "review_session_id": result.review_session_id,
            "result_type": result.result_type,
            "candidate_count": result.candidate_count,
            "selected_count": result.selected_count,
            "rejected_count": result.rejected_count,
            "available_output_count": result.available_output_count,
            "failed_output_count": result.failed_output_count,
            "total_duration_ms": result.total_duration_ms,
            "overall_summary": result.overall_summary,
            "warnings": result.warnings,
            "format_version": result.format_version,
            "result_revision": result.result_revision,
            "seen": not result.unseen,
            "seen_at": result.result_seen_at,
            "source_kind": result.source_kind,
            "completed_at": result.completed_at,
            "updated_at": result.updated_at,
        }

    @staticmethod
    def review_session_dto(session: Any) -> dict[str, Any]:
        return {
            "review_session_id": session.review_session_id,
            "attempt_number": session.attempt_number,
            "status": session.status,
            "resource_ref": session.resource_ref,
            "model_name": session.model_name,
            "strategy_version": session.strategy_version,
            "format_version": session.format_version,
            "overall_summary": session.overall_summary,
            "warnings": session.warnings,
            "candidate_count": session.candidate_count,
            "selected_count": session.selected_count,
            "rejected_count": session.rejected_count,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "validated_at": session.validated_at,
        }

    @staticmethod
    def decision_dto(decision: Any) -> dict[str, Any]:
        return {
            "decision_id": decision.decision_id,
            "candidate_id": decision.candidate_id,
            "decision": decision.decision,
            "rank": decision.rank,
            "candidate_type": decision.candidate_type,
            "source_start_ms": decision.source_start_ms,
            "source_end_ms": decision.source_end_ms,
            "selected_start_ms": decision.selected_start_ms,
            "selected_end_ms": decision.selected_end_ms,
            "remove_ranges": decision.remove_ranges,
            "hook": decision.hook,
            "core_value": decision.core_value,
            "reason": decision.reason,
            "rejection_reason_code": decision.rejection_reason_code,
            "risks": decision.risks,
            "transcript_excerpt": decision.transcript_excerpt,
            "output_id": decision.output_id,
        }

    def mark_seen(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _strict(payload, {"request_id", "expected_result_revision"}, {"request_id", "expected_result_revision"})
        request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
        expected = _integer(payload["expected_result_revision"], "expected_result_revision")
        result = self.repository.get_run_result(run_id)
        if result is None:
            if self.repository.get_run(run_id) is None:
                raise ResultAPIError("run_not_found", "剪辑记录不存在", status=404)
            raise ResultAPIError("result_not_ready", "剪辑结果尚未形成")
        operation = {"run_id": run_id, "expected_result_revision": expected}
        if self._idempotent("result.seen", request_id, operation, run_id):
            return {"ok": True, "result": self.result_dto(result), "unseen_result_count": self.unseen_result_count(), "reused": True}
        if result.result_revision != expected:
            raise ResultAPIError("revision_conflict", "结果已更新", current=self.result_dto(result))
        updated = self.repository.mark_result_seen(run_id, expected_result_revision=expected)
        self._save_idempotency("result.seen", request_id, operation, "run_result", run_id)
        return {"ok": True, "result": self.result_dto(updated), "unseen_result_count": self.unseen_result_count(), "reused": False}

    def output_dto(self, output_id: str, *, include_path: bool = False) -> dict[str, Any]:
        output = self.repository.get_run_output(output_id)
        if output is None:
            raise ResultAPIError("output_not_found", "成片不存在", status=404)
        run = self.repository.get_run(output.run_id)
        if run is None:
            raise ResultAPIError("data_integrity_error", "成片关联的剪辑记录不存在", status=500)
        material = self.repository.get_output_material(output_id)
        issues = [item for item in self.repository.list_issues(run_id=run.run_id, active_only=True) if item.output_id == output_id]
        target = self.resolve_output_path(output_id, require_available=False)
        available = output.status == "ready" and self._output_is_verified(output, run, target)
        if output.status == "ready" and not available:
            self._record_unavailable_output(output, target)
            output = self.repository.get_run_output(output_id)
            assert output is not None
            issues = [
                item
                for item in self.repository.list_issues(run_id=run.run_id, active_only=True)
                if item.output_id == output_id
            ]
        result = {
            "output_id": output.output_id,
            "run_id": output.run_id,
            "project_id": run.project_id,
            "candidate_id": output.candidate_id,
            "status": output.status,
            "display_order": output.display_order,
            "file_name": output.file_name,
            "duration_ms": output.duration_ms,
            "width": output.width,
            "height": output.height,
            "container": output.container,
            "video_codec": output.video_codec,
            "byte_size": output.byte_size,
            "generated_at": output.generated_at,
            "verified_at": output.verified_at,
            "available": available,
            "media_url": f"/api/outputs/{output.output_id}/media" if available else None,
            "material": self.material_summary(material) if material else None,
            "active_issue_summary": self.issue_summary(issues[0]) if issues else None,
        }
        if include_path:
            result["display_path"] = str(target)
        return result

    def resolve_output_path(self, output_id: str, *, require_available: bool = True) -> Path:
        output = self.repository.get_run_output(output_id)
        if output is None:
            raise ResultAPIError("output_not_found", "成片不存在", status=404)
        run = self.repository.get_run(output.run_id)
        if run is None:
            raise ResultAPIError("output_not_found", "成片不存在", status=404)
        if output.storage_kind == "project_output":
            root_value = run.parameter_snapshot.get("output", {}).get("directory")
            root = Path(str(root_value or "")).expanduser().resolve()
        elif output.storage_kind == "run_workspace_compat":
            root = (self.settings.paths.work_dir / "projects" / run.project_id / "runs" / run.run_id).expanduser().resolve()
        else:
            raise ResultAPIError("output_unavailable", "成片存储类型不受支持")
        relative = Path(output.relative_path)
        if relative.is_absolute():
            raise ResultAPIError("output_unavailable", "成片路径不在受控目录内")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ResultAPIError("output_unavailable", "成片路径不在受控目录内") from exc
        if require_available and (output.status != "ready" or not self._output_is_verified(output, run, target)):
            self._record_unavailable_output(output, target)
            raise ResultAPIError("output_unavailable", "成片当前不可用")
        return target

    def _output_is_verified(self, output: Any, run: Any, target: Path) -> bool:
        try:
            if not target.is_file() or output.byte_size is None or target.stat().st_size != output.byte_size:
                return False
            run_dir = self.settings.paths.work_dir / "projects" / run.project_id / "runs" / run.run_id
            evidence_path = run_dir / "outputs" / output.output_id / "media_integrity.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            registered = {
                "duration_ms": output.duration_ms,
                "width": output.width,
                "height": output.height,
                "container": output.container,
                "video_codec": output.video_codec,
                "byte_size": output.byte_size,
            }
            expected_hash = evidence.get("sha256") if isinstance(evidence, dict) else None
            if (
                evidence.get("format_version") != 1
                or evidence.get("output_id") != output.output_id
                or evidence.get("media_metadata") != registered
                or not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            ):
                return False
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == expected_hash
        except (OSError, json.JSONDecodeError):
            return False

    def _record_unavailable_output(self, output: Any, target: Path) -> None:
        if output.status != "ready":
            return
        status = "missing" if not target.is_file() else "unreadable"
        code = "output_missing" if status == "missing" else "output_unreadable"
        summary = "已登记成片文件不存在" if status == "missing" else "已登记成片文件完整性校验失败"
        self.repository.update_output_and_reproject_result(
            output.output_id,
            status=status,
            error_code=code,
            error_summary=summary,
        )

    def media(self, output_id: str, range_header: str | None = None, *, head_only: bool = False) -> MediaResponse:
        output = self.repository.get_run_output(output_id)
        if output is None:
            raise ResultAPIError("output_not_found", "成片不存在", status=404)
        target = self.resolve_output_path(output_id)
        size = target.stat().st_size
        start, end, status = 0, max(size - 1, 0), 200
        if range_header:
            try:
                start, end = self._parse_range(range_header, size)
            except ResultAPIError as exc:
                exc.current = {"size": size}
                raise
            status = 206
        length = max(end - start + 1, 0)
        headers = {
            "Content-Type": "video/mp4" if str(output.container).lower() == "mp4" else "application/octet-stream",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        body = b""
        if not head_only and length:
            with target.open("rb") as handle:
                handle.seek(start)
                body = handle.read(length)
        return MediaResponse(status, headers, body)

    @staticmethod
    def _parse_range(header: str, size: int) -> tuple[int, int]:
        if size <= 0 or not header.startswith("bytes=") or "," in header:
            raise ResultAPIError("range_not_satisfiable", "媒体 Range 无效", status=416)
        value = header[6:].strip()
        if "-" not in value:
            raise ResultAPIError("range_not_satisfiable", "媒体 Range 无效", status=416)
        first, last = value.split("-", 1)
        try:
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    raise ValueError
                return max(size - suffix, 0), size - 1
            start = int(first)
            end = int(last) if last else size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError
            return start, min(end, size - 1)
        except ValueError as exc:
            raise ResultAPIError("range_not_satisfiable", "媒体 Range 无效", status=416) from exc

    def material_dto(self, output_id: str) -> dict[str, Any]:
        material = self.repository.get_output_material(output_id)
        if material is None:
            if self.repository.get_run_output(output_id) is None:
                raise ResultAPIError("output_not_found", "成片不存在", status=404)
            raise ResultAPIError("material_not_found", "发布物料不存在", status=404)
        issues = [
            item
            for item in self.repository.list_issues(run_id=self.repository.get_run_output(output_id).run_id, active_only=True)
            if item.material_id == material.material_id
        ]
        return {
            "material_id": material.material_id,
            "output_id": material.output_id,
            "status": material.status,
            "material_revision": material.material_revision,
            "titles": material.title_candidates,
            "preferred_title_id": material.preferred_title_id,
            "description": material.description,
            "tags": material.tags,
            "generated_from": material.generation_source,
            "saved_at": material.saved_at,
            "active_issue_summary": self.issue_summary(issues[0]) if issues else None,
        }

    @staticmethod
    def material_summary(material: Any) -> dict[str, Any]:
        return {
            "material_id": material.material_id,
            "status": material.status,
            "material_revision": material.material_revision,
            "preferred_title_id": material.preferred_title_id,
            "saved_at": material.saved_at,
        }

    def update_material(self, output_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _strict(
            payload,
            {"request_id", "expected_revision", "titles", "preferred_title_id", "description", "tags"},
            {"request_id", "expected_revision", "titles", "preferred_title_id", "description", "tags"},
        )
        request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
        expected = _integer(payload["expected_revision"], "expected_revision")
        current = self.repository.get_output_material(output_id)
        if current is None:
            raise ResultAPIError("material_not_found", "发布物料不存在", status=404)
        titles = payload["titles"]
        if not isinstance(titles, list) or len(titles) != len(current.title_candidates):
            raise ResultAPIError("validation_failed", "标题候选集合不能增删", status=422, fields={"titles": "集合不一致"})
        normalized_titles: list[dict[str, str]] = []
        for index, item in enumerate(titles):
            if not isinstance(item, dict) or set(item) != {"title_id", "text"}:
                raise ResultAPIError("validation_failed", "标题格式无效", status=422, fields={f"titles.{index}": "格式错误"})
            normalized_titles.append({
                "title_id": _clean_string(item["title_id"], f"titles.{index}.title_id", maximum=128),
                "text": _clean_string(item["text"], f"titles.{index}.text", maximum=100),
            })
        current_ids = {item["title_id"] for item in current.title_candidates}
        if {item["title_id"] for item in normalized_titles} != current_ids:
            raise ResultAPIError("validation_failed", "标题候选 ID 集合不能改变", status=422, fields={"titles": "ID 集合不一致"})
        preferred = _clean_string(payload["preferred_title_id"], "preferred_title_id", maximum=128)
        if preferred not in current_ids:
            raise ResultAPIError("validation_failed", "首选标题不属于候选集合", status=422, fields={"preferred_title_id": "引用无效"})
        description = str(payload["description"])
        if _CONTROL_CHARACTER.search(description) or len(description) > 2000:
            raise ResultAPIError("validation_failed", "描述内容无效", status=422, fields={"description": "最多 2000 字且不能包含控制字符"})
        raw_tags = payload["tags"]
        if not isinstance(raw_tags, list) or len(raw_tags) > 20:
            raise ResultAPIError("validation_failed", "标签最多 20 个", status=422, fields={"tags": "数量无效"})
        tags: list[str] = []
        for index, tag in enumerate(raw_tags):
            normalized = _clean_string(tag, f"tags.{index}", maximum=31).lstrip("#").strip()
            if not 1 <= len(normalized) <= 30:
                raise ResultAPIError("validation_failed", "标签长度无效", status=422, fields={f"tags.{index}": "应为 1～30 字"})
            if normalized not in tags:
                tags.append(normalized)
        operation = {
            "output_id": output_id,
            "expected_revision": expected,
            "titles": normalized_titles,
            "preferred_title_id": preferred,
            "description": description,
            "tags": tags,
        }
        scope = f"material.update:{output_id}"
        if self._idempotent(scope, request_id, operation, current.material_id):
            return {"ok": True, "material": self.material_dto(output_id), "reused": True}
        if current.material_revision != expected:
            raise ResultAPIError("revision_conflict", "发布物料已更新", current=self.material_dto(output_id))
        updated = self.repository.update_output_material(
            output_id,
            expected_material_revision=expected,
            title_candidates=normalized_titles,
            preferred_title_id=preferred,
            description=description,
            tags=tags,
        )
        for issue in self.repository.list_issues(run_id=self.repository.get_run_output(output_id).run_id, active_only=True):
            if issue.material_id == updated.material_id and issue.issue_code == "material_generation_failed":
                self.repository.transition_issue(
                    issue.issue_id,
                    expected_issue_revision=issue.issue_revision,
                    status="resolved",
                    event_type="material_saved",
                )
        self._save_idempotency(scope, request_id, operation, "material", updated.material_id)
        return {"ok": True, "material": self.material_dto(output_id), "reused": False}

    def _issue(self, issue_id: str) -> Any:
        issue = self.repository.get_issue(issue_id)
        if issue is None:
            raise ResultAPIError("issue_not_found", "问题不存在", status=404)
        return issue

    def issue_summary(self, issue: Any) -> dict[str, Any]:
        return {
            "issue_id": issue.issue_id,
            "issue_code": issue.issue_code,
            "group_key": issue.issue_group_key,
            "status": issue.status,
            "impact_level": issue.impact_level,
            "title": issue.title,
            "summary": issue.summary,
            "next_step": issue.next_step,
            "issue_revision": issue.issue_revision,
            "available_actions": self.issue_actions(issue),
        }

    def issue_dto(self, issue: Any, *, include_events: bool = False) -> dict[str, Any]:
        result = {
            **self.issue_summary(issue),
            "category": issue.category,
            "scope": {
                "type": issue.scope_type,
                "project_id": issue.project_id,
                "run_id": issue.run_id,
                "output_id": issue.output_id,
                "material_id": issue.material_id,
            },
            "impact": issue.impact,
            "preserved_content": issue.preserved_content,
            "safe_checkpoint": issue.safe_checkpoint,
            "reuse_stages": issue.reuse_stages,
            "redo_stages": issue.redo_stages,
            "automatic_attempt_count": issue.automatic_attempt_count,
            "total_attempt_count": issue.total_attempt_count,
            "next_retry_at": issue.next_retry_at,
            "retry_exhausted": issue.retry_exhausted,
            "diagnostic": {"diagnostic_id": issue.diagnostic_id, "summary": issue.diagnostic_summary},
            "occurred_at": issue.occurred_at,
            "updated_at": issue.updated_at,
            "resolved_at": issue.resolved_at,
        }
        if include_events:
            result["events"] = [asdict(item) for item in self.repository.list_issue_events(issue.issue_id)]
        return result

    @staticmethod
    def issue_actions(issue: Any) -> list[str]:
        actions: list[str] = []
        if issue.status in {"action_required", "retrying"}:
            actions.append("recheck")
            if issue.issue_code in {"ai_resource_unavailable", "asr_resource_unavailable"}:
                actions.append("open_resource_repair")
            if issue.issue_code in {"source_missing", "source_unreadable"}:
                actions.append("select_source")
            if issue.issue_code in {"output_unwritable", "storage_full"}:
                actions.append("select_recovery_output")
        if issue.status == "ready_to_recover":
            actions.extend({
                "continue_run": ["continue_run"],
                "retry_output": ["retry_output"],
                "retry_material": ["retry_material"],
            }.get(issue.recovery_capability, []))
        if issue.diagnostic_id:
            actions.append("copy_diagnostic")
        return actions

    def issue_action(self, issue_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action in {"recheck", "source", "recovery-output"}:
            allowed = {"request_id", "expected_issue_revision"}
            if action != "recheck":
                allowed.add("selection_token")
            _strict(payload, allowed, allowed)
            request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
            expected = _integer(payload["expected_issue_revision"], "expected_issue_revision")
            issue = self._issue(issue_id)
            if issue.issue_revision != expected:
                raise ResultAPIError("revision_conflict", "问题已更新", current=self.issue_dto(issue))
            operation = {"issue_id": issue_id, "action": action, "expected_issue_revision": expected}
            scope = f"issue.{action}:{issue_id}"
            if self._idempotent(scope, request_id, operation, issue_id):
                return {"ok": True, "issue": self.issue_dto(self._issue(issue_id)), "reused": True}
            overrides: dict[str, Any] | None = None
            if action != "recheck":
                kind = "source" if action == "source" else "recovery_output"
                selected = self.grants.consume(str(payload["selection_token"]), issue_id=issue_id, kind=kind)
                overrides = {"source_path" if kind == "source" else "output_directory": str(selected)}
            updated = recheck_issue(
                self.repository,
                issue_id,
                expected_issue_revision=expected,
                operational_overrides=overrides,
                settings=self.settings,
            )
            if action == "source" and updated.status != "ready_to_recover":
                raise ResultAPIError("source_identity_mismatch", "所选录像与原始内容不一致")
            self._save_idempotency(scope, request_id, operation, "issue", issue_id)
            return {"ok": True, "issue": self.issue_dto(updated), "reused": False}
        if action in {"continue", "retry-output", "retry-material"}:
            _strict(payload, {"request_id", "expected_issue_revision"}, {"request_id", "expected_issue_revision"})
            request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
            expected = _integer(payload["expected_issue_revision"], "expected_issue_revision")
            issue = self._issue(issue_id)
            if issue.issue_revision != expected:
                raise ResultAPIError("revision_conflict", "问题已更新", current=self.issue_dto(issue))
            try:
                if action == "continue":
                    attempt = continue_run(self.repository, issue_id, expected_issue_revision=expected, request_id=request_id, requested_by="web")
                elif action == "retry-output":
                    attempt = retry_output(self.repository, issue_id, expected_issue_revision=expected, request_id=request_id, requested_by="web")
                else:
                    attempt = retry_material(self.repository, issue_id, expected_issue_revision=expected, request_id=request_id, requested_by="web")
            except ValueError as exc:
                code = str(exc)
                if "not_ready" in code:
                    raise ResultAPIError("issue_not_ready", "问题尚未通过检查") from exc
                if action == "retry-output":
                    raise ResultAPIError("output_not_retryable", "当前问题不能重试成片") from exc
                if action == "retry-material":
                    raise ResultAPIError("material_not_retryable", "当前问题不能重试物料") from exc
                raise ResultAPIError("issue_not_ready", "当前问题不支持此恢复操作") from exc
            return {"ok": True, **self.recovery_attempt_dto(attempt)}
        raise ResultAPIError("route_not_found", "API 路由不存在", status=404)

    def group_recheck(self, group_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _strict(payload, {"request_id", "issue_revisions"}, {"request_id", "issue_revisions"})
        request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
        revisions = payload["issue_revisions"]
        if not isinstance(revisions, dict) or not revisions:
            raise ResultAPIError("validation_failed", "issue_revisions 必须是非空对象", status=422)
        issues = [item for item in self.repository.list_issues(active_only=True) if item.issue_group_key == group_key]
        if not issues:
            raise ResultAPIError("issue_group_not_found", "问题组不存在", status=404)
        expected_ids = {item.issue_id for item in issues}
        if set(revisions) != expected_ids or any(not isinstance(value, int) or isinstance(value, bool) for value in revisions.values()):
            raise ResultAPIError("validation_failed", "issue_revisions 必须覆盖当前问题组", status=422)
        operation = {"group_key": group_key, "issue_revisions": revisions}
        scope = f"issue-group.recheck:{group_key}"
        if self._idempotent(scope, request_id, operation, group_key):
            current = [item for item in self.repository.list_issues(active_only=True) if item.issue_group_key == group_key]
            return {"ok": True, "group_key": group_key, "issues": [self.issue_dto(item) for item in current], "reused": True}
        for issue in issues:
            if issue.issue_revision != revisions[issue.issue_id]:
                raise ResultAPIError("revision_conflict", "问题组已更新")
        updated = [
            recheck_issue(
                self.repository,
                issue.issue_id,
                expected_issue_revision=issue.issue_revision,
                settings=self.settings,
            )
            for issue in issues
        ]
        self._save_idempotency(scope, request_id, operation, "issue_group", group_key)
        return {"ok": True, "group_key": group_key, "issues": [self.issue_dto(item) for item in updated], "reused": False}

    @staticmethod
    def recovery_attempt_dto(attempt: Any) -> dict[str, Any]:
        return {
            "run_id": attempt.run_id,
            "recovery_attempt_id": attempt.attempt_id,
            "queued_at": attempt.accepted_at,
            "queue_position": None,
            "reused_stages": attempt.reuse_stages,
            "rerun_stages": attempt.redo_stages,
        }

    def create_file_selection(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_desktop()
        _strict(payload, {"issue_id", "kind", "selected_path"}, {"issue_id", "kind", "selected_path"})
        issue_id = _clean_string(payload["issue_id"], "issue_id", maximum=128)
        issue = self._issue(issue_id)
        kind = _clean_string(payload["kind"], "kind", maximum=32)
        required_action = "select_source" if kind == "source" else "select_recovery_output"
        if required_action not in self.issue_actions(issue):
            raise ResultAPIError("issue_not_ready", "当前问题不接受此类文件选择")
        selected_path = _clean_string(payload["selected_path"], "selected_path", maximum=4096)
        token = self.grants.issue(issue_id=issue_id, kind=kind, selected_path=selected_path)
        return {"ok": True, "selection_token": token, "expires_in_seconds": self.grants.ttl_seconds}

    def repair_context(self, resource_id: str, query: Mapping[str, list[str]]) -> dict[str, Any]:
        issue_id = (query.get("issue_id") or [""])[0]
        if not issue_id:
            raise ResultAPIError("validation_failed", "issue_id 必填", status=422, fields={"issue_id": "必填字段"})
        issue = self._issue(issue_id)
        self._require_issue_resource(issue, resource_id)
        try:
            context = resource_repair_context(self.settings, resource_id, issue_id=issue_id)
        except KeyError as exc:
            raise ResultAPIError("resource_not_repairable", "资源不支持修复", status=404) from exc
        return {"ok": True, "repair_context": context}

    def update_connection(self, resource_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _strict(payload, {"request_id", "issue_id", "api_base", "api_key"}, {"request_id", "issue_id", "api_base"})
        request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
        issue_id = _clean_string(payload["issue_id"], "issue_id", maximum=128)
        issue = self._issue(issue_id)
        self._require_issue_resource(issue, resource_id)
        resource = resource_map(self.settings).get(resource_id)
        if resource is None or resource.resource_type != "analysis":
            raise ResultAPIError("resource_not_repairable", "该资源只能从全局设置修复")
        api_base = self._validate_api_base(payload["api_base"])
        api_key_present = "api_key" in payload and bool(str(payload.get("api_key") or "").strip())
        operation = {
            "resource_id": resource_id,
            "issue_id": issue_id,
            "api_base": api_base,
            "credential_supplied": api_key_present,
        }
        scope = f"resource.connection:{resource_id}:{issue_id}"
        if self._idempotent(scope, request_id, operation, resource_id):
            return {"ok": True, "resource_id": resource_id, "api_base": api_base, "credential_updated": api_key_present, "reused": True}
        api_key = str(payload.get("api_key") or "")
        if api_key_present:
            normalized_key = api_key.strip()
            if any(character in normalized_key for character in ("\r", "\n", "\0")):
                raise ResultAPIError("validation_failed", "API key 内容无效", status=422, fields={"api_key": "包含控制字符"})
        saved = config_editor.save_llm_api_base(api_base, config_path=self.config_path)
        if not saved.get("ok"):
            raise ResultAPIError("validation_failed", "连接地址未通过配置校验", status=422)
        if api_key_present:
            api_key_env = self.settings.llm.api_key_env if self.settings.llm else "CHEAP_MODEL_API_KEY"
            key_result = onboarding.save_llm_api_key(
                api_key,
                api_key_env=api_key_env,
                env_path=self.config_path.parent / ".env",
            )
            if not key_result.get("ok"):
                raise ResultAPIError("validation_failed", "API key 保存失败", status=422, fields={"api_key": "内容无效"})
        self._save_idempotency(scope, request_id, operation, "resource", resource_id)
        return {
            "ok": True,
            "resource_id": resource_id,
            "api_base": api_base,
            "model": resource.version,
            "credential_updated": api_key_present,
            "reused": False,
        }

    def connection_test(self, resource_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _strict(payload, {"request_id", "issue_id"}, {"request_id", "issue_id"})
        request_id = _clean_string(payload["request_id"], "request_id", maximum=128)
        issue_id = _clean_string(payload["issue_id"], "issue_id", maximum=128)
        issue = self._issue(issue_id)
        self._require_issue_resource(issue, resource_id)
        resource = resource_map(self.settings).get(resource_id)
        if resource is None or resource.resource_type != "analysis":
            raise ResultAPIError("resource_not_repairable", "该资源不支持连接测试")
        operation = {"resource_id": resource_id, "issue_id": issue_id}
        scope = f"resource.connection-test:{resource_id}:{issue_id}"
        existing = self.repository.get_idempotency_key(scope, request_id)
        if existing is not None:
            if existing["request_hash"] != _request_hash(operation) or existing["object_id"] != resource_id:
                raise RequestConflictError("request_id_conflict")
            if existing["object_type"] == "resource_test_failed":
                raise ResultAPIError("connection_test_failed", "AI 连接测试失败，请检查地址、凭据和网络")
            return {"ok": True, "resource_id": resource_id, "success": True, "reused": True}
        result = onboarding.test_llm(
            str(self.settings.cheap_model_api_base or ""),
            str(self.settings.cheap_model_api_key or ""),
            str(self.settings.cheap_model_name or ""),
        )
        tested_at = datetime.now(UTC).isoformat()
        if not result.get("ok"):
            self._save_idempotency(scope, request_id, operation, "resource_test_failed", resource_id)
            raise ResultAPIError(
                "connection_test_failed",
                "AI 连接测试失败，请检查地址、凭据和网络",
                current={"resource_id": resource_id, "success": False, "tested_at": tested_at, "failure_code": result.get("error_code")},
            )
        self._save_idempotency(scope, request_id, operation, "resource_test_success", resource_id)
        return {"ok": True, "resource_id": resource_id, "success": True, "tested_at": tested_at, "reused": False}

    @staticmethod
    def _validate_api_base(value: Any) -> str:
        api_base = _clean_string(value, "api_base", maximum=2048).rstrip("/")
        parsed = urlsplit(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ResultAPIError("validation_failed", "api_base 必须是无内嵌凭据的 HTTP(S) 地址", status=422, fields={"api_base": "URL 无效"})
        return api_base

    def _require_issue_resource(self, issue: Any, resource_id: str) -> None:
        if issue.issue_code not in {"ai_resource_unavailable", "asr_resource_unavailable"}:
            raise ResultAPIError("resource_not_repairable", "当前问题不是资源连接问题")
        run = self.repository.get_run(str(issue.run_id)) if issue.run_id else None
        references = run.parameter_snapshot.get("resources", {}) if run else {}
        expected = references.get("review_ref") if issue.issue_code == "ai_resource_unavailable" else references.get("asr_ref")
        if str(expected or issue.root_cause_ref or "") != resource_id:
            raise ResultAPIError("resource_not_repairable", "资源与当前问题不匹配")

    def desktop_output_path(self, output_id: str) -> dict[str, Any]:
        self._require_desktop()
        return {"ok": True, "output_id": output_id, "path": str(self.resolve_output_path(output_id))}

    def _require_desktop(self) -> None:
        if self.auth_context != "bearer":
            raise ResultAPIError("desktop_auth_required", "该接口仅供桌面主进程访问", status=403)

    def legacy_awaiting_review(self) -> dict[str, Any]:
        items = []
        for run in self.repository.list_runs():
            if run.status != "awaiting_review":
                continue
            project = self.repository.get_project(run.project_id)
            items.append({
                "run": {
                    "run_id": run.run_id,
                    "project_id": run.project_id,
                    "source_name": Path(run.latest_seen_path).name,
                    "status": run.status,
                    "current_stage": run.current_stage,
                    "queued_at": run.queued_at,
                    "updated_at": run.updated_at,
                },
                "project": {"project_id": run.project_id, "name": project.name if project else ""},
                "detail_url": f"/projects/{run.project_id}/runs/{run.run_id}",
            })
        return {"ok": True, "runs": items, "count": len(items)}

    def unseen_result_count(self, project_id: str | None = None) -> int:
        return sum(
            item.result_type in _RESULT_LIST_TYPES
            for item in self.repository.list_run_results(project_id=project_id, unseen_only=True)
        )

    def _idempotent(self, scope: str, request_id: str, payload: Mapping[str, Any], object_id: str) -> bool:
        existing = self.repository.get_idempotency_key(scope, request_id)
        if existing is None:
            return False
        if existing["request_hash"] != _request_hash(payload) or existing["object_id"] != object_id:
            raise RequestConflictError("request_id_conflict")
        return True

    def _save_idempotency(
        self,
        scope: str,
        request_id: str,
        payload: Mapping[str, Any],
        object_type: str,
        object_id: str,
    ) -> None:
        if not self.repository.save_idempotency_key(
            scope,
            request_id,
            request_hash=_request_hash(payload),
            object_type=object_type,
            object_id=object_id,
        ):
            self._idempotent(scope, request_id, payload, object_id)
