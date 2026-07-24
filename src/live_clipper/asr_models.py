"""可恢复、可校验的本地 ASR 模型下载与安装。"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from huggingface_hub import hf_hub_download
from modelscope_hub import HubApi

from live_clipper import app_dirs, jobs
from live_clipper.utils import read_json, write_json

DOWNLOAD_JOB_KIND = "asr_model_download"
DOWNLOAD_SCHEMA_VERSION = 1
INSTALL_SCHEMA_VERSION = 1
DEFAULT_MODEL_SOURCE = "modelscope"
HF_MIRROR_REMOVED_MESSAGE = "HF Mirror 已停止支持，请选择 ModelScope 或 Hugging Face"

SOURCE_LABELS = {
    "modelscope": "ModelScope",
    "huggingface": "Hugging Face",
}

REGISTRY: list[dict[str, Any]] = [
    {
        "id": "mlx-community/whisper-small-mlx-q4",
        "display_name": "Whisper Small q4",
        "backend": "mlx_whisper",
        "tier": "light",
        "tier_label": "轻量",
        "size_note": "约 187 MiB",
        "ram_note": "预估内存占用较低",
        "speed_note": "预估速度较快",
        "accuracy_note": "适合轻量处理",
        "recommended": False,
        "sources": {
            "modelscope": {
                "repo": "mlx-community/whisper-small-mlx-q4",
                "revision": "fbd894a9ff818d41c663a36ade75b068776925cf",
                "endpoint": "https://modelscope.cn",
            },
            "huggingface": {
                "repo": "mlx-community/whisper-small-mlx-q4",
                "revision": "cd85bf0648ec125b9cae1eb6b617a41e58721704",
                "endpoint": "https://huggingface.co",
            },
        },
        "files": [
            {
                "path": "config.json",
                "bytes": 339,
                "sha256": "d414b27f911c1c416a90525a0f856e0dc1c9e38632a833ca8dd05c58b3d8a01a",
            },
            {
                "path": "weights.npz",
                "bytes": 196_537_352,
                "sha256": "ca6659298fe7550468ff0fc49dea7442615d9a53d1ce087aaded1b7627451998",
            },
        ],
    },
    {
        "id": "mlx-community/whisper-medium-mlx-q4",
        "display_name": "Whisper Medium q4",
        "backend": "mlx_whisper",
        "tier": "balanced",
        "tier_label": "平衡",
        "size_note": "约 489 MiB",
        "ram_note": "预估内存占用适中",
        "speed_note": "预估速度均衡",
        "accuracy_note": "兼顾速度与精度",
        "recommended": False,
        "sources": {
            "modelscope": {
                "repo": "mlx-community/whisper-medium-mlx-q4",
                "revision": "011c90813369d9c15bfd3c7aaa7ce412f4724a70",
                "endpoint": "https://modelscope.cn",
            },
            "huggingface": {
                "repo": "mlx-community/whisper-medium-mlx-q4",
                "revision": "1b8a6ee7f882cb5ec97d7e93fee4b7f22405bf87",
                "endpoint": "https://huggingface.co",
            },
        },
        "files": [
            {
                "path": "config.json",
                "bytes": 341,
                "sha256": "2cb3af0368f094edf1b2182f516f2cd2c3f36967d3246294203bee11bae72777",
            },
            {
                "path": "weights.npz",
                "bytes": 512_230_640,
                "sha256": "0d0d1c30691660c66ec3f4e559de7244495b359b38b112f9b7e824746e61aa50",
            },
        ],
    },
    {
        "id": "mlx-community/whisper-large-v3-turbo",
        "display_name": "Whisper Large V3 Turbo",
        "backend": "mlx_whisper",
        "tier": "high_accuracy",
        "tier_label": "高精度",
        "size_note": "约 1.6 GB",
        "ram_note": "预估约 4 GB 内存",
        "speed_note": "预估约 8 倍速",
        "accuracy_note": "高精度",
        "recommended": False,
        "sources": {
            "modelscope": {
                "repo": "mlx-community/whisper-large-v3-turbo",
                "revision": "bf7cb825f64339244fffda3a5c514db6493a6ee8",
                "endpoint": "https://modelscope.cn",
            },
            "huggingface": {
                "repo": "mlx-community/whisper-large-v3-turbo",
                "revision": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
                "endpoint": "https://huggingface.co",
            },
        },
        "files": [
            {
                "path": "config.json",
                "bytes": 268,
                "sha256": "b34fc29e4e11e0a25e812775dd67f4dd16fc2c8eb43d28ae25ff7d660ecb6379",
            },
            {
                "path": "weights.safetensors",
                "bytes": 1_613_977_612,
                "sha256": "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6",
            },
        ],
    },
]

_HASH_CACHE: dict[tuple[str, int, int, str], bool] = {}


def registry_ids() -> set[str]:
    return {entry["id"] for entry in REGISTRY}


def source_ids() -> set[str]:
    return set(SOURCE_LABELS)


def model_entry(model_id: str) -> dict[str, Any]:
    for entry in REGISTRY:
        if entry["id"] == model_id:
            return entry
    raise ValueError(f"未知模型: {model_id}")


def models_root() -> Path:
    return app_dirs.default_app_home() / "models"


def _dir_name(model_id: str) -> str:
    return model_id.replace("/", "--")


def install_dir(model_id: str) -> Path:
    return models_root() / _dir_name(model_id)


def partial_dir(model_id: str) -> Path:
    return models_root() / (_dir_name(model_id) + ".partial")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _total_bytes(entry: dict[str, Any]) -> int:
    return sum(int(file["bytes"]) for file in entry["files"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_matches(path: Path, expected: str) -> bool:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, expected)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = _sha256(path) == expected
    return _HASH_CACHE[key]


def _canonical_file_matches(path: Path, file_spec: dict[str, Any]) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == int(file_spec["bytes"])
        and _hash_matches(path, str(file_spec["sha256"]))
    )


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _installation_status(model_id: str) -> tuple[str, str | None]:
    entry = model_entry(model_id)
    target = install_dir(model_id)
    if not target.is_dir():
        return "not_installed", None
    manifest = _safe_read_json(target / "_install.json")
    if manifest is None:
        return "damaged", "缺少安装清单"
    if (
        manifest.get("schema_version") != INSTALL_SCHEMA_VERSION
        or manifest.get("model_id") != model_id
        or manifest.get("backend") != entry["backend"]
    ):
        return "damaged", "安装清单不匹配"
    manifest_files = {
        item.get("path"): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    allowed = {"_install.json", *(str(file["path"]) for file in entry["files"])}
    actual = {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()}
    if actual != allowed:
        return "damaged", "安装目录包含缺失或多余文件"
    for file_spec in entry["files"]:
        relative = str(file_spec["path"])
        path = target / relative
        recorded = manifest_files.get(relative)
        if recorded is None:
            return "damaged", f"安装清单缺少 {relative}"
        if (
            recorded.get("bytes") != int(file_spec["bytes"])
            or recorded.get("sha256") != file_spec["sha256"]
            or not path.is_file()
            or path.stat().st_size != int(file_spec["bytes"])
        ):
            return "damaged", f"{relative} 大小或清单不匹配"
        if recorded.get("mtime_ns") != path.stat().st_mtime_ns and not _hash_matches(path, str(file_spec["sha256"])):
            return "damaged", f"{relative} 校验失败"
    return "installed", None


def local_path_for(model_id: str) -> Path | None:
    """只有完整性快速校验通过的模型目录才可进入转写。"""
    if model_id not in registry_ids():
        return None
    state, _reason = _installation_status(model_id)
    return install_dir(model_id) if state == "installed" else None


def _download_metadata(entry: dict[str, Any], source: str, *, last_error: str | None) -> dict[str, Any]:
    source_spec = entry["sources"][source]
    return {
        "schema_version": DOWNLOAD_SCHEMA_VERSION,
        "model_id": entry["id"],
        "source": source,
        "repo": source_spec["repo"],
        "revision": source_spec["revision"],
        "files": [dict(file) for file in entry["files"]],
        "total_bytes": _total_bytes(entry),
        "last_error": last_error,
        "updated_at": _now(),
    }


def _write_download_metadata(staging: Path, entry: dict[str, Any], source: str, *, last_error: str | None) -> None:
    write_json(staging / "_download.json", _download_metadata(entry, source, last_error=last_error))


def _partial_bytes(staging: Path, entry: dict[str, Any]) -> int:
    if not staging.is_dir():
        return 0
    total = 0
    for file_spec in entry["files"]:
        path = staging / str(file_spec["path"])
        candidates = [path, path.with_suffix(path.suffix + ".incomplete")]
        total += max((candidate.stat().st_size for candidate in candidates if candidate.is_file()), default=0)
    hf_cache = staging / ".cache" / "huggingface" / "download"
    if hf_cache.is_dir():
        total += sum(path.stat().st_size for path in hf_cache.rglob("*.incomplete") if path.is_file())
    return min(total, _total_bytes(entry))


def _reuse_healthy_final_files(entry: dict[str, Any], target: Path, staging: Path) -> None:
    if not target.is_dir():
        return
    staging.mkdir(parents=True, exist_ok=True)
    for file_spec in entry["files"]:
        source = target / str(file_spec["path"])
        destination = staging / str(file_spec["path"])
        if not _canonical_file_matches(source, file_spec):
            continue
        if _canonical_file_matches(destination, file_spec):
            continue
        if destination.exists():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def _download_huggingface_file(entry: dict[str, Any], source: str, file_spec: dict[str, Any], staging: Path) -> Path:
    source_spec = entry["sources"][source]
    downloaded = hf_hub_download(
        repo_id=source_spec["repo"],
        filename=file_spec["path"],
        revision=source_spec["revision"],
        repo_type="model",
        local_dir=staging,
        endpoint=source_spec["endpoint"],
        token=os.getenv("HF_TOKEN") or None,
    )
    return Path(downloaded)


def _download_modelscope_file(entry: dict[str, Any], file_spec: dict[str, Any], staging: Path) -> Path:
    source_spec = entry["sources"]["modelscope"]
    api = HubApi(endpoint=source_spec["endpoint"])
    return Path(
        api.download_file(
            source_spec["repo"],
            "model",
            file_spec["path"],
            revision=source_spec["revision"],
            local_dir=staging,
            expected_sha256=file_spec["sha256"],
        )
    )


def _download_file(entry: dict[str, Any], source: str, file_spec: dict[str, Any], staging: Path) -> None:
    destination = staging / str(file_spec["path"])
    if destination.exists():
        destination.unlink()
    if source == "modelscope":
        downloaded = _download_modelscope_file(entry, file_spec, staging)
    else:
        downloaded = _download_huggingface_file(entry, source, file_spec, staging)
    if downloaded.resolve() != destination.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(downloaded, destination)


def _clean_staging_for_install(staging: Path, entry: dict[str, Any]) -> None:
    allowed = {str(file["path"]) for file in entry["files"]}
    for path in sorted(staging.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        relative = str(path.relative_to(staging))
        if path.is_file() and relative not in allowed:
            path.unlink()
        elif path.is_dir() and not any(value == relative or value.startswith(relative + "/") for value in allowed):
            shutil.rmtree(path, ignore_errors=True)


def _write_install_manifest(staging: Path, entry: dict[str, Any], source: str) -> None:
    source_spec = entry["sources"][source]
    files = []
    for file_spec in entry["files"]:
        path = staging / str(file_spec["path"])
        files.append({
            **file_spec,
            "mtime_ns": path.stat().st_mtime_ns,
        })
    write_json(
        staging / "_install.json",
        {
            "schema_version": INSTALL_SCHEMA_VERSION,
            "model_id": entry["id"],
            "backend": entry["backend"],
            "source": source,
            "repo": source_spec["repo"],
            "revision": source_spec["revision"],
            "files": files,
            "verified_at": _now(),
        },
    )


def _atomic_install(staging: Path, target: Path) -> None:
    if not target.exists():
        staging.rename(target)
        return
    backup = target.with_name(target.name + ".damaged-backup")
    if backup.exists():
        shutil.rmtree(backup)
    target.rename(backup)
    try:
        staging.rename(target)
    except Exception:
        backup.rename(target)
        raise
    shutil.rmtree(backup)


def download_model(model_id: str, source: str = DEFAULT_MODEL_SOURCE) -> dict[str, Any]:
    """下载或修复白名单模型；失败时保留 partial 与错误摘要。"""
    entry = model_entry(model_id)
    if source == "hf-mirror":
        raise ValueError(HF_MIRROR_REMOVED_MESSAGE)
    if source not in entry["sources"] or source not in source_ids():
        raise ValueError(f"未知模型下载源: {source}")
    state, _reason = _installation_status(model_id)
    if state == "installed":
        return {"ok": True, "model": model_id, "status": "already_installed"}

    target = install_dir(model_id)
    staging = partial_dir(model_id)
    staging.mkdir(parents=True, exist_ok=True)
    if state == "damaged":
        _reuse_healthy_final_files(entry, target, staging)
    _write_download_metadata(staging, entry, source, last_error=None)

    try:
        for file_spec in entry["files"]:
            path = staging / str(file_spec["path"])
            if _canonical_file_matches(path, file_spec):
                continue
            _download_file(entry, source, file_spec, staging)
            if not _canonical_file_matches(path, file_spec):
                raise ValueError(f"{file_spec['path']} SHA256 校验失败")
        for file_spec in entry["files"]:
            path = staging / str(file_spec["path"])
            if not _canonical_file_matches(path, file_spec):
                raise ValueError(f"{file_spec['path']} 最终完整性校验失败")
        _clean_staging_for_install(staging, entry)
        _write_install_manifest(staging, entry, source)
        _atomic_install(staging, target)
    except Exception as exc:
        staging.mkdir(parents=True, exist_ok=True)
        _write_download_metadata(staging, entry, source, last_error=str(exc))
        raise

    return {
        "ok": True,
        "model": model_id,
        "status": "installed",
        "source": source,
        "total_bytes": _total_bytes(entry),
    }


def delete_model(model_id: str, *, service_dir: Path | None = None) -> dict[str, Any]:
    model_entry(model_id)
    if service_dir is not None and jobs.active_job_for(service_dir, model_id, DOWNLOAD_JOB_KIND):
        raise RuntimeError("模型正在下载，不能删除")
    removed = False
    for path in (install_dir(model_id), partial_dir(model_id)):
        if path.exists():
            shutil.rmtree(path)
            removed = True
    return {"model": model_id, "removed": removed}


def list_models(
    service_dir: Path,
    *,
    download_source: str = DEFAULT_MODEL_SOURCE,
    current_backend: str | None = None,
    current_model: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in REGISTRY:
        model_id = entry["id"]
        state, reason = _installation_status(model_id)
        job = jobs.active_job_for(service_dir, model_id, DOWNLOAD_JOB_KIND)
        if job is not None:
            state = "downloading"
            reason = None
        staging = partial_dir(model_id)
        download_meta = _safe_read_json(staging / "_download.json") or {}
        install_meta = _safe_read_json(install_dir(model_id) / "_install.json") or {}
        partial_bytes = _partial_bytes(staging, entry)
        items.append(
            {
                **entry,
                "state": state,
                "state_reason": reason,
                "installed": state == "installed",
                "downloading": state == "downloading",
                "job_id": job["id"] if job else None,
                "installed_bytes": _total_bytes(entry) if state == "installed" else 0,
                "partial_bytes": partial_bytes,
                "bytes_downloaded": partial_bytes,
                "bytes_total": _total_bytes(entry),
                "download_source": download_source,
                "last_source": download_meta.get("source") or install_meta.get("source"),
                "last_error": download_meta.get("last_error"),
                "current": current_backend == entry["backend"] and current_model == model_id,
            }
        )
    return items
