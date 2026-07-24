"""本地 ASR 模型管理：内置注册表、按需下载（HF 官方/国内镜像）、安装检测、删除。

模型存放在应用数据目录下的 models/ 子目录（macOS 默认
~/Library/Application Support/Venus/models/），与安装包解耦：应用本体保持小体积，
模型由用户在设置页按需下载。下载先写入 <目录>.partial，全部完成后原子重命名为
最终目录——「最终目录存在」即「安装完成」，无需额外标记文件；前端进度 = partial
目录当前字节数 / 清单总字节数。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import requests

from live_clipper import app_dirs, jobs
from live_clipper.utils import read_json, write_json

# 下载源 → HF 兼容端点。hf-mirror.com 是 huggingface.co 的国内反向代理，
# /api/models 与 /resolve 路径完全同构。
MODEL_SOURCES: dict[str, str] = {
    "huggingface": "https://huggingface.co",
    "hf-mirror": "https://hf-mirror.com",
}

# 内置模型注册表。web 下载接口以此为白名单，只允许下载注册表内的模型。
# 新模型（qwen3-asr 等）经技术验证后按同样结构追加条目即可。
REGISTRY: list[dict[str, Any]] = [
    {
        "id": "mlx-community/whisper-large-v3-turbo",
        "display_name": "Whisper Large V3 Turbo",
        "size_note": "约 1.6 GB",
        "ram_note": "约 4 GB 内存",
        "speed_note": "约 8 倍速",
        "accuracy_note": "高精度",
        "recommended": True,
    },
]

DOWNLOAD_JOB_KIND = "asr_model_download"

# 对推理无用、下载时跳过的仓库文件
_SKIP_FILES = {".gitattributes"}


def registry_ids() -> set[str]:
    return {entry["id"] for entry in REGISTRY}


def models_root() -> Path:
    return app_dirs.default_app_home() / "models"


def _dir_name(model_id: str) -> str:
    return model_id.replace("/", "--")


def install_dir(model_id: str) -> Path:
    return models_root() / _dir_name(model_id)


def partial_dir(model_id: str) -> Path:
    return models_root() / (_dir_name(model_id) + ".partial")


def local_path_for(model_id: str) -> Path | None:
    """已安装则返回本地模型目录，否则 None。transcribe 用它决定传本地路径还是 repo 名。"""
    path = install_dir(model_id)
    return path if path.is_dir() else None


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _fetch_file_list(model_id: str, endpoint: str) -> list[dict[str, Any]]:
    response = requests.get(f"{endpoint}/api/models/{model_id}?blobs=true", timeout=60)
    response.raise_for_status()
    payload = response.json()
    siblings = payload.get("siblings") or []
    files: list[dict[str, Any]] = []
    for entry in siblings:
        name = entry.get("rfilename", "")
        if not name or name in _SKIP_FILES or name.startswith("."):
            continue
        files.append({"name": name, "size": int(entry.get("size") or 0)})
    if not files:
        raise RuntimeError(f"模型仓库文件列表为空: {model_id}")
    return files


def download_model(model_id: str, source: str = "huggingface") -> dict[str, Any]:
    """阻塞式下载，设计为在后台 job 线程里执行；返回值会被写入 job.result。"""
    if model_id not in registry_ids():
        raise ValueError(f"未知模型: {model_id}")
    endpoint = MODEL_SOURCES.get(source, MODEL_SOURCES["huggingface"])
    target = install_dir(model_id)
    if target.is_dir():
        return {"ok": True, "model": model_id, "status": "already_installed"}
    staging = partial_dir(model_id)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    files = _fetch_file_list(model_id, endpoint)
    total = sum(f["size"] for f in files)
    write_json(staging / "_manifest.json", {"total_bytes": total, "files": [f["name"] for f in files]})
    for entry in files:
        url = f"{endpoint}/{model_id}/resolve/main/{entry['name']}"
        dest = staging / entry["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=600) as response:
            response.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
    staging.rename(target)
    return {"ok": True, "model": model_id, "status": "installed", "total_bytes": total}


def delete_model(model_id: str) -> dict[str, Any]:
    if model_id not in registry_ids():
        raise ValueError(f"未知模型: {model_id}")
    removed = False
    for path in (install_dir(model_id), partial_dir(model_id)):
        if path.exists():
            shutil.rmtree(path)
            removed = True
    return {"model": model_id, "removed": removed}


def list_models(service_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in REGISTRY:
        model_id = entry["id"]
        installed = install_dir(model_id).is_dir()
        staging = partial_dir(model_id)
        job = jobs.active_job_for(service_dir, model_id, DOWNLOAD_JOB_KIND)
        downloading = job is not None
        bytes_total = 0
        bytes_downloaded = 0
        if downloading or staging.is_dir():
            manifest_path = staging / "_manifest.json"
            if manifest_path.is_file():
                try:
                    bytes_total = int(read_json(manifest_path).get("total_bytes") or 0)
                except Exception:
                    bytes_total = 0
            bytes_downloaded = _dir_size(staging)
        items.append(
            {
                **entry,
                "installed": installed,
                "installed_bytes": _dir_size(install_dir(model_id)) if installed else 0,
                "downloading": downloading,
                "job_id": job["id"] if job else None,
                "bytes_downloaded": bytes_downloaded,
                "bytes_total": bytes_total,
            }
        )
    return items
