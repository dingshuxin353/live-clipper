from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project_domain import legacy_id, normalize_utc, stable_json
from .project_projection import project_result_projection
from .project_storage import ProjectRepository, _one


@dataclass(frozen=True)
class ProjectResultArtifactInspection:
    run_id: str
    project_id: str
    evidence_hash: str
    selections: tuple[dict[str, Any], ...]
    edit_decisions: tuple[dict[str, Any], ...]
    clip_facts: tuple[dict[str, Any], ...]
    report: tuple[dict[str, str], ...]
    run_workspace: Path = field(repr=False)


@dataclass(frozen=True)
class ProjectResultIndexPlan:
    run_id: str
    project_id: str
    evidence_hash: str
    review_session_id: str | None
    decisions: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    materials: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    report: tuple[dict[str, str], ...]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "run_id": self.run_id,
            "evidence_hash": self.evidence_hash,
            "decision_count": len(self.decisions),
            "output_count": len(self.outputs),
            "issue_count": len(self.issues),
            "report": list(self.report),
        }


@dataclass(frozen=True)
class ProjectResultIndexApplyResult:
    run_id: str
    evidence_hash: str
    applied: bool
    already_applied: bool
    result_type: str | None
    output_count: int
    issue_count: int


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def inspect_safe_migration_result(
    path: str | Path,
    *,
    output_root: str | Path,
    expected_sha256: str,
) -> dict[str, Any] | None:
    """Verify one explicitly registered legacy result without discovering neighbours."""
    target = Path(path).expanduser()
    root = Path(output_root).expanduser()
    if target.is_symlink() or root.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        resolved = target.resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    digest = hashlib.sha256()
    _hash_file(digest, resolved)
    if digest.hexdigest() != expected_sha256:
        return None
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        process = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(resolved),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(process.stdout) if process.returncode == 0 else {}
        streams = payload.get("streams", []) if isinstance(payload, Mapping) else []
        video = next(
            (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"),
            None,
        )
        format_payload = payload.get("format", {}) if isinstance(payload, Mapping) else {}
        duration_ms = max(1, round(float(format_payload.get("duration")) * 1000))
        width = int(video.get("width")) if video else 0
        height = int(video.get("height")) if video else 0
        container = str(format_payload.get("format_name") or "")
        codec = str(video.get("codec_name") or "") if video else ""
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return None
    if width <= 0 or height <= 0 or not container or not codec:
        return None
    return {
        "relative_path": relative.as_posix(),
        "file_name": resolved.name,
        "sha256": expected_sha256,
        "duration_ms": duration_ms,
        "width": width,
        "height": height,
        "container": container,
        "video_codec": codec,
        "byte_size": resolved.stat().st_size,
    }


def _candidate_id(item: Mapping[str, Any]) -> str:
    candidate_id = str(item.get("candidate_id") or item.get("clip_id") or "")
    return candidate_id if _SAFE_ID.fullmatch(candidate_id) else ""


def _hash_file(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)


def _known_run_workspace(repository: ProjectRepository, run_id: str, supplied: str | Path | None) -> Path:
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    snapshot = run.parameter_snapshot
    explicit = snapshot.get("run_workspace")
    if explicit:
        expected = Path(str(explicit)).expanduser().resolve()
    else:
        work_root = snapshot.get("work_dir")
        if not work_root:
            raise ValueError("run workspace is not bound by the stored parameter snapshot")
        expected = Path(str(work_root)).expanduser().resolve() / "runs" / run_id
    target = Path(supplied).expanduser().resolve() if supplied is not None else expected
    if target != expected or not target.is_dir():
        raise ValueError("run workspace does not match the stored controlled reference")
    return target


def _contained_file(workspace: Path, relative_path: str) -> Path | None:
    candidate = workspace / relative_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _duration_ms(selection: Mapping[str, Any], edl: Mapping[str, Any]) -> int:
    if edl.get("duration_ms") is not None:
        return max(0, int(edl["duration_ms"]))
    start = float(selection.get("source_start", selection.get("source_start_ms", 0) / 1000))
    end = float(selection.get("source_end", selection.get("source_end_ms", 0) / 1000))
    removed = 0.0
    for item in selection.get("remove_ranges", []):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            removed += max(0.0, float(item[1]) - float(item[0]))
    return max(0, round((end - start - removed) * 1000))


def inspect_project_result_artifacts(
    repository: ProjectRepository,
    run_id: str,
    *,
    run_workspace: str | Path | None = None,
) -> ProjectResultArtifactInspection:
    """Inspect only the workspace already bound to this Run; never writes or scans elsewhere."""
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    workspace = _known_run_workspace(repository, run_id, run_workspace)
    selection_path = workspace / "selected_clips.json"
    edl_path = workspace / "edit_decision_list.json"
    report: list[dict[str, str]] = []
    selections: list[dict[str, Any]] = []
    edit_decisions: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for label, path in (("selected_clips", selection_path), ("edit_decision_list", edl_path)):
        if not path.is_file() or path.is_symlink():
            report.append({"code": f"{label}_missing", "message": f"{label} is unavailable"})
            continue
        raw = path.read_bytes()
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
        loaded = json.loads(raw)
        if not isinstance(loaded, list) or any(not isinstance(item, Mapping) for item in loaded):
            report.append({"code": f"{label}_invalid", "message": f"{label} is not a list"})
            continue
        target = selections if label == "selected_clips" else edit_decisions
        target.extend(dict(item) for item in loaded)

    edl_by_candidate = {
        _candidate_id(item): item for item in edit_decisions if _candidate_id(item)
    }
    clip_facts: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for selection in selections:
        candidate_id = _candidate_id(selection)
        if not candidate_id or candidate_id in seen_candidates:
            report.append({"code": "candidate_identity_invalid", "message": "candidate identity is missing or repeated"})
            continue
        seen_candidates.add(candidate_id)
        edl = edl_by_candidate.get(candidate_id)
        if edl is None:
            report.append({"code": "edl_candidate_missing", "message": f"EDL is unavailable for {candidate_id}"})
            continue
        file_name = str(edl.get("file_name") or selection.get("file_name") or f"{candidate_id}.mp4")
        if not file_name or "/" in file_name or "\\" in file_name:
            report.append({"code": "clip_name_invalid", "message": f"clip name is invalid for {candidate_id}"})
            continue
        relative_path = f"clips/{file_name}"
        clip = _contained_file(workspace, relative_path)
        fact = {
            "candidate_id": candidate_id,
            "file_name": file_name,
            "relative_path": relative_path,
            "status": "ready" if clip is not None else "missing",
            "duration_ms": _duration_ms(selection, edl),
            "width": int(edl.get("width", 0)),
            "height": int(edl.get("height", 0)),
            "container": str(edl.get("container") or Path(file_name).suffix.lstrip(".") or "unknown"),
            "video_codec": str(edl.get("video_codec") or "legacy_unknown"),
            "byte_size": int(clip.stat().st_size) if clip is not None else None,
        }
        if clip is not None:
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            _hash_file(digest, clip)
            digest.update(b"\0")
        else:
            report.append({"code": "clip_file_missing", "message": f"registered clip is missing for {candidate_id}"})
        clip_facts.append(fact)
    if not selections and selection_path.is_file():
        report.append({"code": "empty_selection_unproven", "message": "an empty legacy selection is not no_clip"})
    evidence_hash = digest.hexdigest()
    return ProjectResultArtifactInspection(
        run_id=run_id,
        project_id=run.project_id,
        evidence_hash=evidence_hash,
        selections=tuple(selections),
        edit_decisions=tuple(edit_decisions),
        clip_facts=tuple(clip_facts),
        report=tuple(report),
        run_workspace=workspace,
    )


def build_project_result_index_plan(
    inspection: ProjectResultArtifactInspection,
) -> ProjectResultIndexPlan:
    decisions: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    review_session_id = (
        legacy_id(inspection.evidence_hash, f"review:{inspection.run_id}") if inspection.clip_facts else None
    )
    selection_by_candidate = {
        _candidate_id(item): item for item in inspection.selections if _candidate_id(item)
    }
    for index, fact in enumerate(inspection.clip_facts, start=1):
        candidate_id = fact["candidate_id"]
        selection = selection_by_candidate[candidate_id]
        output_id = legacy_id(inspection.evidence_hash, f"output:{inspection.run_id}:{candidate_id}")
        source_start_ms = int(
            selection.get("source_start_ms", round(float(selection.get("source_start", 0)) * 1000))
        )
        source_end_ms = int(
            selection.get("source_end_ms", round(float(selection.get("source_end", 0)) * 1000))
        )
        decisions.append(
            {
                "decision_id": legacy_id(
                    inspection.evidence_hash, f"decision:{inspection.run_id}:{candidate_id}"
                ),
                "candidate_id": candidate_id,
                "decision": "selected",
                "rank": index,
                "candidate_type": str(selection.get("candidate_type") or "legacy_highlight"),
                "source_start_ms": source_start_ms,
                "source_end_ms": source_end_ms,
                "selected_start_ms": source_start_ms,
                "selected_end_ms": source_end_ms,
                "remove_ranges": selection.get("remove_ranges", []),
                "hook": str(selection.get("hook") or ""),
                "core_value": str(selection.get("core_value") or ""),
                "reason": str(selection.get("reason") or "Legacy selected clip"),
                "risks": [],
                "transcript_excerpt": str(selection.get("transcript_excerpt") or ""),
                "output_id": output_id,
            }
        )
        outputs.append({"output_id": output_id, "display_order": index, **fact})
        materials.append(
            {
                "material_id": legacy_id(inspection.evidence_hash, f"material:{output_id}"),
                "output_id": output_id,
                "title_candidates": [],
                "preferred_title_id": None,
                "description": "",
                "tags": [],
                "generation_source": "indexed_v1",
                "status": "pending",
            }
        )
        if fact["status"] == "missing":
            issues.append(
                {
                    "issue_code": "output_missing",
                    "category": "output",
                    "scope_type": "output",
                    "output_id": output_id,
                    "issue_group_key": "legacy-output-missing",
                    "impact_level": "blocking",
                    "title": "历史成片文件缺失",
                    "summary": "历史记录中的成片文件当前不可用",
                    "recovery_capability": "retry_output",
                }
            )
    if inspection.selections:
        issues.append(
            {
                "issue_code": "legacy_judgement_unavailable",
                "category": "review",
                "scope_type": "run",
                "output_id": None,
                "issue_group_key": "legacy-review-evidence",
                "impact_level": "informational",
                "title": "历史 AI 判断记录不完整",
                "summary": "已保留入选结果，但缺少部分未入选记录",
                "recovery_capability": "none",
            }
        )
    else:
        issues.append(
            {
                "issue_code": "legacy_judgement_unavailable",
                "category": "review",
                "scope_type": "run",
                "output_id": None,
                "issue_group_key": "legacy-review-evidence",
                "impact_level": "blocking",
                "title": "历史判断记录不可用",
                "summary": "空的历史 selection 不能证明正常没有成片",
                "recovery_capability": "continue_run",
            }
        )
    return ProjectResultIndexPlan(
        run_id=inspection.run_id,
        project_id=inspection.project_id,
        evidence_hash=inspection.evidence_hash,
        review_session_id=review_session_id,
        decisions=tuple(decisions),
        outputs=tuple(outputs),
        materials=tuple(materials),
        issues=tuple(issues),
        report=inspection.report,
    )


def apply_project_result_index_plan(
    repository: ProjectRepository,
    plan: ProjectResultIndexPlan,
    *,
    occurred_at: str | None = None,
    fault_injection: Callable[[str], None] | None = None,
) -> ProjectResultIndexApplyResult:
    """Atomically and idempotently apply one explicit, evidence-bound compatibility plan."""
    timestamp = normalize_utc(occurred_at)
    run = repository.get_run(plan.run_id)
    if run is None or run.project_id != plan.project_id:
        raise ValueError("index plan does not match a known Run")

    def inject(phase: str) -> None:
        if fault_injection is not None:
            fault_injection(phase)

    with repository.transaction():
        idempotency_scope = f"project_result_index:{plan.run_id}"
        existing = _one(
            repository.connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND request_id = ?",
                (idempotency_scope, plan.evidence_hash),
            )
        )
        if existing is not None:
            if existing["object_id"] != plan.run_id or existing["request_hash"] != plan.evidence_hash:
                raise ValueError("result_index_plan_conflict")
            result = repository.get_run_result(plan.run_id)
            return ProjectResultIndexApplyResult(
                run_id=plan.run_id,
                evidence_hash=plan.evidence_hash,
                applied=False,
                already_applied=True,
                result_type=result.result_type if result else None,
                output_count=len(repository.list_run_outputs(plan.run_id)),
                issue_count=len(repository.list_issues(run_id=plan.run_id)),
            )
        if repository.get_run_result(plan.run_id) is not None:
            raise ValueError("run already has a result")
        inject("before_objects")
        if plan.review_session_id is not None:
            repository.connection.execute(
                """INSERT INTO ai_review_sessions(
                     review_session_id, run_id, attempt_number, status, resource_ref, model_name,
                     strategy_version, config_revision, parameter_snapshot_json, format_version,
                     overall_summary, warnings_json, candidate_count, selected_count,
                     rejected_count, evidence_relative_path, evidence_sha256, started_at,
                     completed_at, validated_at, updated_at
                   ) VALUES (?, ?, 1, 'selected', 'legacy.analysis.default', 'legacy',
                     'indexed_v1', ?, '{}', 1, '历史结果索引', '[]', ?, ?, 0,
                     'selected_clips.json', ?, ?, ?, ?, ?)""",
                (
                    plan.review_session_id,
                    plan.run_id,
                    run.config_revision,
                    len(plan.decisions),
                    len(plan.decisions),
                    plan.evidence_hash,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            for item in plan.outputs:
                ready = item["status"] == "ready"
                repository.connection.execute(
                    """INSERT INTO run_outputs(
                         output_id, run_id, review_session_id, candidate_id, display_order,
                         status, storage_kind, relative_path, file_name, duration_ms, width,
                         height, container, video_codec, byte_size, generated_at, verified_at,
                         updated_at, error_code, error_summary
                       ) VALUES (?, ?, ?, ?, ?, ?, 'run_workspace_compat', ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?)""",
                    (
                        item["output_id"],
                        plan.run_id,
                        plan.review_session_id,
                        item["candidate_id"],
                        item["display_order"],
                        item["status"],
                        item["relative_path"],
                        item["file_name"],
                        item["duration_ms"],
                        item["width"],
                        item["height"],
                        item["container"],
                        item["video_codec"],
                        item["byte_size"],
                        timestamp if ready else None,
                        timestamp if ready else None,
                        timestamp,
                        "output_missing" if not ready else None,
                        "历史成片文件缺失" if not ready else None,
                    ),
                )
            for item in plan.decisions:
                repository.connection.execute(
                    """INSERT INTO candidate_decisions(
                         decision_id, review_session_id, run_id, candidate_id, decision, rank,
                         candidate_type, source_start_ms, source_end_ms, selected_start_ms,
                         selected_end_ms, remove_ranges_json, hook, core_value, reason,
                         rejection_reason_code, risks_json, transcript_excerpt, output_id
                       ) VALUES (?, ?, ?, ?, 'selected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        item["decision_id"],
                        plan.review_session_id,
                        plan.run_id,
                        item["candidate_id"],
                        item["rank"],
                        item["candidate_type"],
                        item["source_start_ms"],
                        item["source_end_ms"],
                        item["selected_start_ms"],
                        item["selected_end_ms"],
                        stable_json(item["remove_ranges"]),
                        item["hook"],
                        item["core_value"],
                        item["reason"],
                        stable_json(item["risks"]),
                        item["transcript_excerpt"],
                        item["output_id"],
                    ),
                )
            for item in plan.materials:
                repository.connection.execute(
                    """INSERT INTO output_materials(
                         material_id, output_id, title_candidates_json, preferred_title_id,
                         description, tags_json, generation_source, status, material_revision,
                         created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        item["material_id"],
                        item["output_id"],
                        stable_json(item["title_candidates"]),
                        item["preferred_title_id"],
                        item["description"],
                        stable_json(item["tags"]),
                        item["generation_source"],
                        item["status"],
                        timestamp,
                        timestamp,
                    ),
                )
            projected = project_result_projection(
                review_status="selected", decisions=plan.decisions, outputs=plan.outputs
            )
            repository.connection.execute(
                """INSERT INTO run_results(
                     run_id, review_session_id, result_type, candidate_count, selected_count,
                     rejected_count, available_output_count, failed_output_count,
                     total_duration_ms, overall_summary, warnings_json, format_version,
                     result_revision, source_kind, evidence_hash, completed_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '历史结果索引', '[]', 1, 1,
                     'indexed_v1', ?, ?, ?)""",
                (
                    plan.run_id,
                    plan.review_session_id,
                    projected.result_type,
                    projected.candidate_count,
                    projected.selected_count,
                    projected.rejected_count,
                    projected.available_output_count,
                    projected.failed_output_count,
                    projected.total_duration_ms,
                    plan.evidence_hash,
                    timestamp,
                    timestamp,
                ),
            )
            repository.connection.execute(
                """UPDATE runs SET status = ?, completed_at = ?, updated_at = ?,
                     error_code = ?, error_summary = ? WHERE run_id = ?""",
                (
                    "failed" if projected.result_type == "unavailable" else "completed",
                    None if projected.result_type == "unavailable" else timestamp,
                    timestamp,
                    "result_unavailable" if projected.result_type == "unavailable" else None,
                    "历史结果不可用" if projected.result_type == "unavailable" else None,
                    plan.run_id,
                ),
            )
        inject("before_issues")
        for item in plan.issues:
            repository._discover_issue_in_transaction(  # noqa: SLF001 - one aggregate transaction.
                issue_code=item["issue_code"],
                category=item["category"],
                scope_type=item["scope_type"],
                project_id=plan.project_id,
                run_id=plan.run_id,
                output_id=item["output_id"],
                material_id=None,
                issue_group_key=item["issue_group_key"],
                status="action_required",
                impact_level=item["impact_level"],
                title=item["title"],
                summary=item["summary"],
                impact="历史结果信息不完整",
                preserved_content="已验证的历史对象保持不变",
                next_step="打开问题详情，检查或恢复历史结果",
                recovery_capability=item["recovery_capability"],
                occurred_at=timestamp,
                issue_id=legacy_id(
                    plan.evidence_hash,
                    f"issue:{plan.run_id}:{item['issue_code']}:{item.get('output_id') or 'run'}",
                ),
            )
        repository.connection.execute(
            """INSERT INTO idempotency_keys(
                 scope, request_id, request_hash, object_type, object_id, created_at
               ) VALUES (?, ?, ?, 'run_result', ?, ?)""",
            (idempotency_scope, plan.evidence_hash, plan.evidence_hash, plan.run_id, timestamp),
        )
        inject("before_commit")
    result = repository.get_run_result(plan.run_id)
    return ProjectResultIndexApplyResult(
        run_id=plan.run_id,
        evidence_hash=plan.evidence_hash,
        applied=True,
        already_applied=False,
        result_type=result.result_type if result else None,
        output_count=len(repository.list_run_outputs(plan.run_id)),
        issue_count=len(repository.list_issues(run_id=plan.run_id)),
    )
