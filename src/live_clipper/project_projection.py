from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

WORKLOAD_STATUSES = ("processing", "queued", "awaiting_review", "failed", "completed")
RESULT_WORKLOAD_STATUSES = ("processing", "queued", "failed", "completed", "new_results")


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


@dataclass(frozen=True)
class ResultProjection:
    result_type: str
    candidate_count: int
    selected_count: int
    rejected_count: int
    available_output_count: int
    failed_output_count: int
    total_duration_ms: int


@dataclass(frozen=True)
class ResultWorkload:
    processing: int = 0
    queued: int = 0
    failed: int = 0
    completed: int = 0
    new_results: int = 0

    def as_dict(self) -> dict[str, int]:
        return {status: getattr(self, status) for status in RESULT_WORKLOAD_STATUSES}


@dataclass(frozen=True)
class ProjectResultProjection:
    main_status: str
    activation_state: str
    blocked: bool
    workload: ResultWorkload


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


def project_result_projection(
    *,
    review_status: str,
    decisions: Iterable[Any],
    outputs: Iterable[Any],
    material_problem_count: int = 0,
) -> ResultProjection:
    """Project one result from validated review decisions and current output facts."""
    decision_items = list(decisions)
    output_items = list(outputs)
    selected = [item for item in decision_items if str(_field(item, "decision")) == "selected"]
    rejected = [item for item in decision_items if str(_field(item, "decision")) == "rejected"]
    if len(decision_items) != len(selected) + len(rejected):
        raise ValueError("candidate decisions must be selected or rejected")
    output_by_id = {str(_field(item, "output_id")): item for item in output_items}
    if len(output_by_id) != len(output_items):
        raise ValueError("output identities must be unique")
    selected_output_ids: list[str] = []
    for item in selected:
        output_id = _field(item, "output_id")
        if not output_id or str(output_id) not in output_by_id:
            raise ValueError("every selected decision must reference one output")
        selected_output_ids.append(str(output_id))
    if len(set(selected_output_ids)) != len(selected_output_ids):
        raise ValueError("selected decisions must not share an output")
    if any(_field(item, "output_id") is not None for item in rejected):
        raise ValueError("rejected decisions cannot reference an output")
    if set(output_by_id) != set(selected_output_ids):
        raise ValueError("every output must belong to one selected decision")

    ready = [item for item in output_items if str(_field(item, "status")) == "ready"]
    failed = [
        item
        for item in output_items
        if str(_field(item, "status")) in {"failed", "missing", "unreadable"}
    ]
    for item in ready:
        required = ("duration_ms", "width", "height", "container", "video_codec", "byte_size")
        if any(_field(item, field) in {None, ""} for field in required):
            raise ValueError("ready outputs require complete media metadata")
        numeric = [_field(item, field) for field in ("duration_ms", "width", "height", "byte_size")]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in numeric):
            raise ValueError("ready output media metadata must use non-negative integers")

    if review_status == "no_clip":
        if selected or output_items:
            raise ValueError("no_clip review cannot contain selected decisions or outputs")
        result_type = "no_clip"
    elif ready and not failed and material_problem_count == 0:
        result_type = "clips_ready"
    elif ready:
        result_type = "partial"
    else:
        result_type = "unavailable"
    return ResultProjection(
        result_type=result_type,
        candidate_count=len(decision_items),
        selected_count=len(selected),
        rejected_count=len(rejected),
        available_output_count=len(ready),
        failed_output_count=len(failed),
        total_duration_ms=sum(int(_field(item, "duration_ms", 0) or 0) for item in ready),
    )


def result_workload_counts(runs: Iterable[Any], results: Iterable[Any]) -> ResultWorkload:
    """Count second-batch workload while deliberately excluding legacy awaiting_review rows."""
    result_by_run = {str(_field(item, "run_id")): item for item in results}
    counts = {status: 0 for status in RESULT_WORKLOAD_STATUSES}
    for run in runs:
        status = str(_field(run, "status", ""))
        if status not in {"processing", "queued", "failed", "completed"}:
            continue
        counts[status] += 1
        if status == "completed":
            result = result_by_run.get(str(_field(run, "run_id")))
            if result is not None:
                revision = int(_field(result, "result_revision", 0) or 0)
                seen_revision = _field(result, "seen_result_revision")
                if seen_revision is None or int(seen_revision) < revision:
                    counts["new_results"] += 1
    return ResultWorkload(**counts)


def project_projection_v2(
    *,
    activation_state: str,
    runs: Iterable[Any],
    results: Iterable[Any],
    blocking_issue: bool = False,
) -> ProjectResultProjection:
    workload = result_workload_counts(runs, results)
    if blocking_issue:
        main_status = "blocked"
    elif workload.failed:
        main_status = "failed"
    elif workload.processing:
        main_status = "processing"
    elif workload.queued:
        main_status = "queued"
    elif workload.new_results:
        main_status = "new_results"
    elif activation_state == "paused":
        main_status = "paused"
    elif activation_state == "inactive":
        main_status = "inactive"
    else:
        main_status = "idle"
    return ProjectResultProjection(
        main_status=main_status,
        activation_state=activation_state,
        blocked=blocking_issue,
        workload=workload,
    )
