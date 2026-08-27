from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config import Settings
from .project_domain import new_id, normalize_utc, stable_json
from .project_resources import resource_map
from .project_result_domain import (
    Issue,
    RecoveryAttempt,
    RequestConflictError,
    RevisionConflictError,
    sanitize_persisted_text,
)
from .project_service import output_directory_is_writable
from .project_storage import ProjectRepository

IssueChecker = Callable[[Issue], Mapping[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _default_check(
    repository: ProjectRepository,
    issue: Issue,
    settings: Settings | None = None,
) -> dict[str, Any]:
    run = repository.get_run(str(issue.run_id)) if issue.run_id else None
    if run is None:
        return {"ok": False, "reason": "run_missing"}
    if issue.issue_code in {"source_missing", "source_unreadable"}:
        source = Path(run.latest_seen_path)
        return {
            "ok": source.is_file() and _sha256(source) == run.content_id,
            "safe_checkpoint": issue.safe_checkpoint or "source_and_transcript",
        }
    if issue.issue_code in {"output_unwritable", "storage_full", "render_failed", "output_missing", "output_unreadable"}:
        output = Path(str(run.parameter_snapshot.get("output", {}).get("directory", ""))).expanduser().resolve()
        return {
            "ok": output_directory_is_writable(output),
            "safe_checkpoint": issue.safe_checkpoint or "validated_review",
        }
    if issue.issue_code in {"asr_resource_unavailable", "ai_resource_unavailable"}:
        if settings is None:
            return {"ok": False, "reason": "resource_check_required"}
        references = run.parameter_snapshot.get("resources", {})
        resource_id = (
            references.get("asr_ref")
            if issue.issue_code == "asr_resource_unavailable"
            else references.get("review_ref")
        )
        resource = resource_map(settings).get(str(resource_id))
        return {
            "ok": bool(resource and resource.ready),
            "safe_checkpoint": issue.safe_checkpoint or "artifacts",
        }
    return {"ok": True, "safe_checkpoint": issue.safe_checkpoint or "artifacts"}


def recheck_issue(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    checker: IssueChecker | None = None,
    operational_overrides: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
) -> Issue:
    issue = repository.transition_issue(
        issue_id,
        expected_issue_revision=expected_issue_revision,
        status="checking",
        event_type="recheck_started",
    )
    overrides = dict(operational_overrides or {})
    run = repository.get_run(str(issue.run_id)) if issue.run_id else None
    override_failure: str | None = None
    if run is not None and "source_path" in overrides:
        source = Path(str(overrides["source_path"])).expanduser()
        if not source.is_absolute() or not source.is_file() or _sha256(source.resolve()) != run.content_id:
            overrides = {**overrides, "source_path": ""}
            override_failure = "source_identity_mismatch"
        else:
            overrides["source_path"] = str(source.resolve())
    if run is not None and "output_directory" in overrides:
        output = Path(str(overrides["output_directory"])).expanduser()
        source_parent = Path(str(overrides.get("source_path") or run.latest_seen_path)).resolve().parent
        if not output.is_absolute() or not output_directory_is_writable(output.resolve()):
            override_failure = "output_unwritable"
        else:
            resolved_output = output.resolve()
            try:
                resolved_output.relative_to(source_parent)
            except ValueError:
                overrides["output_directory"] = str(resolved_output)
            else:
                override_failure = "output_inside_source"
    try:
        if override_failure is not None:
            result = {"ok": False, "reason": override_failure}
        elif overrides and checker is None:
            result = {"ok": True, "safe_checkpoint": issue.safe_checkpoint or "artifacts"}
        else:
            result = dict(checker(issue) if checker else _default_check(repository, issue, settings))
    except Exception:  # noqa: BLE001 - durable issue state must survive checker failures.
        result = {"ok": False, "reason": "check_failed"}
    if result.get("ok"):
        if overrides:
            with repository.transaction():
                repository.connection.execute(
                    "UPDATE issues SET operational_overrides_json = ? WHERE issue_id = ?",
                    (stable_json(overrides), issue_id),
                )
        return repository.transition_issue(
            issue_id,
            expected_issue_revision=issue.issue_revision,
            status="ready_to_recover",
            event_type="recheck_succeeded",
            detail={"safe_checkpoint": result.get("safe_checkpoint")},
            recovery_capability=issue.recovery_capability,
            safe_checkpoint=str(result.get("safe_checkpoint") or issue.safe_checkpoint or "artifacts"),
        )
    return repository.transition_issue(
        issue_id,
        expected_issue_revision=issue.issue_revision,
        status="action_required",
        event_type="recheck_failed",
        detail={"reason": sanitize_persisted_text(result.get("reason", "not_ready"))},
    )


def _existing_attempt(repository: ProjectRepository, issue_id: str, request_id: str) -> RecoveryAttempt | None:
    return next(
        (item for item in repository.list_recovery_attempts(issue_id) if item.request_id == request_id),
        None,
    )


def _accepted_attempt(
    repository: ProjectRepository,
    issue: Issue,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
    attempt_type: str,
    output_id: str | None = None,
    material_id: str | None = None,
    operational_overrides: Mapping[str, Any] | None = None,
) -> RecoveryAttempt:
    existing = _existing_attempt(repository, issue.issue_id, request_id)
    if existing is not None:
        expected = (attempt_type, issue.run_id, output_id, material_id, sanitize_persisted_text(requested_by))
        actual = (
            existing.attempt_type,
            existing.run_id,
            existing.output_id,
            existing.material_id,
            existing.requested_by,
        )
        if actual != expected:
            raise RequestConflictError("recovery_request_conflict")
        return existing
    if issue.status != "ready_to_recover":
        raise ValueError("issue_not_ready_to_recover")
    if issue.issue_revision != expected_issue_revision:
        raise RevisionConflictError("issue_revision_conflict")
    timestamp = normalize_utc()
    attempt_id = new_id()
    overrides = dict(issue.operational_overrides if operational_overrides is None else operational_overrides)
    with repository.transaction():
        current = repository.connection.execute(
            "SELECT status, issue_revision FROM issues WHERE issue_id = ?", (issue.issue_id,)
        ).fetchone()
        if current is None:
            raise KeyError(issue.issue_id)
        if current[0] != "ready_to_recover" or int(current[1]) != expected_issue_revision:
            raise RevisionConflictError("issue_revision_conflict")
        repository.connection.execute(
            """INSERT INTO recovery_attempts(
                 attempt_id, issue_id, request_id, attempt_type, run_id, output_id,
                 material_id, requested_by, reuse_stages_json, redo_stages_json,
                 operational_overrides_json, status, requested_at, accepted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
            (
                attempt_id,
                issue.issue_id,
                request_id,
                attempt_type,
                issue.run_id,
                output_id,
                material_id,
                sanitize_persisted_text(requested_by),
                stable_json(issue.reuse_stages),
                stable_json(issue.redo_stages),
                stable_json(overrides),
                timestamp,
                timestamp,
            ),
        )
        repository.connection.execute(
            """UPDATE issues SET status = 'recovering', total_attempt_count = total_attempt_count + 1,
                 operational_overrides_json = ?, issue_revision = issue_revision + 1, updated_at = ?
               WHERE issue_id = ?""",
            (stable_json(overrides), timestamp, issue.issue_id),
        )
        repository.connection.execute(
            "INSERT INTO issue_events(issue_id, event_type, occurred_at, detail_json) VALUES (?, 'recovery_accepted', ?, ?)",
            (issue.issue_id, timestamp, stable_json({"attempt_id": attempt_id, "attempt_type": attempt_type})),
        )
        if attempt_type == "continue_run":
            redo_stage = issue.redo_stages[0] if issue.redo_stages else "read_source"
            cursor = repository.connection.execute(
                """UPDATE runs SET status = 'queued', current_stage = ?, queued_at = ?, completed_at = NULL,
                     updated_at = ?, error_code = NULL, error_summary = NULL WHERE run_id = ?""",
                (redo_stage, timestamp, timestamp, issue.run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(str(issue.run_id))
            repository.connection.execute(
                "INSERT INTO run_stage_events(run_id, stage, event_type, occurred_at, detail_json) VALUES (?, ?, 'recovery_queued', ?, ?)",
                (issue.run_id, redo_stage, timestamp, stable_json({"attempt_id": attempt_id})),
            )
        elif attempt_type == "retry_output":
            cursor = repository.connection.execute(
                """UPDATE run_outputs SET status = 'pending', error_code = NULL, error_summary = NULL,
                     updated_at = ? WHERE output_id = ? AND run_id = ?""",
                (timestamp, output_id, issue.run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(str(output_id))
        elif attempt_type == "retry_material":
            cursor = repository.connection.execute(
                """UPDATE output_materials SET status = 'pending', updated_at = ?
                   WHERE material_id = ? AND output_id = ?""",
                (timestamp, material_id, output_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(str(material_id))
        run = repository.get_run(str(issue.run_id))
        assert run is not None
        repository.connection.execute(
            """INSERT INTO workspace_events(event_type, project_id, run_id, occurred_at, payload_json)
               VALUES ('recovery_accepted', ?, ?, ?, ?)""",
            (run.project_id, issue.run_id, timestamp, stable_json({"attempt_id": attempt_id, "type": attempt_type})),
        )
    result = repository.get_recovery_attempt(attempt_id)
    assert result is not None
    return result


def continue_run(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
    operational_overrides: Mapping[str, Any] | None = None,
) -> RecoveryAttempt:
    issue = repository.get_issue(issue_id)
    if issue is None:
        raise KeyError(issue_id)
    if issue.run_id is None:
        raise ValueError("continue_run requires a run issue")
    if issue.recovery_capability != "continue_run":
        raise ValueError("issue does not allow continue_run")
    if operational_overrides is not None and dict(operational_overrides) != issue.operational_overrides:
        raise ValueError("operational overrides must be validated by recheck_issue")
    return _accepted_attempt(
        repository,
        issue,
        expected_issue_revision=expected_issue_revision,
        request_id=request_id,
        requested_by=requested_by,
        attempt_type="continue_run",
        operational_overrides=operational_overrides,
    )


def retry_output(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
) -> RecoveryAttempt:
    issue = repository.get_issue(issue_id)
    if issue is None:
        raise KeyError(issue_id)
    if issue.output_id is None:
        raise ValueError("retry_output requires an output issue")
    if issue.recovery_capability != "retry_output":
        raise ValueError("issue does not allow retry_output")
    return _accepted_attempt(
        repository,
        issue,
        expected_issue_revision=expected_issue_revision,
        request_id=request_id,
        requested_by=requested_by,
        attempt_type="retry_output",
        output_id=issue.output_id,
    )


def retry_material(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
) -> RecoveryAttempt:
    issue = repository.get_issue(issue_id)
    if issue is None:
        raise KeyError(issue_id)
    if issue.output_id is None or issue.material_id is None:
        raise ValueError("retry_material requires a material issue")
    if issue.recovery_capability != "retry_material":
        raise ValueError("issue does not allow retry_material")
    return _accepted_attempt(
        repository,
        issue,
        expected_issue_revision=expected_issue_revision,
        request_id=request_id,
        requested_by=requested_by,
        attempt_type="retry_material",
        output_id=issue.output_id,
        material_id=issue.material_id,
    )
