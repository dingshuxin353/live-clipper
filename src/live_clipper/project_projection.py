from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

WORKLOAD_STATUSES = ("processing", "queued", "awaiting_review", "failed", "completed")


@dataclass(frozen=True)
class Workload:
    processing: int = 0
    queued: int = 0
    awaiting_review: int = 0
    failed: int = 0
    completed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {status: getattr(self, status) for status in WORKLOAD_STATUSES}


@dataclass(frozen=True)
class ProjectProjection:
    main_status: str
    activation_state: str
    blocked: bool
    workload: Workload


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def workload_counts(runs: Iterable[Any]) -> Workload:
    counts = {status: 0 for status in WORKLOAD_STATUSES}
    for run in runs:
        status = str(_field(run, "status", ""))
        if status in counts:
            counts[status] += 1
    return Workload(**counts)


def project_projection(
    *,
    activation_state: str,
    runs: Iterable[Any],
    blocked: bool = False,
) -> ProjectProjection:
    workload = workload_counts(runs)
    if blocked or workload.failed:
        main_status = "blocked" if blocked else "failed"
    elif workload.awaiting_review:
        main_status = "awaiting_review"
    elif workload.processing:
        main_status = "processing"
    elif workload.queued:
        main_status = "queued"
    elif activation_state == "paused":
        main_status = "paused"
    elif activation_state == "inactive":
        main_status = "inactive"
    else:
        main_status = "idle"
    return ProjectProjection(
        main_status=main_status,
        activation_state=activation_state,
        blocked=blocked,
        workload=workload,
    )


def queue_positions(runs: Iterable[Any]) -> dict[str, int]:
    queued = [run for run in runs if _field(run, "status") == "queued"]
    queued.sort(key=lambda run: (str(_field(run, "queued_at", "")), str(_field(run, "run_id", ""))))
    return {str(_field(run, "run_id")): index for index, run in enumerate(queued, start=1)}
