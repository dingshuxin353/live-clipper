from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .first_run_detection import StartupInspection, _read_database_facts, readonly_database
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

_HISTORY_REASON_LABELS = {
    "content_identity_missing": "缺少识别录像所需的信息，无法导入项目。",
    "duplicate_content_identity": "录像标识重复，无法导入项目。",
    "source_identity_unsupported": "无法确认录像来源，不能导入项目。",
    "timestamp_untrusted": "记录的时间信息不完整或不符合要求，无法导入项目。",
    "state_unrecognized": "无法识别旧版处理状态，不能导入项目。",
}


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
        raise MigrationError("validation_failed", "无法读取本次操作的信息，请重新读取升级状态。", status=422, fields=fields)


def _safe_failure_summary(error: BaseException) -> str:
    if isinstance(error, MigrationError):
        return error.message
    return "升级未完成，本次升级未修改旧版数据。请查看问题提示后重试。"


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

    @contextmanager
    def _execution_lease(self):
        path = self.service_dir / '.migration-execution.lock'
        self.service_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'r+') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MigrationError('migration_running', '原来的升级仍在执行，请先查看升级状态。') from exc
            yield lease

    @staticmethod
    def _run_owned(owner, function, *args):
        with owner:
            function(*args)

    def _submit_owned(self, owner, function, *args):
        try:
            return self._executor.submit(self._run_owned, owner, function, *args)
        except BaseException:
            owner.close()
            raise

    def _load_settings(self) -> Settings:
        return load_settings(self.config_path, env_path=self.env_path)

    def _inspection(self) -> LegacyInspection:
        try:
            return inspect_legacy_state(self.service_dir, config_path=self.config_path)
        except LegacySourceError as exc:
            raise MigrationError(exc.code, "无法读取旧版数据，请记录问题编号以便排查。", status=409) from exc

    @staticmethod
    def _plan_payload(plan: MigrationPlan) -> dict[str, Any]:
        raw = plan.to_dict()
        backup = dict(raw["backup_summary"])
        backup.pop("target_path", None)
        history_entries = raw["history_summary"]["entries"]
        public_entries = []
        for position, item in enumerate(history_entries, start=1):
            category = str(item["category"])
            reason_code = str(item["reason_code"]) if item.get("reason_code") else None
            if reason_code is not None:
                reason_label = _HISTORY_REASON_LABELS.get(reason_code, "这条记录无法导入项目，将保留在升级备份中。")
            elif category == "compatibility":
                reason_label = "这条记录尚未处理完成，升级后不会自动继续处理。"
            elif item.get("safe_result"):
                reason_label = "记录可导入，关联成片将在升级时核验。"
            else:
                reason_label = "记录可导入"
            public_entries.append(
                {
                    "display_identity": f"历史记录 {position}",
                    "category": category,
                    "reason_code": reason_code,
                    "reason_label": reason_label,
                    "safe_result": bool(item.get("safe_result")),
                }
            )
        public_history = {
            "counts": raw["history_summary"]["counts"],
            "entries": public_entries,
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
        facts = _read_database_facts(database_path(self.service_dir))
        if facts.unreadable:
            raise MigrationError("diagnostic_required", "无法读取升级状态，请记录问题编号以便排查。")
        return facts.migration_record

    @staticmethod
    def _completion_consistent(data_mode: str, project_ids: tuple[str, ...], session: MigrationSession) -> bool:
        return bool(
            data_mode == "projects"
            and session.project_id in project_ids
            and session.report
            and session.report.get("project", {}).get("project_id") == session.project_id
            and session.report.get("backup_created") is True
            and session.backup_status == "completed"
            and session.backup_path
        )

    def snapshot(self, *, startup: StartupInspection | None = None) -> dict[str, Any]:
        facts = startup.facts if startup else _read_database_facts(database_path(self.service_dir))
        if facts.unreadable:
            raise MigrationError("diagnostic_required", "无法读取升级状态，请记录问题编号以便排查。")
        session = facts.migration_record
        report = ({**session.report, "acknowledged_at": session.acknowledged_at} if session and session.report else None)
        if session is not None:
            incomplete = facts.data_mode == "projects" and facts.resource_migration_state != "completed"
            if incomplete:
                entry = "executing" if session.state == "validating" and session.stage == "resources" else "incomplete"
                report = None
            elif session.state.startswith("completed_"):
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
                consistent = self._completion_consistent(facts.data_mode, facts.project_ids, session)
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
            raise MigrationError("validation_failed", "无法读取升级设置，请返回后重试。", status=422)
        inspection = self._inspection()
        if body["source_fingerprint"] != inspection.source_fingerprint:
            raise MigrationError("migration_source_changed", "旧版数据已变化，请重新检查。", status=409)
        baseline = build_migration_plan(inspection, backup_root=self.backup_root)
        if body["plan_hash"] != baseline.plan_hash:
            raise MigrationError("migration_plan_changed", "升级设置已变化，请重新检查。", status=409)
        try:
            plan = build_migration_plan(inspection, choices=body["choices"], backup_root=self.backup_root)
        except ValueError as exc:
            raise MigrationError("validation_failed", "升级设置有误，请检查后重试。", status=422) from exc
        return 200, {"ok": True, "plan": self._plan_payload(plan)}

    def _validated_execution(self, body: Mapping[str, Any]) -> tuple[LegacyInspection, MigrationPlan, str]:
        _require_fields(
            body,
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
            {"request_id", "source_fingerprint", "plan_hash", "choices"},
        )
        request_id = str(body["request_id"])
        if not _PUBLIC_ID.fullmatch(request_id):
            raise MigrationError("validation_failed", "无法确认本次操作，请返回后重新检查。", status=422)
        if not isinstance(body["choices"], Mapping):
            raise MigrationError("validation_failed", "无法读取升级设置，请返回后重试。", status=422)
        inspection = self._inspection()
        if body["source_fingerprint"] != inspection.source_fingerprint:
            raise MigrationError("migration_source_changed", "旧版数据已变化，请重新检查。", status=409)
        try:
            plan = build_migration_plan(inspection, choices=body["choices"], backup_root=self.backup_root)
        except ValueError as exc:
            raise MigrationError("validation_failed", "升级设置有误，请检查后重试。", status=422) from exc
        if body["plan_hash"] != plan.plan_hash:
            raise MigrationError("migration_plan_changed", "升级设置已变化，请重新检查。", status=409)
        if plan.backup_summary["space_status"] != "ready":
            raise MigrationError("migration_space_insufficient", "备份空间不足，请释放磁盘空间后重新检查。", status=409)
        if plan.requires_user_choices:
            raise MigrationError(
                "migration_choices_required",
                "还有设置需要补充，请返回修改。",
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
                    raise MigrationError("request_id_conflict", "当前设置与上次提交的不一致，请先确认上次升级的状态。", status=409)
                raise MigrationError("migration_conflict", "已有另一条升级记录，请先查看其状态。", status=409)
        inspection, plan, request_id = self._validated_execution(body)
        canonical = {
            "source_fingerprint": plan.source_fingerprint,
            "plan_hash": plan.plan_hash,
            "choices": dict(plan.choices),
        }
        request_hash = _request_hash(canonical)
        migration_id = legacy_id(plan.source_fingerprint, "migration")
        with self._lock, ExitStack() as execution:
            execution.enter_context(self._execution_lease())
            repository = ProjectRepository(self.service_dir)
            try:
                existing_request = repository.get_migration_session_by_request(request_id)
                if existing_request is not None and existing_request.request_hash != request_hash:
                    raise MigrationError("request_id_conflict", "当前设置与上次提交的不一致，请先确认上次升级的状态。", status=409)
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
                    raise MigrationError("request_id_conflict", "当前设置与上次提交的不一致，请先确认上次升级的状态。", status=409) from exc
                except MigrationStateError as exc:
                    raise MigrationError("migration_conflict", "已有另一条升级记录，请先查看其状态。", status=409) from exc
            finally:
                repository.close()
            key = (str(self.service_dir), session.migration_id)
            future = self._futures.get(key)
            if session.state == "backing_up" and (future is None or future.done()):
                self._futures[key] = self._submit_owned(execution.pop_all(), self._run, inspection, plan, session.migration_id)
        return 202, {"ok": True, "session": self._session_payload(session)}

    def retry(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(body, {"request_id", "migration_id", "expected_revision"}, {"request_id", "migration_id", "expected_revision"})
        request_id = str(body["request_id"])
        migration_id = str(body["migration_id"])
        if not _PUBLIC_ID.fullmatch(request_id) or not _PUBLIC_ID.fullmatch(migration_id):
            raise MigrationError("validation_failed", "无法识别升级记录，请重新读取升级状态。", status=422)
        expected_revision = body["expected_revision"]
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise MigrationError("validation_failed", "无法确认升级状态，请重新读取后重试。", status=422)
        with self._lock, ProjectRepository(self.service_dir) as repository, ExitStack() as execution:
            current = repository.get_migration_session(migration_id)
            if current is None:
                raise MigrationError("migration_not_found", "找不到这条升级记录，请重新读取升级状态。", status=404)
            scope = "migration.retry"
            digest = _request_hash({"migration_id": migration_id, "expected_revision": expected_revision})
            existing = repository.get_idempotency_key(scope, request_id)
            if existing:
                if existing["request_hash"] != digest or existing["object_id"] != migration_id:
                    raise MigrationError("request_id_conflict", "当前设置与上次提交的不一致，请先确认上次升级的状态。")
                return 202, {"ok": True, "session": self._session_payload(current)}
            resource_state = repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
            if current.project_id and (not resource_state or resource_state[0] != 'completed'):
                if current.revision != expected_revision:
                    raise MigrationError("revision_conflict", "升级状态已变化，请重新读取后重试。")
                key = (str(self.service_dir), migration_id)
                future = self._futures.get(key)
                if future is not None and not future.done():
                    return 202, {"ok": True, "session": self._session_payload(current)}
                execution.enter_context(self._execution_lease())
                latest = repository.get_migration_session(migration_id)
                if latest.revision != expected_revision:
                    raise MigrationError('revision_conflict', '升级状态已变化，请重新读取后重试。')
                try:
                    self._validate_resource_resume(repository, current)
                except MigrationError as exc:
                    self._record_unknown(repository, current, exc.code, exc.message)
                    raise
                with repository.transaction():
                    repository.connection.execute(
                        "UPDATE migration_sessions SET state='validating',stage='resources',acknowledged_at=NULL,"
                        "completed_at=NULL,failure_code=NULL,failure_summary=NULL,revision=revision+1,updated_at=? "
                        "WHERE migration_id=? AND revision=?", (normalize_utc(), migration_id, expected_revision),
                    )
                    repository.save_idempotency_key(scope, request_id, request_hash=digest, object_type='migration', object_id=migration_id)
                retrying = repository.get_migration_session(migration_id)
                try:
                    self._futures[key] = self._submit_owned(execution.pop_all(), self._resume_resources, migration_id)
                except Exception as exc:
                    self._record_unknown(repository, retrying, 'migration_resources_failed', '无法开始配置转换，已保存的数据已保留。请继续升级。')
                    raise MigrationError('migration_resources_failed', '无法开始配置转换，请重新读取状态。') from exc
                return 202, {"ok": True, "session": self._session_payload(retrying)}
            if current.state != "failed_rolled_back":
                return 202, {"ok": True, "session": self._session_payload(current)}
            if current.revision != expected_revision:
                raise MigrationError("revision_conflict", "升级状态已变化，请重新读取后重试。")
            execution.enter_context(self._execution_lease())
            if repository.get_migration_session(migration_id).revision != expected_revision:
                raise MigrationError('revision_conflict', '升级状态已变化，请重新读取后重试。')
            try:
                inspection = self._inspection()
                if inspection.source_fingerprint != current.source_fingerprint:
                    raise MigrationError("migration_source_changed", "旧版数据与本次升级记录不一致，无法直接继续升级。请记录问题编号并联系开发者排查。")
                plan = build_migration_plan(inspection, choices=current.choices, backup_root=self.backup_root)
                if plan.backup_summary["space_status"] != "ready":
                    raise MigrationError("migration_space_insufficient", "备份空间不足，请释放磁盘空间后重试升级。")
                if plan.plan_hash != current.plan_hash:
                    raise MigrationError("migration_plan_changed", "当前检查结果与本次升级记录不一致，无法直接继续升级。请记录问题编号并联系开发者排查。")
            except Exception as exc:
                error = exc if isinstance(exc, MigrationError) else MigrationError("migration_inspection_failed", "无法读取当前升级条件，请记录问题编号以便排查。")
                repository.record_migration_failure(migration_id, current.revision, failure_code=error.code, failure_summary=error.message, backup_status=current.backup_status)
                if error is exc:
                    raise
                raise error from exc
            with repository.transaction():
                retrying = repository.update_migration_stage(migration_id, expected_revision, state="backing_up", stage="copy", backup_status="pending")
                repository.save_idempotency_key(scope, request_id, request_hash=digest, object_type="migration", object_id=migration_id)
            key = (str(self.service_dir), migration_id)
            try:
                self._futures[key] = self._submit_owned(execution.pop_all(), self._run, inspection, plan, migration_id)
            except Exception as exc:
                repository.record_migration_failure(migration_id, retrying.revision, failure_code="migration_apply_failed", failure_summary=_safe_failure_summary(exc), backup_status=current.backup_status)
                raise MigrationError("migration_apply_failed", "无法开始升级，请重新读取升级状态。") from exc
        return 202, {"ok": True, "session": self._session_payload(retrying)}

    def _validate_resource_resume(self, repository: ProjectRepository, session: MigrationSession) -> Settings:
        if (repository.get_data_mode() != 'projects' or not session.project_id
                or not repository.get_project(session.project_id) or not session.report
                or session.report.get('project', {}).get('project_id') != session.project_id
                or session.backup_status != 'completed' or not session.backup_path):
            raise MigrationError('migration_completion_conflict', '无法确认已保存的升级结果，请保留数据并联系开发者排查。')
        inspection = self._inspection()
        if inspection.source_fingerprint != session.source_fingerprint:
            raise MigrationError('migration_source_changed', '旧版设置或记录已变化，无法安全继续原来的升级。')
        plan = build_migration_plan(inspection, choices=session.choices, backup_root=self.backup_root)
        if plan.plan_hash != session.plan_hash:
            raise MigrationError('migration_plan_changed', '升级设置已变化，无法安全继续原来的升级。')
        try:
            backup = self._authorized_backup_path(session.migration_id, session.backup_path)
            verify_migration_backup(backup, migration_id=session.migration_id,
                                    source_fingerprint=session.source_fingerprint,
                                    source_manifest=inspection.source_manifest)
        except (MigrationError, LegacySourceError, OSError, ValueError, TypeError) as exc:
            raise MigrationError('migration_backup_invalid', '无法核验原升级备份，请保留数据并联系开发者排查。') from exc
        for project in repository.list_projects():
            refs = repository.get_config_revision(project.project_id).config['resources']
            for value in refs.values():
                legacy = project.project_id == session.project_id and value in {'legacy.asr.default', 'legacy.analysis.default'}
                if value and value != 'reuse_analysis' and not legacy and not repository.connection.execute(
                    'SELECT 1 FROM resources WHERE resource_id=? AND deleted_at IS NULL', (value,),
                ).fetchone():
                    raise MigrationError('migration_resource_conflict', '项目使用的模型配置无法核验，请保留现有设置并联系开发者排查。')
        if self.env_path.exists() and not (self.service_dir / 'resource-migration-owned.jsonl').exists():
            raise MigrationError('migration_credential_source_unknown', '原升级没有记录这份凭据的来源，暂时不能自动绑定当前凭据。请保留数据并联系开发者排查。')
        return self.settings_loader()

    def _resume_resources(self, migration_id: str) -> None:
        from .resource_migration import prepare_resource_migration

        with ProjectRepository(self.service_dir) as repository:
            try:
                current = repository.get_migration_session(migration_id)
                if current is None or current.stage != 'resources':
                    raise MigrationStateError('resource recovery state changed')
                settings = self._validate_resource_resume(repository, current)
                with prepare_resource_migration(repository, settings, fault=self.fault_injection) as convert:
                    with repository.transaction():
                        convert()
                        now = normalize_utc()
                        blockers = sorted(set(current.report.get('blocker_codes', [])) | {'resource_validation_required'})
                        report = {**current.report, 'completed_at': now, 'acknowledged_at': None,
                                  'readiness': 'attention', 'blocker_codes': blockers, 'blocker_count': len(blockers)}
                        repository.connection.execute(
                            "UPDATE migration_sessions SET state='completed_attention',stage='complete',report_json=?,"
                            "completed_at=?,updated_at=?,failure_code=NULL,failure_summary=NULL,revision=revision+1 "
                            "WHERE migration_id=?", (stable_json(report), now, now, migration_id),
                        )
            except Exception as exc:
                with repository.transaction():
                    state = repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
                    if not state or state[0] != 'completed':
                        repository.connection.execute(
                            "UPDATE migration_sessions SET state='diagnostic_required',stage='resources',"
                            "failure_code=?,failure_summary=?,updated_at=?,revision=revision+1 "
                            "WHERE migration_id=?", (exc.code if isinstance(exc, MigrationError) else 'migration_resources_failed',
                            exc.message if isinstance(exc, MigrationError) else '模型配置转换尚未完成，已导入的项目和历史记录已保留。请检查原升级条件后继续。', normalize_utc(), migration_id),
                        )

    def acknowledge(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _require_fields(body, {"request_id", "migration_id", "expected_revision"}, {"request_id", "migration_id", "expected_revision"})
        with ProjectRepository(self.service_dir) as repository:
            resource_state = repository.connection.execute("SELECT value FROM system_state WHERE key='named_resources_migration'").fetchone()
            if not resource_state or resource_state[0] != 'completed':
                raise MigrationError("migration_pending", "模型配置升级尚未完成，请先继续升级。")
            current = repository.get_migration_session(str(body["migration_id"]))
            if current is not None and current.state.startswith("completed_") and not self._completion_consistent(repository.get_data_mode(), tuple(p.project_id for p in repository.list_projects()), current):
                raise MigrationError("diagnostic_required", "暂时无法确认升级结果，请记录问题编号并联系开发者排查。")
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
                raise MigrationError("revision_conflict", "升级状态已变化，暂时无法进入项目。请重新读取状态后重试。", status=409) from exc
        return 200, {"ok": True, "session": self._session_payload(session), "project_id": session.project_id}

    def history(self, migration_id: str) -> tuple[int, dict[str, Any]]:
        with readonly_database(database_path(self.service_dir)) as connection:
            repository = ProjectRepository(self.service_dir, connection=connection)
            session = repository.get_migration_session(migration_id)
            if session is None or not session.project_id or not repository.get_project(session.project_id):
                raise MigrationError('migration_not_found', '找不到已保存的升级项目。', status=404)
            return 200, {'ok': True, 'history': [
                {'run_id': run.run_id, 'status': run.status, 'created_at': run.queued_at}
                for run in repository.list_runs(session.project_id)
            ]}

    @staticmethod
    def _directory_identity(path: Path) -> tuple[int, int]:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409)
        if not stat.S_ISDIR(details.st_mode):
            raise MigrationError("backup_not_available", "暂时无法显示升级备份，请重新读取升级状态后重试。", status=404)
        return details.st_dev, details.st_ino

    def _authorized_backup_path(self, migration_id: str, recorded_path: str) -> Path:
        approved_root = self.backup_root
        expected = approved_root / migration_id
        recorded = Path(recorded_path)
        if not recorded.is_absolute() or recorded != expected:
            raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409)
        try:
            root_identity = self._directory_identity(approved_root)
            target_identity = self._directory_identity(expected)
            real_root = approved_root.resolve(strict=True)
            real_target = expected.resolve(strict=True)
        except FileNotFoundError as exc:
            raise MigrationError("backup_not_available", "暂时无法显示升级备份，请重新读取升级状态后重试。", status=404) from exc
        except OSError as exc:
            raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409) from exc
        if real_target != real_root / migration_id or real_target.parent != real_root:
            raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409)
        try:
            if self._directory_identity(approved_root) != root_identity:
                raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409)
            if self._directory_identity(expected) != target_identity:
                raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409)
        except FileNotFoundError as exc:
            raise MigrationError("backup_not_available", "暂时无法显示升级备份，请重新读取升级状态后重试。", status=404) from exc
        except OSError as exc:
            raise MigrationError("diagnostic_required", "无法确认备份位置，请记录问题编号以便排查。", status=409) from exc
        return real_target

    def backup_grant(self, migration_id: str, *, auth_context: str) -> tuple[int, dict[str, Any]]:
        if auth_context != "bearer":
            raise MigrationError("bearer_required", "请在 Venus 桌面应用中显示备份。", status=403)
        if not _PUBLIC_ID.fullmatch(migration_id):
            raise MigrationError("backup_not_available", "暂时无法显示升级备份，请重新读取升级状态后重试。", status=404)
        with ProjectRepository(self.service_dir) as repository:
            session = repository.get_migration_session(migration_id)
        if (
            session is None
            or session.backup_status != "completed"
            or not session.backup_path
        ):
            raise MigrationError("backup_not_available", "暂时无法显示升级备份，请重新读取升级状态后重试。", status=404)
        target = self._authorized_backup_path(migration_id, session.backup_path)
        return 200, {
            "ok": True,
            "grant": {
                "grant_version": 1,
                "kind": "migration_backup_reveal",
                "migration_id": migration_id,
                "backup_path": str(target),
            },
        }

    def _config(self, plan: MigrationPlan) -> dict[str, Any]:
        config = default_project_config(
            str(plan.project_preview["source_directory"]),
            str(plan.project_preview["output_directory"]),
        )
        config["resources"].update(asr_ref="legacy.asr.default", analysis_ref="legacy.analysis.default")
        schedule = config["schedule"]
        schedule["timezone"] = str(plan.project_preview["timezone"])
        schedule["enabled"] = plan.project_preview["trigger_mode"] == "scheduled"
        if schedule["enabled"]:
            schedule["mode"] = str(plan.project_preview["schedule_mode"])
            schedule["daily_time"] = plan.project_preview["daily_time"]
            schedule["interval_minutes"] = plan.project_preview["interval_minutes"]
        return project_config_v2(config)

    def _evidence_owner(self, migration_id: str) -> Path:
        return self.service_dir / f'{migration_id}.evidence.json'

    def _safe_results(self, plan: MigrationPlan, project_id: str) -> list[dict[str, Any]]:
        safe = []
        for entry in plan.history_summary['entries']:
            registered = entry.get('safe_result')
            if not isinstance(registered, Mapping):
                continue
            fact = inspect_safe_migration_result(str(registered['path_identity']),
                output_root=str(plan.project_preview['output_directory']), expected_sha256=str(registered['sha256']))
            if fact is not None:
                safe.append({'legacy_run_id': str(entry['legacy_run_id']), **fact})
        migration_id = legacy_id(plan.source_fingerprint, 'migration')
        owner = self._evidence_owner(migration_id)
        payload = {'source': plan.source_fingerprint, 'plan': plan.plan_hash,
                   'runs': [item['legacy_run_id'] for item in safe]}
        if owner.is_symlink():
            raise MigrationError('migration_evidence_conflict', '无法核验升级证据的归属，请保留数据并联系开发者。')
        if owner.exists():
            if json.loads(owner.read_text()) != payload:
                raise MigrationError('migration_evidence_conflict', '升级证据与原计划不一致，请保留数据并联系开发者。')
        else:
            for fact in safe:
                run_id = legacy_id(plan.source_fingerprint, f"run:{fact['legacy_run_id']}")
                output_id = legacy_id(plan.source_fingerprint, f"output:{fact['legacy_run_id']}")
                root = self.service_dir.parent / 'projects' / project_id / 'runs' / run_id / 'outputs' / output_id
                if root.exists() or root.is_symlink():
                    raise MigrationError('migration_evidence_conflict', '升级证据位置已有内容，无法确认归属。请保留数据并联系开发者。')
            descriptor = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(stable_json(payload))
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(self.service_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        for fact in safe:
            run_id = legacy_id(plan.source_fingerprint, f"run:{fact['legacy_run_id']}")
            output_id = legacy_id(plan.source_fingerprint, f"output:{fact['legacy_run_id']}")
            root = self.service_dir.parent / 'projects' / project_id / 'runs' / run_id / 'outputs' / output_id
            if any(path.is_symlink() for path in [root, *root.parents] if path != self.service_dir.parent):
                raise MigrationError('migration_evidence_conflict', '升级证据位置无法核验，请保留数据并联系开发者。')
            root.mkdir(parents=True, exist_ok=True)
            evidence = {'format_version': 1, 'output_id': output_id, 'sha256': fact['sha256'],
                        'media_metadata': {key: fact[key] for key in
                            ('duration_ms', 'width', 'height', 'container', 'video_codec', 'byte_size')}}
            path = root / 'media_integrity.json'
            content = stable_json(evidence)
            if path.exists():
                if path.is_symlink() or path.read_text() != content:
                    raise MigrationError('migration_evidence_conflict', '已保存的升级证据无法核验，请保留数据并联系开发者。')
                continue
            temporary = root / 'media_integrity.tmp'
            if temporary.is_symlink():
                raise MigrationError('migration_evidence_conflict', '升级证据位置无法核验，请保留数据并联系开发者。')
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return safe

    def _cleanup_evidence(self, repository: ProjectRepository, session: MigrationSession) -> None:
        owner = self._evidence_owner(session.migration_id)
        if not owner.exists():
            return
        if owner.is_symlink():
            raise ValueError('migration_evidence_conflict')
        payload = json.loads(owner.read_text())
        if payload['source'] != session.source_fingerprint or payload['plan'] != session.plan_hash:
            raise ValueError('migration_evidence_conflict')
        project_id = legacy_id(session.source_fingerprint, 'project:default')
        boundary = self.service_dir.parent / 'projects'
        for legacy_run in payload['runs']:
            run_id = legacy_id(session.source_fingerprint, f'run:{legacy_run}')
            output_id = legacy_id(session.source_fingerprint, f'output:{legacy_run}')
            if repository.connection.execute('SELECT 1 FROM run_outputs WHERE output_id=?', (output_id,)).fetchone():
                continue
            root = boundary / project_id / 'runs' / run_id / 'outputs' / output_id
            if any(path.is_symlink() for path in [root, *root.parents] if path != self.service_dir.parent):
                raise ValueError('migration_evidence_conflict')
            for name in ('media_integrity.json', 'media_integrity.tmp'):
                path = root / name
                if path.is_symlink():
                    raise ValueError('migration_evidence_conflict')
                path.unlink(missing_ok=True)
            parent = root
            while parent != boundary and parent != parent.parent:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        owner.unlink()

    def _run(self, inspection: LegacyInspection, plan: MigrationPlan, migration_id: str) -> None:
        repository: ProjectRepository | None = None
        try:
            current_inspection = self._inspection()
            if current_inspection.source_fingerprint != inspection.source_fingerprint:
                raise MigrationError("migration_source_changed", "旧版数据与本次升级记录不一致，无法直接继续升级。请记录问题编号并联系开发者排查。")
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
                raise MigrationError("migration_source_changed", "备份后检测到旧版数据与本次升级记录不一致，无法直接继续升级。请记录问题编号并联系开发者排查。")
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
                migration_id, history.revision, state="validating", stage="resources"
            )
            project_id = legacy_id(plan.source_fingerprint, "project:default")
            safe_results = self._safe_results(plan, project_id)
            counts = dict(plan.history_summary["counts"])
            blockers = [
                str(code)
                for code in plan.readiness_summary["resource_problems"]
                if code != "backup_space"
            ]
            # Migrated settings have no structured capability evidence for the new resource contract.
            blockers.append('resource_validation_required')
            report = {
                "plan_version": PLAN_VERSION,
                "plan_hash": plan.plan_hash,
                "project": {"project_id": project_id, "name": plan.project_preview["name"]},
                "discovery": {
                    **dict(plan.discovery_summary),
                    "timezone": plan.project_preview["timezone"],
                    "trigger_mode": plan.project_preview["trigger_mode"],
                    "schedule_mode": plan.project_preview["schedule_mode"],
                    "daily_time": plan.project_preview["daily_time"],
                    "interval_minutes": plan.project_preview["interval_minutes"],
                },
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
                "blocker_count": len(blockers),
                "blocker_codes": sorted(blockers),
                "completed_at": normalize_utc(),
                "acknowledged_at": None,
            }
            from .resource_migration import prepare_resource_migration

            with prepare_resource_migration(repository, self.settings_loader(), fault=self.fault_injection) as convert:
                with repository.transaction():
                    repository.connection.execute("UPDATE system_state SET value='migrating' WHERE key='named_resources_migration'")
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
                    convert()
                    completed_at = normalize_utc()
                    report["completed_at"] = completed_at
                    repository.connection.execute(
                        "UPDATE migration_sessions SET report_json=?,completed_at=?,updated_at=? WHERE migration_id=?",
                        (stable_json(report), completed_at, completed_at, migration_id),
                    )
            self._cleanup_evidence(repository, repository.get_migration_session(migration_id))
        except Exception as exc:  # noqa: BLE001 - background ownership must become a durable outcome.
            try:
                if repository is None:
                    repository = ProjectRepository(self.service_dir)
                current = repository.get_migration_session(migration_id)
                if current is not None and not current.state.startswith("completed_") and current.state != "failed_rolled_back":
                    if repository.get_data_mode() != 'legacy' or repository.list_projects():
                        self._record_unknown(repository, current, 'migration_result_unknown', '升级结果尚待核验，已保存的数据已保留。')
                        return
                    self._cleanup_evidence(repository, current)
                    backup_status = "completed" if current.backup_status == "completed" else "failed"
                    code = exc.code if isinstance(exc, MigrationError) else ("migration_resources_failed" if current.stage == "resources" else "migration_apply_failed")
                    repository.record_migration_failure(
                        migration_id,
                        current.revision,
                        failure_code=code,
                        failure_summary=("项目与模型配置转换未完成，本次业务变更未提交。请查看问题提示后重试。"
                                         if code == "migration_resources_failed" else _safe_failure_summary(exc)),
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
        if method == "GET" and len(parts) == 4 and parts[3] == "history":
            return self.history(parts[2])
        if method == "GET" and len(parts) == 4 and parts[3] == "backup-grant":
            return self.backup_grant(parts[2], auth_context=auth_context)
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
        with self._lock, ExitStack() as leases:
            for name in ('.migration-execution.lock', '.resource-migration.lock'):
                path = self.service_dir / name
                try:
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                except FileNotFoundError:
                    if name == '.migration-execution.lock':
                        return session
                    continue
                lease = leases.enter_context(os.fdopen(descriptor, 'r'))
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return session
            with ProjectRepository(self.service_dir) as repository:
                current = repository.get_migration_session(session.migration_id)
                if current.revision != session.revision:
                    return current
                if repository.get_data_mode() != 'legacy' or repository.list_projects():
                    return self._record_unknown(repository, current, 'migration_interrupted',
                                                '升级已中断，已保存的项目和历史记录已保留。请继续升级。')
                backup_status = 'failed'
                if session.backup_status == 'completed' and session.backup_path:
                    try:
                        manifest = tuple(SourceManifestEntry(**item) for item in session.source_manifest)
                        backup = self._authorized_backup_path(session.migration_id, session.backup_path)
                        verify_migration_backup(backup, migration_id=session.migration_id,
                                                source_fingerprint=session.source_fingerprint, source_manifest=manifest)
                        backup_status = 'completed'
                    except (MigrationError, LegacySourceError, OSError, KeyError, TypeError, ValueError):
                        pass
                return repository.record_migration_failure(
                    session.migration_id, session.revision, failure_code='migration_interrupted',
                    failure_summary='升级已中断，本次业务变更未提交。请重试升级。', backup_status=backup_status,
                )

    @staticmethod
    def _record_unknown(repository: ProjectRepository, session: MigrationSession, code: str, summary: str):
        with repository.transaction():
            repository.connection.execute(
                "UPDATE migration_sessions SET state='diagnostic_required',acknowledged_at=NULL,completed_at=NULL,failure_code=?,failure_summary=?,"
                "revision=revision+1,updated_at=? WHERE migration_id=? AND revision=?",
                (code, summary, normalize_utc(), session.migration_id, session.revision),
            )
        return repository.get_migration_session(session.migration_id)


def migration_summary_for_startup(
    *,
    startup: StartupInspection,
    service_dir: str | Path,
    config_path: str | Path,
    input_dir: str | Path,
    output_root: str | Path,
    include_inspection: bool = True,
) -> dict[str, Any] | None:
    """Return a DTO-safe M2 summary for the startup envelope."""
    coordinator = MigrationCoordinator(
        service_dir=service_dir,
        config_path=config_path,
        env_path=Path(config_path).parent / ".env",
        input_dir=input_dir,
        output_root=output_root,
    )
    if startup.facts.unreadable:
        return {"entry": "diagnostic", "session": None, "report": None}
    if not include_inspection and not startup.detection.resource_migration_incomplete:
        session = startup.facts.migration_record
        if (
            session is None
            or not session.state.startswith("completed_")
            or session.acknowledged_at is not None
        ):
            return None
    try:
        snapshot = coordinator.snapshot(startup=startup)
    except MigrationError:
        return {"entry": "diagnostic", "session": None, "report": None}
    return {"entry": snapshot["entry"], "session": snapshot["session"], "report": snapshot["report"]}
