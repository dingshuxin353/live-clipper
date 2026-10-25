from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config import Settings
from .project_domain import Run
from .project_result_domain import (
    Issue,
    RecoveryAttempt,
    RequestConflictError,
    RevisionConflictError,
    sanitize_persisted_text,
)
from .project_service import output_directory_is_writable
from .project_storage import ProjectRepository
from .resource_store import ResourceError, ResourceStore

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
    if issue.scope_type == "project" and issue.recovery_capability == "operational_repair":
        project = repository.get_project(issue.project_id)
        runtime = repository.get_runtime(issue.project_id)
        if project is None:
            return {"ok": False, "reason": "project_missing"}
        if runtime is None:
            return {"ok": False, "reason": "runtime_missing"}
        if project.activation_state != "active":
            return {"ok": False, "reason": "project_inactive"}
        if runtime.readiness_state != "ready" or runtime.auto_scan_state == "blocked" or runtime.failure_code:
            return {"ok": False, "reason": "project_not_ready"}
        return {"ok": True, "resolve_issue": True, "safe_checkpoint": "project_ready"}
    run = repository.get_run(str(issue.run_id)) if issue.run_id else None
    if run is None:
        return {"ok": False, "reason": "run_missing"}
    if issue.issue_code in {"source_missing", "source_unreadable", "source_identity_mismatch"}:
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
    if issue.recovery_capability == "continue_run":
        _check_frozen_resources(repository, run)
    return {"ok": True, "safe_checkpoint": issue.safe_checkpoint or "artifacts"}


def _check_frozen_resources(repository: ProjectRepository, run: Run) -> None:
    if run.parameter_snapshot.get('resource_contract') != 1:
        raise ResourceError('original_configuration_unknown')
    try:
        store = ResourceStore(repository)
        for purpose in ('asr', 'analysis', 'review'):
            store.resolved_binding(run.parameter_snapshot['resources'][purpose])
    except (KeyError, TypeError):
        raise ResourceError('original_configuration_unknown') from None


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
        if run is not None and issue.recovery_capability == 'continue_run':
            _check_frozen_resources(repository, run)
        if override_failure is not None:
            result = {"ok": False, "reason": override_failure}
        elif overrides and checker is None:
            result = {"ok": True, "safe_checkpoint": issue.safe_checkpoint or "artifacts"}
        else:
            result = dict(checker(issue) if checker else _default_check(repository, issue, settings))
    except ResourceError as exc:
        result = {"ok": False, "reason": exc.code}
    except Exception:  # noqa: BLE001 - durable issue state must survive checker failures.
        result = {"ok": False, "reason": "check_failed"}
    if result.get("ok"):
        if (
            run is not None
            and issue.recovery_capability == "operational_repair"
            and overrides.get("source_path")
        ):
            return repository.resolve_run_source_issue(
                issue_id,
                expected_issue_revision=issue.issue_revision,
                source_path=str(overrides["source_path"]),
            )
        if result.get("resolve_issue"):
            return repository.transition_issue(
                issue_id,
                expected_issue_revision=issue.issue_revision,
                status="resolved",
                event_type="resolved",
                detail={"safe_checkpoint": result.get("safe_checkpoint")},
                recovery_capability=issue.recovery_capability,
                safe_checkpoint=str(result.get("safe_checkpoint") or issue.safe_checkpoint or "project_ready"),
                operational_overrides=overrides or None,
            )
        return repository.transition_issue(
            issue_id,
            expected_issue_revision=issue.issue_revision,
            status="ready_to_recover",
            event_type="recheck_succeeded",
            detail={"safe_checkpoint": result.get("safe_checkpoint")},
            recovery_capability=issue.recovery_capability,
            safe_checkpoint=str(result.get("safe_checkpoint") or issue.safe_checkpoint or "artifacts"),
            operational_overrides=overrides or None,
        )
    return repository.transition_issue(
        issue_id,
        expected_issue_revision=issue.issue_revision,
        status="action_required",
        event_type="recheck_failed",
        detail={"reason": sanitize_persisted_text(result.get("reason", "not_ready"))},
    )
def _accept_recovery(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
    attempt_type: str,
    operational_overrides: Mapping[str, Any] | None = None,
) -> RecoveryAttempt | Run:
    with repository.transaction():
        issue = repository.get_issue(issue_id)
        if issue is None:
            raise KeyError(issue_id)
        existing = repository.find_recovery_attempt(issue_id, request_id)
        if existing is None:
            if issue.recovery_capability != attempt_type:
                raise ValueError("issue does not allow " + attempt_type)
            if operational_overrides is not None and dict(operational_overrides) != issue.operational_overrides:
                raise ValueError("operational overrides must be validated by recheck_issue")
            if issue.issue_revision != expected_issue_revision:
                raise RevisionConflictError("issue_revision_conflict")
            if issue.status != "ready_to_recover":
                raise ValueError("issue_not_ready_to_recover")
            if attempt_type == "continue_run":
                if issue.run_id is None:
                    raise ValueError("continue_run requires a run issue")
                run = repository.get_run(str(issue.run_id))
                if run is None:
                    raise KeyError(str(issue.run_id))
                if "review" in issue.redo_stages and repository.list_run_outputs(run.run_id):
                    raise ResourceError("review_reprocess_required")
                _check_frozen_resources(repository, run)
                active = repository.find_active_run(run.project_id, run.content_id, exclude_run_id=run.run_id)
                if active is not None:
                    return active
        elif operational_overrides is not None and dict(operational_overrides) != existing.operational_overrides:
            raise RequestConflictError("recovery_request_conflict")
        return repository.accept_recovery(
            issue_id, expected_issue_revision=expected_issue_revision, request_id=request_id,
            requested_by=requested_by, attempt_type=attempt_type,
        )


def continue_run(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
    operational_overrides: Mapping[str, Any] | None = None,
) -> RecoveryAttempt | Run:
    return _accept_recovery(
        repository, issue_id, expected_issue_revision=expected_issue_revision, request_id=request_id,
        requested_by=requested_by, attempt_type="continue_run", operational_overrides=operational_overrides,
    )


def retry_output(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
) -> RecoveryAttempt:
    return _accept_recovery(
        repository, issue_id, expected_issue_revision=expected_issue_revision, request_id=request_id,
        requested_by=requested_by, attempt_type="retry_output",
    )


def retry_material(
    repository: ProjectRepository,
    issue_id: str,
    *,
    expected_issue_revision: int,
    request_id: str,
    requested_by: str,
) -> RecoveryAttempt:
    return _accept_recovery(
        repository, issue_id, expected_issue_revision=expected_issue_revision, request_id=request_id,
        requested_by=requested_by, attempt_type="retry_material",
    )
