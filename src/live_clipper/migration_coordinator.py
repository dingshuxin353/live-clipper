from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .project_domain import default_project_config, legacy_id, normalize_utc, project_config_v2, stable_json
from .project_migration import (
    PLAN_VERSION,
    LegacyInspection,
    LegacySourceError,
    MigrationPlan,
    SourceManifestEntry,
    build_migration_plan,
    create_migration_backup,
    inspect_legacy_state,
    verify_migration_backup,
)
from .project_result_domain import RequestConflictError, RevisionConflictError
from .project_result_index import inspect_safe_migration_result
from .project_storage import MigrationSession, MigrationStateError, ProjectRepository, database_path

_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class MigrationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        fields: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.fields = dict(fields or {})
        super().__init__(code)


def _request_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(dict(value)).encode("utf-8")).hexdigest()


def _require_fields(body: Mapping[str, Any], allowed: set[str], required: set[str] = frozenset()) -> None:
    unknown = set(body) - allowed
    missing = required - set(body)
    if unknown or missing:
        fields = {key: "unsupported" for key in sorted(unknown)} | {key: "required" for key in sorted(missing)}
        raise MigrationError("validation_failed", "迁移请求字段不符合合同", status=422, fields=fields)


def _safe_failure_summary(_error: BaseException) -> str:
    return "迁移未提交，旧数据保持不变，可在确认后重试"


class MigrationCoordinator:
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="venus-migration")
    _registry_lock = threading.Lock()
    _locks: dict[str, threading.Lock] = {}
    _futures: dict[tuple[str, str], Future[None]] = {}

    def __init__(
        self,
        *,
        service_dir: str | Path,
        config_path: str | Path,
        env_path: str | Path,
        input_dir: str | Path,
        output_root: str | Path,
        settings_loader: Callable[[], Settings] | None = None,
    ) -> None:
        self.service_dir = Path(service_dir).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.env_path = Path(env_path).expanduser().resolve()
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.backup_root = self.service_dir.parent / "migration-backups"
        self.settings_loader = settings_loader or self._load_settings
        self.fault_injection: Callable[[str], None] | None = None
        key = str(self.service_dir)
        with self._registry_lock:
            self._lock = self._locks.setdefault(key, threading.Lock())

    def _load_settings(self) -> Settings:
        try:
            return load_settings(self.config_path)
        except Exception:
            return Settings()

    def _inspection(self) -> LegacyInspection:
        try:
            return inspect_legacy_state(self.service_dir, config_path=self.config_path)
        except LegacySourceError as exc:
            raise MigrationError(exc.code, "旧版数据无法安全读取", status=409) from exc

    @staticmethod
    def _plan_payload(plan: MigrationPlan) -> dict[str, Any]:
        raw = plan.to_dict()
        backup = dict(raw["backup_summary"])
        backup.pop("target_path", None)
        history_entries = raw["history_summary"]["entries"]
        public_history = {
            "counts": raw["history_summary"]["counts"],
            "quarantine_reason_codes": sorted(
                {
                    str(item["reason_code"])
                    for item in history_entries
                    if item.get("category") == "quarantined"
                }
            ),
        }
        return {
            "plan_version": raw["plan_version"],
            "source_fingerprint": raw["source_fingerprint"],
            "plan_hash": raw["plan_hash"],
            "project": raw["project_preview"],
            "resources": raw["resource_summary"],
            "discovery": raw["discovery_summary"],
            "history": public_history,
            "backup": backup,
            "readiness": raw["readiness_summary"],
            "required_choices": raw["requires_user_choices"],
            "warnings": [],
            "choices": raw["choices"],
        }

    @staticmethod
    def _session_payload(session: MigrationSession) -> dict[str, Any]:
        total = 0
        processed = 0
        if session.report:
            total = int(session.report.get("history_total", 0))
            processed = total if session.state.startswith("completed_") else 0
        return {
            "migration_id": session.migration_id,
            "state": session.state,
            "stage": session.stage,
            "revision": session.revision,
            "processed_history_count": processed if session.stage == "history" else None,
            "total_history_count": total if session.stage == "history" else None,
            "backup_status": session.backup_status,
            "failure": (
                {"code": session.failure_code, "summary": session.failure_summary}
                if session.failure_code
                else None
            ),
            "project_id": session.project_id,
            "started_at": session.started_at,
            "updated_at": session.updated_at,
        }

    def _read_session(self) -> MigrationSession | None:
        if not database_path(self.service_dir).is_file():
            return None
        with ProjectRepository(self.service_dir) as repository:
            sessions = repository.list_migration_sessions()
            return sessions[0] if sessions else None

    def snapshot(self) -> dict[str, Any]:
        session = self._read_session()
        report = ({**session.report, "acknowledged_at": session.acknowledged_at} if session and session.report else None)
        if session is not None:
            if session.state.startswith("completed_"):
                entry = "completed"
            elif session.state == "failed_rolled_back":
                entry = "failed"
            elif session.state == "diagnostic_required":
                entry = "diagnostic"
            else:
                entry = "executing"
            source = {
                "detected": True,
                "checked_at": session.updated_at,
                "display_summary": {"metadata_file_count": len(session.source_manifest)},
            }
            plan = None
            if session.state.startswith("completed_"):
                with ProjectRepository(self.service_dir) as repository:
                    consistent = (
                        repository.get_data_mode() == "projects"
                        and session.project_id is not None
                        and repository.get_project(session.project_id) is not None
                        and session.report is not None
                    )
                if not consistent:
                    entry = "diagnostic"
        else:
            inspection = self._inspection()
            plan_object = build_migration_plan(inspection, backup_root=self.backup_root)
            entry = "review" if not plan_object.requires_user_choices else "inspect"
            source = {
                "detected": True,
                "checked_at": normalize_utc(),
                "display_summary": {
                    "metadata_file_count": len(inspection.source_manifest),
                    "history_count": len(inspection.runs),
                },
            }
            plan = self._plan_payload(plan_object)
        payload = {
            "ok": True,
            "entry": entry,
            "source": source,
            "plan": plan,
            "session": self._session_payload(session) if session else None,
            "report": report,
        }
        assert "source_manifest" not in json.dumps(payload, ensure_ascii=False)
        return payload

    def inspect(self, body: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        body = dict(body or {})
        _require_fields(body, {"request_id"})
        inspection = self._inspection()
        plan = build_migration_plan(inspection, backup_root=self.backup_root)
        return 200, {
            "ok": True,
            "source": {
                "detected": True,
                "checked_at": normalize_utc(),
                "display_summary": {
                    "metadata_file_count": len(inspection.source_manifest),
                    "history_count": len(inspection.runs),
                },
            },
            "plan": self._plan_payload(plan),
        }

    def validate(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(body, {"source_fingerprint", "plan_hash", "choices"}, {"source_fingerprint", "plan_hash", "choices"})
        if not isinstance(body["choices"], Mapping):
            raise MigrationError("validation_failed", "choices 必须是对象", status=422)
        inspection = self._inspection()
        if body["source_fingerprint"] != inspection.source_fingerprint:
            raise MigrationError("migration_source_changed", "旧版数据已变化，请重新检查", status=409)
        baseline = build_migration_plan(inspection, backup_root=self.backup_root)
        if body["plan_hash"] != baseline.plan_hash:
            raise MigrationError("migration_plan_changed", "迁移计划已变化，请重新检查", status=409)
        try:
            plan = build_migration_plan(inspection, choices=body["choices"], backup_root=self.backup_root)
        except ValueError as exc:
            raise MigrationError("validation_failed", "迁移选择不符合合同", status=422) from exc
        return 200, {"ok": True, "plan": self._plan_payload(plan)}

    def _validated_execution(self, body: Mapping[str, Any]) -> tuple[LegacyInspection, MigrationPlan, str]:
        _require_fields(
            body,
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
        )
        request_id = str(body["request_id"])
        if not _PUBLIC_ID.fullmatch(request_id):
            raise MigrationError("validation_failed", "request_id 不符合合同", status=422)
        if not isinstance(body["choices"], Mapping):
            raise MigrationError("validation_failed", "choices 必须是对象", status=422)
        inspection = self._inspection()
        if body["source_fingerprint"] != inspection.source_fingerprint:
            raise MigrationError("migration_source_changed", "旧版数据已变化，请重新检查", status=409)
        try:
            plan = build_migration_plan(inspection, choices=body["choices"], backup_root=self.backup_root)
        except ValueError as exc:
            raise MigrationError("validation_failed", "迁移选择不符合合同", status=422) from exc
        if body["plan_hash"] != plan.plan_hash:
            raise MigrationError("migration_plan_changed", "迁移计划已变化，请重新校验", status=409)
        if plan.backup_summary["space_status"] != "ready":
            raise MigrationError("migration_space_insufficient", "迁移备份空间不足", status=409)
        if plan.requires_user_choices:
            raise MigrationError(
                "migration_choices_required",
                "迁移仍有必须确认的选择",
                status=422,
                fields={str(item): "required" for item in plan.requires_user_choices},
            )
        return inspection, plan, request_id

    def execute(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(
            body,
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
        )
        request_id_probe = str(body.get("request_id") or "")
        if database_path(self.service_dir).is_file():
            with ProjectRepository(self.service_dir) as repository:
                existing = repository.get_migration_session_by_request(request_id_probe)
                sessions = repository.list_migration_sessions()
            durable = existing or (sessions[0] if sessions else None)
            if durable is not None:
                same = (
                    body.get("source_fingerprint") == durable.source_fingerprint
                    and body.get("plan_hash") == durable.plan_hash
                    and isinstance(body.get("choices"), Mapping)
                    and dict(body["choices"]) == durable.choices
                )
                if same:
                    return 202, {"ok": True, "session": self._session_payload(durable)}
                if existing is not None:
                    raise MigrationError("request_id_conflict", "同一 request_id 的请求内容不一致", status=409)
                raise MigrationError("migration_conflict", "已有不同的迁移事实", status=409)
        inspection, plan, request_id = self._validated_execution(body)
        canonical = {
            "source_fingerprint": plan.source_fingerprint,
            "plan_hash": plan.plan_hash,
            "choices": dict(plan.choices),
        }
        request_hash = _request_hash(canonical)
        migration_id = legacy_id(plan.source_fingerprint, "migration")
        with self._lock:
            repository = ProjectRepository(self.service_dir)
            try:
                existing_request = repository.get_migration_session_by_request(request_id)
                if existing_request is not None and existing_request.request_hash != request_hash:
                    raise MigrationError("request_id_conflict", "同一 request_id 的请求内容不一致", status=409)
                try:
                    session = repository.create_migration_session(
                        migration_id=migration_id,
                        source_fingerprint=plan.source_fingerprint,
                        plan_version=plan.plan_version,
                        plan_hash=plan.plan_hash,
                        source_manifest=[item.to_dict() for item in inspection.source_manifest],
                        choices=dict(plan.choices),
                        request_id=request_id,
                        request_hash=request_hash,
                        backup_path=str(self.backup_root / migration_id),
                    )
                except RequestConflictError as exc:
                    raise MigrationError("request_id_conflict", "同一 request_id 的请求内容不一致", status=409) from exc
                except MigrationStateError as exc:
                    raise MigrationError("migration_conflict", "已有其他迁移事实", status=409) from exc
            finally:
                repository.close()
            key = (str(self.service_dir), session.migration_id)
            future = self._futures.get(key)
            if session.state == "backing_up" and (future is None or future.done()):
                self._futures[key] = self._executor.submit(self._run, inspection, plan, session.migration_id)
        return 202, {"ok": True, "session": self._session_payload(session)}

    def retry(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(body, {"request_id", "migration_id", "expected_revision"}, {"request_id", "migration_id", "expected_revision"})
        request_id = str(body["request_id"])
        migration_id = str(body["migration_id"])
        if not _PUBLIC_ID.fullmatch(request_id) or not _PUBLIC_ID.fullmatch(migration_id):
            raise MigrationError("validation_failed", "迁移身份不符合合同", status=422)
        expected_revision = body["expected_revision"]
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise MigrationError("validation_failed", "expected_revision 不符合合同", status=422)
        with self._lock, ProjectRepository(self.service_dir) as repository:
            current = repository.get_migration_session(migration_id)
            if current is None:
                raise MigrationError("migration_not_found", "迁移不存在", status=404)
            if current.state != "failed_rolled_back":
                return 202, {"ok": True, "session": self._session_payload(current)}
            try:
                retrying = repository.update_migration_stage(
                    migration_id,
                    expected_revision,
                    state="backing_up",
                    stage="copy",
                    backup_status="pending",
                )
            except RevisionConflictError as exc:
                raise MigrationError("revision_conflict", "迁移状态已变化", status=409) from exc
            inspection = self._inspection()
            if inspection.source_fingerprint != current.source_fingerprint:
                repository.record_migration_failure(
                    migration_id,
                    retrying.revision,
                    failure_code="migration_source_changed",
                    failure_summary="旧版数据已变化，请重新检查",
                    backup_status=current.backup_status,
                )
                raise MigrationError("migration_source_changed", "旧版数据已变化，请重新检查", status=409)
            plan = build_migration_plan(inspection, choices=current.choices, backup_root=self.backup_root)
            if plan.plan_hash != current.plan_hash:
                raise MigrationError("migration_plan_changed", "迁移计划已变化", status=409)
            key = (str(self.service_dir), migration_id)
            self._futures[key] = self._executor.submit(self._run, inspection, plan, migration_id)
        return 202, {"ok": True, "session": self._session_payload(retrying)}

    def acknowledge(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(body, {"request_id", "migration_id", "expected_revision"}, {"request_id", "migration_id", "expected_revision"})
        with ProjectRepository(self.service_dir) as repository:
            current = repository.get_migration_session(str(body["migration_id"]))
            if current is not None and current.acknowledged_at is not None:
                return 200, {
                    "ok": True,
                    "session": self._session_payload(current),
                    "project_id": current.project_id,
                }
            try:
                session = repository.acknowledge_migration_session(
                    str(body["migration_id"]), int(body["expected_revision"])
                )
            except (MigrationStateError, RevisionConflictError, ValueError) as exc:
                raise MigrationError("revision_conflict", "迁移状态已变化或不可确认", status=409) from exc
        return 200, {"ok": True, "session": self._session_payload(session), "project_id": session.project_id}

    def backup_action(self, migration_id: str, *, auth_context: str) -> tuple[int, dict[str, Any]]:
        if auth_context != "bearer":
            raise MigrationError("bearer_required", "备份动作仅允许桌面主进程访问", status=403)
        with ProjectRepository(self.service_dir) as repository:
            session = repository.get_migration_session(migration_id)
        if session is None or not session.state.startswith("completed_") or not session.backup_path:
            raise MigrationError("backup_not_available", "迁移备份不可用", status=404)
        target = Path(session.backup_path).resolve(strict=False)
        try:
            target.relative_to(self.backup_root.resolve(strict=False))
        except ValueError as exc:
            raise MigrationError("diagnostic_required", "迁移备份记录不安全", status=409) from exc
        if target != self.backup_root / migration_id or not target.is_dir():
            raise MigrationError("backup_not_available", "迁移备份不可用", status=404)
        opener = shutil.which("open")
        if opener is None:
            raise MigrationError("backup_action_unavailable", "当前系统无法显示迁移备份", status=409)
        try:
            result = subprocess.run(
                [opener, "-R", str(target)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MigrationError("backup_action_failed", "迁移备份显示失败", status=500) from exc
        if result.returncode != 0:
            raise MigrationError("backup_action_failed", "迁移备份显示失败", status=500)
        return 200, {"ok": True, "action": "reveal_backup", "migration_id": migration_id}

    def _config(self, plan: MigrationPlan) -> dict[str, Any]:
        config = default_project_config(
            str(plan.project_preview["source_directory"]),
            str(plan.project_preview["output_directory"]),
        )
        schedule = config["schedule"]
        schedule["timezone"] = str(plan.project_preview["timezone"])
        schedule["enabled"] = plan.project_preview["trigger_mode"] == "scheduled"
        if schedule["enabled"]:
            schedule["mode"] = str(plan.project_preview["schedule_mode"])
            schedule["daily_time"] = plan.project_preview["daily_time"]
            schedule["interval_minutes"] = plan.project_preview["interval_minutes"]
        return project_config_v2(config)

    def _safe_results(self, plan: MigrationPlan, project_id: str) -> tuple[list[dict[str, Any]], list[Path]]:
        safe: list[dict[str, Any]] = []
        created_roots: list[Path] = []
        work_root = self.service_dir.parent
        for entry in plan.history_summary["entries"]:
            registered = entry.get("safe_result")
            if not isinstance(registered, Mapping):
                continue
            fact = inspect_safe_migration_result(
                str(registered["path_identity"]),
                output_root=str(plan.project_preview["output_directory"]),
                expected_sha256=str(registered["sha256"]),
            )
            if fact is None:
                continue
            legacy_run_id = str(entry["legacy_run_id"])
            run_id = legacy_id(plan.source_fingerprint, f"run:{legacy_run_id}")
            output_id = legacy_id(plan.source_fingerprint, f"output:{legacy_run_id}")
            evidence_root = work_root / "projects" / project_id / "runs" / run_id / "outputs" / output_id
            evidence_root.mkdir(parents=True, exist_ok=True)
            created_roots.append(evidence_root)
            evidence = {
                "format_version": 1,
                "output_id": output_id,
                "sha256": fact["sha256"],
                "media_metadata": {
                    key: fact[key]
                    for key in ("duration_ms", "width", "height", "container", "video_codec", "byte_size")
                },
            }
            path = evidence_root / "media_integrity.json"
            temporary = path.with_suffix(".tmp")
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(stable_json(evidence))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(evidence_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            safe.append({"legacy_run_id": legacy_run_id, **fact})
        return safe, created_roots

    def _remove_evidence(self, roots: list[Path]) -> None:
        boundary = self.service_dir.parent / "projects"
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
            parent = root.parent
            while parent != boundary and parent != parent.parent:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _run(self, inspection: LegacyInspection, plan: MigrationPlan, migration_id: str) -> None:
        repository: ProjectRepository | None = None
        created_evidence: list[Path] = []
        committed = False
        try:
            current_inspection = self._inspection()
            if current_inspection.source_fingerprint != inspection.source_fingerprint:
                raise MigrationError("migration_source_changed", "旧版数据已变化")
            backup = create_migration_backup(
                current_inspection,
                backup_root=self.backup_root,
                migration_id=migration_id,
            )
            repository = ProjectRepository(self.service_dir)
            current = repository.get_migration_session(migration_id)
            if current is None:
                raise MigrationStateError("migration session disappeared")
            backed_up = repository.update_migration_stage(
                migration_id,
                current.revision,
                state="backing_up",
                stage="copy",
                backup_status="completed",
                backup_path=str(backup.path),
            )
            after_backup = self._inspection()
            if after_backup.source_fingerprint != inspection.source_fingerprint:
                raise MigrationError("migration_source_changed", "备份后旧版数据发生变化")
            migrating = repository.update_migration_stage(
                migration_id,
                backed_up.revision,
                state="migrating",
                stage="project",
            )
            history = repository.update_migration_stage(
                migration_id, migrating.revision, state="migrating", stage="history"
            )
            validating = repository.update_migration_stage(
                migration_id, history.revision, state="validating", stage="database"
            )
            project_id = legacy_id(plan.source_fingerprint, "project:default")
            safe_results, created_evidence = self._safe_results(plan, project_id)
            counts = dict(plan.history_summary["counts"])
            blockers = [
                str(code)
                for code in plan.readiness_summary["resource_problems"]
                if code != "backup_space"
            ]
            report = {
                "plan_version": PLAN_VERSION,
                "plan_hash": plan.plan_hash,
                "project": {"project_id": project_id, "name": plan.project_preview["name"]},
                "discovery": dict(plan.discovery_summary),
                "imported": int(counts.get("importable", 0)),
                "compatibility": int(counts.get("compatibility", 0)),
                "quarantined": int(counts.get("quarantined", 0)),
                "safe_results": len(safe_results),
                "history_total": len(plan.history_summary["entries"]),
                "quarantine_reason_codes": sorted(
                    {
                        str(item["reason_code"])
                        for item in plan.history_summary["entries"]
                        if item.get("category") == "quarantined"
                    }
                ),
                "backup_created": True,
                "readiness": "attention" if blockers else "ready",
                "completed_at": normalize_utc(),
                "acknowledged_at": None,
            }
            repository.apply_migration_transaction(
                migration_id,
                validating.revision,
                source_fingerprint=plan.source_fingerprint,
                plan_hash=plan.plan_hash,
                project_id=project_id,
                project_name=str(plan.project_preview["name"]),
                config=self._config(plan),
                history_entries=plan.history_summary["entries"],
                safe_results=safe_results,
                blocker_codes=blockers,
                report=report,
                fault_injection=self.fault_injection,
            )
            committed = True
            from . import service

            completed = repository.get_migration_session(migration_id)
            if completed is not None and completed.state == "completed_ready" and completed.project_id:
                try:
                    readiness = service.ensure_service_ready(
                        self.settings_loader,
                        service_dir=self.service_dir,
                        project_id=completed.project_id,
                    )
                except Exception:  # noqa: BLE001 - the committed migration becomes attention, never rollback.
                    readiness = {"ok": False, "error_code": "service_not_ready"}
                if not readiness.get("ok"):
                    repository.mark_completed_migration_attention(
                        migration_id,
                        completed.revision,
                        failure_code=str(readiness.get("error_code") or "service_not_ready"),
                    )
        except Exception as exc:  # noqa: BLE001 - background ownership must become a durable outcome.
            if not committed:
                self._remove_evidence(created_evidence)
            try:
                if repository is None:
                    repository = ProjectRepository(self.service_dir)
                current = repository.get_migration_session(migration_id)
                if current is not None and not current.state.startswith("completed_") and current.state != "failed_rolled_back":
                    backup_status = "completed" if current.backup_status == "completed" else "failed"
                    code = exc.code if isinstance(exc, MigrationError) else "migration_apply_failed"
                    repository.record_migration_failure(
                        migration_id,
                        current.revision,
                        failure_code=code,
                        failure_summary=_safe_failure_summary(exc),
                        backup_status=backup_status,
                    )
            finally:
                if repository is not None:
                    repository.close()
            return
        finally:
            if repository is not None:
                repository.close()

    def dispatch(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        auth_context: str = "browser",
    ) -> tuple[int, dict[str, Any]] | None:
        parts = [part for part in path.split("?")[0].split("/") if part]
        if parts[:2] != ["api", "migration"]:
            return None
        payload = dict(body or {})
        if method == "GET" and parts == ["api", "migration"]:
            return 200, self.snapshot()
        if method == "GET" and len(parts) == 4 and parts[3] == "backup-action":
            return self.backup_action(parts[2], auth_context=auth_context)
        routes = {
            ("POST", "inspect"): self.inspect,
            ("POST", "validate"): self.validate,
            ("POST", "execute"): self.execute,
            ("POST", "retry"): self.retry,
            ("POST", "acknowledge"): self.acknowledge,
        }
        if len(parts) == 3:
            handler = routes.get((method, parts[2]))
            if handler is not None:
                return handler(payload)
        return None

    def recover_interrupted(self) -> MigrationSession | None:
        """Fail closed after process restart; never resumes writes without retry."""
        session = self._read_session()
        if session is None or session.state in {
            "completed_ready",
            "completed_attention",
            "failed_rolled_back",
            "diagnostic_required",
        }:
            return session
        key = (str(self.service_dir), session.migration_id)
        future = self._futures.get(key)
        if future is not None and not future.done():
            return session
        backup_status = "failed"
        if session.backup_path:
            try:
                manifest = tuple(
                    SourceManifestEntry(
                        logical_type=str(item["logical_type"]),
                        source_identity=str(item["source_identity"]),
                        size=int(item["size"]),
                        mtime_ns=int(item["mtime_ns"]),
                        sha256=str(item["sha256"]),
                    )
                    for item in session.source_manifest
                )
                verify_migration_backup(
                    session.backup_path,
                    migration_id=session.migration_id,
                    source_fingerprint=session.source_fingerprint,
                    source_manifest=manifest,
                )
                backup_status = "completed"
            except (LegacySourceError, KeyError, TypeError, ValueError):
                backup_status = "failed"
        with ProjectRepository(self.service_dir) as repository:
            return repository.record_migration_failure(
                session.migration_id,
                session.revision,
                failure_code="migration_interrupted",
                failure_summary="迁移进程已中断，旧数据保持不变，可确认后重试",
                backup_status=backup_status,
            )


def migration_summary_for_startup(
    *, service_dir: str | Path, config_path: str | Path, input_dir: str | Path, output_root: str | Path
) -> dict[str, Any] | None:
    """Return a DTO-safe M2 summary for the startup envelope."""
    try:
        snapshot = MigrationCoordinator(
            service_dir=service_dir,
            config_path=config_path,
            env_path=Path(config_path).parent / ".env",
            input_dir=input_dir,
            output_root=output_root,
        ).snapshot()
    except MigrationError:
        return {"entry": "diagnostic", "session": None, "report": None}
    return {"entry": snapshot["entry"], "session": snapshot["session"], "report": snapshot["report"]}
