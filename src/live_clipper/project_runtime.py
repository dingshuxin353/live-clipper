from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .project_domain import Run
from .project_storage import ProjectRepository, database_path


@dataclass(frozen=True)
class RuntimeReport:
    started_run_ids: tuple[str, ...] = ()
    failed_run_ids: tuple[str, ...] = ()
    recovered_run_ids: tuple[str, ...] = ()


class ProjectRuntimeStartError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def run_work_dir(work_dir: str | Path, run: Run) -> Path:
    return Path(work_dir).expanduser().resolve() / "projects" / run.project_id / "runs" / run.run_id


def dispatch_queued(
    repository: ProjectRepository,
    *,
    work_dir: str | Path,
    processor: Callable[[Run, Path], Any],
    capacity: int = 1,
) -> RuntimeReport:
    processing = sum(
        run.status == "processing" and run.current_stage not in {"review", "render"}
        for run in repository.list_runs()
    )
    available = max(0, capacity - processing)
    started: list[str] = []
    failed: list[str] = []
    for run in (
        item
        for item in repository.list_runs()
        if item.status == "queued" and item.current_stage not in {"review", "render"}
    ):
        if len(started) >= available:
            break
        expected_source = run.parameter_snapshot.get("source")
        if isinstance(expected_source, dict):
            try:
                source_stat = Path(run.latest_seen_path).stat()
                source_error = None if (
                    source_stat.st_size == expected_source.get("bytes")
                    and source_stat.st_mtime_ns == expected_source.get("mtime_ns")
                ) else "source_identity_mismatch"
            except OSError:
                source_error = "source_missing"
            if source_error:
                repository.transition_run(
                    run.run_id,
                    status="failed",
                    stage="read_source",
                    event_type="failed",
                    detail={"reason": source_error},
                    error_code=source_error,
                    error_summary="原始录像已变化，重新处理已安全停止",
                )
                failed.append(run.run_id)
                continue
        target = run_work_dir(work_dir, run)
        target.mkdir(parents=True, exist_ok=True)
        repository.transition_run(
            run.run_id,
            status="processing",
            stage="read_source",
            event_type="started",
            detail={"run_dir": str(target)},
        )
        try:
            processor_result = processor(run, target)
        except Exception as exc:  # noqa: BLE001 - one failed start must not block the FIFO.
            error_code = str(getattr(exc, "code", "processor_start_failed"))
            repository.transition_run(
                run.run_id,
                status="failed",
                stage="read_source",
                event_type="failed",
                detail={"reason": error_code},
                error_code=error_code,
                error_summary="处理资源不可用" if error_code.endswith("resource_unavailable") else "处理任务启动失败",
            )
            if run.parameter_snapshot.get("schema_version") == 2 and error_code in {
                "asr_resource_unavailable",
                "ai_resource_unavailable",
            }:
                from .project_result_runtime import _functional_issue

                _functional_issue(repository, run, error_code)
            failed.append(run.run_id)
            continue
        repository.append_stage_event(
            run.run_id,
            stage="read_source",
            event_type="process_started",
            detail={"pid": processor_result} if isinstance(processor_result, int) else {},
        )
        started.append(run.run_id)
    return RuntimeReport(started_run_ids=tuple(started), failed_run_ids=tuple(failed))


def recover_processing(
    repository: ProjectRepository,
    *,
    validator: Callable[[Run, Path], bool],
    work_dir: str | Path,
) -> RuntimeReport:
    recovered: list[str] = []
    failed: list[str] = []
    for run in (item for item in repository.list_runs() if item.status == "processing"):
        target = run_work_dir(work_dir, run)
        stage = run.current_stage or "read_source"
        if validator(run, target):
            repository.transition_run(
                run.run_id,
                status="processing",
                stage=stage,
                event_type="recovery_succeeded",
                detail={"run_dir": str(target)},
            )
            recovered.append(run.run_id)
        else:
            repository.transition_run(
                run.run_id,
                status="failed",
                stage=stage,
                event_type="recovery_failed",
                detail={"reason": "stage_not_recoverable"},
                error_code="recovery_failed",
                error_summary="重启后无法安全恢复当前处理阶段",
            )
            failed.append(run.run_id)
    return RuntimeReport(recovered_run_ids=tuple(recovered), failed_run_ids=tuple(failed))


def _launch_with_existing_pipeline(settings: Settings, service_dir: Path) -> Callable[[Run, Path], int]:
    def launch(run: Run, target: Path) -> int:
        from . import service

        if run.parameter_snapshot.get("schema_version") == 2:
            asr = run.parameter_snapshot.get("resources", {}).get("asr", {})
            if asr.get("backend") == "openai" and not settings.asr_api_key:
                raise ProjectRuntimeStartError("asr_resource_unavailable", "ASR resource is unavailable")
            if not settings.cheap_model_api_key:
                raise ProjectRuntimeStartError("ai_resource_unavailable", "analysis resource is unavailable")
        else:
            service.require_pipeline_configuration(settings)
        source = Path(run.latest_seen_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        return service._start_pipeline_process(  # noqa: SLF001 - explicit compatibility adapter.
            source,
            input_dir=target / "input",
            run_dir=target,
            log_path=service_dir / "runs" / f"{run.run_id}.log",
        )

    return launch


def reconcile_processing(repository: ProjectRepository, *, work_dir: str | Path, stale_after_minutes: int) -> list[str]:
    changed = []
    current = datetime.now(UTC)
    for run in (item for item in repository.list_runs() if item.status == "processing"):
        target = run_work_dir(work_dir, run)
        clips = target / "clips"
        verified_stages = []
        if (target / "run_metadata.json").is_file() or (target / "audio.wav").is_file():
            verified_stages.append("read_source")
        if (target / "transcript.json").is_file():
            verified_stages.append("transcribe")
        if (target / "merged_candidates.json").is_file():
            verified_stages.append("analyze")
        if (target / "codex_brief.json").is_file():
            verified_stages.append("arbitrate")
        stage_order = ["read_source", "transcribe", "analyze", "arbitrate"]
        current_index = stage_order.index(run.current_stage) if run.current_stage in stage_order else -1
        for stage in verified_stages:
            stage_index = stage_order.index(stage)
            if stage_index <= current_index:
                continue
            repository.transition_run(
                run.run_id,
                status="processing",
                stage=stage,
                event_type="succeeded",
                detail={"verified_artifact_boundary": True},
            )
            current_index = stage_index
            changed.append(run.run_id)
        if clips.is_dir() and any(clips.glob("*.mp4")):
            repository.transition_run(run.run_id, status="completed", stage="render", event_type="completed")
            changed.append(run.run_id)
        elif (target / "codex_brief.json").is_file():
            if run.parameter_snapshot.get("schema_version") == 2:
                if run.current_stage != "review":
                    repository.transition_run(
                        run.run_id,
                        status="processing",
                        stage="review",
                        event_type="review_ready",
                    )
            else:
                repository.transition_run(
                    run.run_id,
                    status="awaiting_review",
                    stage="review",
                    event_type="awaiting_review",
                )
            changed.append(run.run_id)
        else:
            updated = datetime.fromisoformat(run.updated_at.replace("Z", "+00:00"))
            if stale_after_minutes > 0 and current - updated >= timedelta(minutes=stale_after_minutes):
                repository.transition_run(
                    run.run_id,
                    status="failed",
                    stage=run.current_stage or "read_source",
                    event_type="recovery_failed",
                    error_code="processing_interrupted",
                    error_summary="处理进程中断且没有可验证产物",
                )
                changed.append(run.run_id)
    return list(dict.fromkeys(changed))


def ensure_retention_confirmations(
    repository: ProjectRepository,
    settings: Settings,
    *,
    service_dir: Path,
    now: datetime | None = None,
) -> list[str]:
    from . import service
    from .pipeline import cleanup_plan

    current = now or datetime.now(UTC)
    existing_run_ids = {
        str(item.get("run_id"))
        for item in service.load_confirmations(service_dir)
        if item.get("action") == "cleanup_confirm"
    }
    created: list[str] = []
    work_root = Path(settings.paths.work_dir).expanduser().resolve()
    for run in (item for item in repository.list_runs() if item.status == "completed"):
        if run.run_id in existing_run_ids:
            continue
        policy = str(run.parameter_snapshot.get("output", {}).get("intermediate_retention", "keep"))
        if policy == "keep" or not run.completed_at:
            continue
        completed_at = datetime.fromisoformat(run.completed_at.replace("Z", "+00:00"))
        if policy == "remind_after_7_days" and current - completed_at < timedelta(days=7):
            continue
        target = run_work_dir(work_root, run)
        expected_root = work_root / "projects" / run.project_id / "runs" / run.run_id
        if target != expected_root.resolve() or not target.is_dir():
            continue
        try:
            targets = cleanup_plan(target, input_dir=target / "input")
        except (FileNotFoundError, OSError, ValueError):
            continue
        safe_targets = []
        for item in targets:
            path = Path(str(item.get("path", ""))).resolve()
            try:
                path.relative_to(target)
            except ValueError:
                continue
            if item.get("deletable") and item.get("kind") in {"audio", "local_source_video"}:
                safe_targets.append(item)
        if not safe_targets:
            continue
        service.create_confirmation(
            action="cleanup_confirm",
            run_id=run.run_id,
            target_path=target,
            reason="项目中间产物已达到保留策略的提醒时间",
            risk_level="high",
            validation={"cleanup_targets": safe_targets, "project_run": True},
            service_dir=service_dir,
            created_by="project_runtime",
        )
        existing_run_ids.add(run.run_id)
        created.append(run.run_id)
    return created


def tick_project_runtime(settings: Settings, *, service_dir: Path) -> dict[str, Any]:
    if not database_path(service_dir).exists():
        return {"ok": True, "mode": "legacy"}
    with ProjectRepository(service_dir) as repository:
        if repository.get_data_mode() != "projects":
            return {"ok": True, "mode": "legacy"}
        from .project_service import ProjectManager

        manager = ProjectManager(repository, settings)
        upgraded_project_ids: list[str] = []
        for project in repository.list_projects():
            revision = repository.get_config_revision(project.project_id)
            if revision is not None and revision.schema_version == 1:
                manager.ensure_v2_config(project.project_id)
                upgraded_project_ids.append(project.project_id)
        reconciled = reconcile_processing(
            repository,
            work_dir=settings.paths.work_dir,
            stale_after_minutes=settings.service.stuck_after_minutes,
        )
        report = dispatch_queued(
            repository,
            work_dir=settings.paths.work_dir,
            processor=_launch_with_existing_pipeline(settings, service_dir),
            capacity=1,
        )
        retention_run_ids = ensure_retention_confirmations(repository, settings, service_dir=service_dir)
        from .project_result_runtime import tick_project_result_workers

        worker_report = tick_project_result_workers(
            repository,
            settings,
            work_dir=settings.paths.work_dir,
        )
        return {
            "ok": True,
            "mode": "projects",
            "reconciled_run_ids": reconciled,
            "started_run_ids": list(report.started_run_ids),
            "failed_run_ids": list(report.failed_run_ids),
            "retention_confirmation_run_ids": retention_run_ids,
            "upgraded_project_ids": upgraded_project_ids,
            "review_worker_run_ids": list(worker_report.scheduled_review_run_ids),
            "render_worker_run_ids": list(worker_report.scheduled_render_run_ids),
            "completed_worker_run_ids": list(worker_report.completed_run_ids),
            "failed_worker_run_ids": list(worker_report.failed_run_ids),
        }
