from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .project_domain import assert_secret_free


@dataclass(frozen=True)
class ResourceOption:
    resource_id: str
    display_name: str
    resource_type: str
    ready: bool
    problem: str | None = None
    version: str | None = None


class ResourceUnavailableError(ValueError):
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id
        super().__init__(f"resource unavailable: {resource_id}")


def compatibility_resources(settings: Settings) -> tuple[ResourceOption, ...]:
    asr = settings.asr
    asr_ready = bool(asr and asr.model and (asr.backend != "openai" or settings.asr_api_key))
    analysis_ready = bool(settings.cheap_model_api_key and settings.cheap_model_name)
    resources = [
        ResourceOption(
            resource_id="legacy.asr.default",
            display_name="当前语音识别配置",
            resource_type="asr",
            ready=asr_ready,
            problem=None if asr_ready else "语音识别资源尚未就绪",
            version=str(asr.model) if asr else None,
        ),
        ResourceOption(
            resource_id="legacy.analysis.default",
            display_name="当前内容分析配置",
            resource_type="analysis",
            ready=analysis_ready,
            problem=None if analysis_ready else "内容分析资源尚未配置",
            version=settings.cheap_model_name,
        ),
    ]
    return tuple(resources)


def resource_map(settings: Settings) -> dict[str, ResourceOption]:
    return {resource.resource_id: resource for resource in compatibility_resources(settings)}


def resource_repair_context(settings: Settings, resource_id: str, *, issue_id: str) -> dict[str, Any]:
    resource = resource_map(settings).get(resource_id)
    if resource is None:
        raise KeyError(resource_id)
    inline = resource.resource_type == "analysis" and resource_id == "legacy.analysis.default"
    return {
        "resource_id": resource.resource_id,
        "display_name": resource.display_name,
        "resource_type": resource.resource_type,
        "api_base": settings.cheap_model_api_base if inline else None,
        "model": resource.version,
        "credential_state": "configured" if resource.ready else "missing",
        "repair_capability": "inline_connection" if inline else "settings_only",
        "settings_url": "/settings",
        "issue_id": issue_id,
    }


def resolve_parameter_snapshot(config: dict[str, Any], settings: Settings) -> dict[str, Any]:
    refs = config["resources"]
    available = resource_map(settings)
    required = [str(refs["asr_ref"]), str(refs["analysis_ref"])]
    if config.get("schema_version") == 2:
        required.append(str(refs["review_ref"]))
    if refs.get("arbitration_mode") != "reuse_analysis" and refs.get("arbitration_ref"):
        required.append(str(refs["arbitration_ref"]))
    for resource_id in required:
        resource = available.get(resource_id)
        if resource is None or not resource.ready:
            raise ResourceUnavailableError(resource_id)
    endpoint = urlsplit(str(settings.cheap_model_api_base or ""))
    endpoint_summary = f"{endpoint.scheme}://{endpoint.netloc}" if endpoint.scheme and endpoint.netloc else None
    snapshot = {
        "schema_version": int(config["schema_version"]),
        "resources": {
            "asr_ref": refs["asr_ref"],
            "analysis_ref": refs["analysis_ref"],
            "arbitration_mode": refs["arbitration_mode"],
            "arbitration_ref": refs["arbitration_ref"],
            "asr": {
                "backend": settings.asr.backend if settings.asr else None,
                "model": settings.asr.model if settings.asr else None,
                "language": settings.asr.language if settings.asr else None,
            },
            "analysis": {
                "provider": settings.llm.provider_label if settings.llm else None,
                "model": settings.cheap_model_name,
            },
        },
        "processing": dict(config["processing"]),
        "output": dict(config["output"]),
    }
    if config["schema_version"] == 2:
        snapshot["resources"]["review_ref"] = refs["review_ref"]
        snapshot["resources"]["review"] = {
            "provider": settings.llm.provider_label if settings.llm else "OpenAI-compatible LLM",
            "model": settings.review_automation.model.model or settings.cheap_model_name,
            "endpoint": endpoint_summary,
        }
        snapshot["retry_policy"] = {
            "version": "project_runtime_retry_v1",
            "ai": {"max_retries": 2, "delays_seconds": [30, 120]},
            "render": {"max_retries": 1, "delays_seconds": [30]},
        }
    assert_secret_free(snapshot)
    return snapshot
