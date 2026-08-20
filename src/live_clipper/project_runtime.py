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


def run_work_dir(work_dir: str | Path, run: Run) -> Path:
    return Path(work_dir).expanduser().resolve() / "projects" / run.project_id / "runs" / run.run_id


def dispatch_queued(
    repository: ProjectRepository,
    *,
    work_dir: str | Path,
    processor: Callable[[Run, Path], Any],
    capacity: int = 1,
) -> RuntimeReport:
    processing = sum(run.status == "processing" for run in repository.list_runs())
    available = max(0, capacity - processing)
    started: list[str] = []
    failed: list[str] = []
    for run in (item for item in repository.list_runs() if item.status == "queued"):
        if len(started) >= available:
            break
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
        except Exception:  # noqa: BLE001 - one failed start must not block the FIFO.
            repository.transition_run(
                run.run_id,
                status="failed",
                stage="read_source",
                event_type="failed",
                detail={"reason": "processor_start_failed"},
                error_code="processor_start_failed",
                error_summary="处理任务启动失败",
            )
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
        return {
            "ok": True,
            "mode": "projects",
            "reconciled_run_ids": reconciled,
            "started_run_ids": list(report.started_run_ids),
            "failed_run_ids": list(report.failed_run_ids),
            "retention_confirmation_run_ids": retention_run_ids,
        }
