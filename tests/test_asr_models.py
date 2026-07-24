from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

from live_clipper import asr_models, jobs
from live_clipper.web import WebPaths, handle_api_request

MODEL_ID = "mlx-community/whisper-large-v3-turbo"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))


@pytest.fixture
def small_registry(monkeypatch):
    files = {
        "config.json": b'{"model":"test"}',
        "weights.safetensors": b"weights-for-tests",
    }
    entry = deepcopy(asr_models.REGISTRY[0])
    entry["files"] = [
        {
            "path": path,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in files.items()
    ]
    monkeypatch.setattr(asr_models, "REGISTRY", [entry])
    asr_models._HASH_CACHE.clear()
    return entry, files


def _paths(tmp_path: Path) -> WebPaths:
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "work" / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def _wait_for_job(service_dir: Path, job_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = jobs.read_job(service_dir, job_id)
        if job and job["status"] in jobs.TERMINAL_STATUSES:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {job_id}")


def _install_with_fake_hf(monkeypatch, files, *, source="huggingface", extra_cache=False):
    calls = []

    def fake_hf_hub_download(**kwargs):
        calls.append(kwargs)
        destination = Path(kwargs["local_dir"]) / kwargs["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(files[kwargs["filename"]])
        if extra_cache:
            cache_file = Path(kwargs["local_dir"]) / ".cache" / "huggingface" / "download" / "metadata.lock"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("sdk cache", encoding="utf-8")
        return str(destination)

    monkeypatch.setattr(asr_models, "hf_hub_download", fake_hf_hub_download)
    result = asr_models.download_model(MODEL_ID, source)
    return result, calls


def test_registry_pins_large_model_artifacts():
    entry = asr_models.model_entry(MODEL_ID)

    assert entry["backend"] == "mlx_whisper"
    assert entry["tier"] == "high_accuracy"
    assert entry["sources"]["huggingface"]["revision"] == "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
    assert entry["sources"]["modelscope"]["revision"] == "bf7cb825f64339244fffda3a5c514db6493a6ee8"
    assert set(entry["sources"]) == {"modelscope", "huggingface"}
    assert entry["files"] == [
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
    ]
    assert asr_models._total_bytes(entry) == 1_613_977_880


def test_two_sources_route_to_pinned_sdk_contract(monkeypatch, small_registry):
    entry, files = small_registry
    hf_calls = []
    ms_calls = []

    def fake_hf_hub_download(**kwargs):
        hf_calls.append(kwargs)
        destination = Path(kwargs["local_dir"]) / kwargs["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(files[kwargs["filename"]])
        return str(destination)

    class FakeHubApi:
        def __init__(self, *, endpoint):
            self.endpoint = endpoint

        def download_file(
            self,
            repo_id,
            repo_type,
            file_path,
            *,
            revision,
            local_dir,
            expected_sha256,
        ):
            ms_calls.append({
                "endpoint": self.endpoint,
                "repo_id": repo_id,
                "repo_type": repo_type,
                "file_path": file_path,
                "revision": revision,
                "expected_sha256": expected_sha256,
            })
            destination = Path(local_dir) / file_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(files[file_path])
            return destination

    monkeypatch.setattr(asr_models, "hf_hub_download", fake_hf_hub_download)
    monkeypatch.setattr(asr_models, "HubApi", FakeHubApi)

    asr_models.download_model(MODEL_ID, "modelscope")
    asr_models.delete_model(MODEL_ID)
    asr_models.download_model(MODEL_ID, "huggingface")

    assert {call["endpoint"] for call in ms_calls} == {entry["sources"]["modelscope"]["endpoint"]}
    assert {call["revision"] for call in ms_calls} == {entry["sources"]["modelscope"]["revision"]}
    assert {call["expected_sha256"] for call in ms_calls} == {file["sha256"] for file in entry["files"]}
    assert {call["endpoint"] for call in hf_calls} == {entry["sources"]["huggingface"]["endpoint"]}
    assert {call["revision"] for call in hf_calls} == {entry["sources"]["huggingface"]["revision"]}


def test_unknown_and_removed_sources_do_not_fall_back(monkeypatch, small_registry):
    network_calls = []
    monkeypatch.setattr(asr_models, "hf_hub_download", lambda **kwargs: network_calls.append(kwargs))
    monkeypatch.setattr(asr_models, "HubApi", lambda **kwargs: network_calls.append(kwargs))

    with pytest.raises(ValueError, match="未知模型下载源"):
        asr_models.download_model(MODEL_ID, "unknown")
    with pytest.raises(ValueError, match=asr_models.HF_MIRROR_REMOVED_MESSAGE):
        asr_models.download_model(MODEL_ID, "hf-mirror")
    assert network_calls == []


def test_download_failure_keeps_partial_and_last_error(monkeypatch, small_registry):
    _entry, files = small_registry

    def failing_download(**kwargs):
        staging = Path(kwargs["local_dir"])
        incomplete = staging / ".cache" / "huggingface" / "download" / "weights.incomplete"
        incomplete.parent.mkdir(parents=True, exist_ok=True)
        incomplete.write_bytes(b"partial")
        raise RuntimeError("network interrupted")

    monkeypatch.setattr(asr_models, "hf_hub_download", failing_download)

    with pytest.raises(RuntimeError, match="network interrupted"):
        asr_models.download_model(MODEL_ID, "huggingface")

    staging = asr_models.partial_dir(MODEL_ID)
    assert staging.is_dir()
    metadata = json.loads((staging / "_download.json").read_text(encoding="utf-8"))
    assert metadata["last_error"] == "network interrupted"
    assert metadata["source"] == "huggingface"
    assert asr_models._partial_bytes(staging, asr_models.model_entry(MODEL_ID)) >= len(b"partial")
    assert not asr_models.install_dir(MODEL_ID).exists()
    assert files


def test_modelscope_retry_reuses_incomplete_file(monkeypatch, small_registry):
    _entry, files = small_registry
    attempts = []

    class FakeHubApi:
        def __init__(self, *, endpoint):
            self.endpoint = endpoint

        def download_file(
            self,
            repo_id,
            repo_type,
            file_path,
            *,
            revision,
            local_dir,
            expected_sha256,
        ):
            destination = Path(local_dir) / file_path
            incomplete = destination.with_suffix(destination.suffix + ".incomplete")
            attempts.append((file_path, incomplete.exists()))
            if len(attempts) == 1:
                incomplete.parent.mkdir(parents=True, exist_ok=True)
                incomplete.write_bytes(b"partial")
                raise RuntimeError("interrupted")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(files[file_path])
            return destination

    monkeypatch.setattr(asr_models, "HubApi", FakeHubApi)
    with pytest.raises(RuntimeError):
        asr_models.download_model(MODEL_ID, "modelscope")
    result = asr_models.download_model(MODEL_ID, "modelscope")

    assert result["ok"] is True
    assert attempts[1] == ("config.json", True)


def test_legacy_hf_mirror_partial_reuses_complete_file_with_modelscope(monkeypatch, small_registry):
    _entry, files = small_registry
    staging = asr_models.partial_dir(MODEL_ID)
    staging.mkdir(parents=True)
    (staging / "config.json").write_bytes(files["config.json"])
    (staging / "_download.json").write_text(
        json.dumps({"source": "hf-mirror", "last_error": "legacy failure"}),
        encoding="utf-8",
    )
    assert asr_models.list_models(Path("service"))[0]["last_source"] == "hf-mirror"
    calls = []

    class FakeHubApi:
        def __init__(self, *, endpoint):
            self.endpoint = endpoint

        def download_file(
            self,
            repo_id,
            repo_type,
            file_path,
            *,
            revision,
            local_dir,
            expected_sha256,
        ):
            calls.append(file_path)
            destination = Path(local_dir) / file_path
            destination.write_bytes(files[file_path])
            return destination

    monkeypatch.setattr(asr_models, "HubApi", FakeHubApi)
    result = asr_models.download_model(MODEL_ID, "modelscope")

    assert result["ok"] is True
    assert calls == ["weights.safetensors"]
    install_metadata = json.loads(
        (asr_models.install_dir(MODEL_ID) / "_install.json").read_text(encoding="utf-8")
    )
    assert install_metadata["source"] == "modelscope"


def test_sha_mismatch_never_installs(monkeypatch, small_registry):
    _entry, files = small_registry

    def corrupt_download(**kwargs):
        destination = Path(kwargs["local_dir"]) / kwargs["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * len(files[kwargs["filename"]]))
        return str(destination)

    monkeypatch.setattr(asr_models, "hf_hub_download", corrupt_download)
    with pytest.raises(ValueError, match="SHA256"):
        asr_models.download_model(MODEL_ID, "huggingface")

    assert asr_models.partial_dir(MODEL_ID).is_dir()
    assert not asr_models.install_dir(MODEL_ID).exists()


def test_normal_install_writes_manifest_and_removes_sdk_cache(monkeypatch, small_registry):
    entry, files = small_registry
    result, _calls = _install_with_fake_hf(monkeypatch, files, extra_cache=True)
    target = asr_models.install_dir(MODEL_ID)

    assert result == {
        "ok": True,
        "model": MODEL_ID,
        "status": "installed",
        "source": "huggingface",
        "total_bytes": asr_models._total_bytes(entry),
    }
    assert {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()} == {
        "config.json",
        "weights.safetensors",
        "_install.json",
    }
    manifest = json.loads((target / "_install.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "huggingface"
    assert all(isinstance(file["mtime_ns"], int) for file in manifest["files"])
    assert asr_models.local_path_for(MODEL_ID) == target


def test_legacy_hf_mirror_install_source_remains_historical_metadata(monkeypatch, small_registry):
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    manifest_path = asr_models.install_dir(MODEL_ID) / "_install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"] = "hf-mirror"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    item = asr_models.list_models(Path("service"))[0]

    assert item["state"] == "installed"
    assert item["last_source"] == "hf-mirror"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["source"] == "hf-mirror"


@pytest.mark.parametrize("damage", ["missing", "size", "mtime_hash"])
def test_damaged_install_is_not_loadable(monkeypatch, small_registry, damage):
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    target = asr_models.install_dir(MODEL_ID)
    weights = target / "weights.safetensors"
    if damage == "missing":
        weights.unlink()
    elif damage == "size":
        weights.write_bytes(b"short")
    else:
        stat = weights.stat()
        weights.write_bytes(b"x" * len(files["weights.safetensors"]))
        assert weights.stat().st_mtime_ns != stat.st_mtime_ns
    asr_models._HASH_CACHE.clear()

    item = asr_models.list_models(Path("service"))[0]
    assert item["state"] == "damaged"
    assert item["installed"] is False
    assert asr_models.local_path_for(MODEL_ID) is None


def test_repair_reuses_healthy_file_and_restores_install(monkeypatch, small_registry):
    _entry, files = small_registry
    target = asr_models.install_dir(MODEL_ID)
    target.mkdir(parents=True)
    (target / "config.json").write_bytes(files["config.json"])
    (target / "weights.safetensors").write_bytes(b"broken")
    assert asr_models.list_models(Path("service"))[0]["state"] == "damaged"
    assert asr_models.local_path_for(MODEL_ID) is None
    calls = []

    def fake_hf_hub_download(**kwargs):
        calls.append(kwargs["filename"])
        destination = Path(kwargs["local_dir"]) / kwargs["filename"]
        destination.write_bytes(files[kwargs["filename"]])
        return str(destination)

    monkeypatch.setattr(asr_models, "hf_hub_download", fake_hf_hub_download)
    result = asr_models.download_model(MODEL_ID, "huggingface")

    assert result["ok"] is True
    assert calls == ["weights.safetensors"]
    assert asr_models.local_path_for(MODEL_ID) == target
    assert not target.with_name(target.name + ".damaged-backup").exists()


def test_delete_api_returns_409_while_download_active(monkeypatch, tmp_path, small_registry):
    paths = _paths(tmp_path)
    monkeypatch.setattr(jobs, "active_job_for", lambda *args: {"id": "active"})

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/delete",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 409
    assert payload["error_code"] == "model_download_active"


def test_download_job_success_result_contains_ok(monkeypatch, tmp_path, small_registry):
    paths = _paths(tmp_path)
    paths.config_path.write_text('[asr]\nmodel_source = "huggingface"\n', encoding="utf-8")
    monkeypatch.setattr(
        asr_models,
        "download_model",
        lambda model_id, source: {"ok": True, "model": model_id, "source": source},
    )

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        paths,
        body={"model": MODEL_ID},
    )
    job = _wait_for_job(paths.service_dir, payload["job"]["id"])

    assert status == 202
    assert job["status"] == "succeeded"
    assert job["result"]["ok"] is True
    assert job["result"]["source"] == "huggingface"


def test_get_api_exposes_four_states_and_status_fields(monkeypatch, tmp_path, small_registry):
    _entry, files = small_registry
    paths = _paths(tmp_path)
    paths.config_path.write_text("", encoding="utf-8")

    status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)
    item = payload["models"][0]
    assert status == 200
    assert item["state"] == "not_installed"
    for field in [
        "state_reason",
        "partial_bytes",
        "download_source",
        "last_source",
        "last_error",
        "backend",
        "tier",
        "installed",
        "downloading",
    ]:
        assert field in item
    assert item["download_source"] == "modelscope"

    _install_with_fake_hf(monkeypatch, files)
    assert asr_models.list_models(paths.service_dir)[0]["state"] == "installed"
    (asr_models.install_dir(MODEL_ID) / "config.json").unlink()
    assert asr_models.list_models(paths.service_dir)[0]["state"] == "damaged"
    monkeypatch.setattr(jobs, "active_job_for", lambda *args: {"id": "active"})
    downloading = asr_models.list_models(paths.service_dir)[0]
    assert downloading["state"] == "downloading"
    assert downloading["downloading"] is True
    assert downloading["installed"] is False


def test_download_api_rejects_unknown_model_and_source(monkeypatch, tmp_path, small_registry):
    paths = _paths(tmp_path)
    status, _headers, _payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        paths,
        body={"model": "evil/repo"},
    )
    assert status == 400

    paths.config_path.write_text('[asr]\nmodel_source = "invalid"\n', encoding="utf-8")
    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        paths,
        body={"model": MODEL_ID},
    )
    assert status == 400
    assert payload["error_code"] == "unknown_model_source"

    monkeypatch.setattr(
        jobs,
        "start_job",
        lambda *args, **kwargs: pytest.fail("removed source must not start a network job"),
    )
    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        paths,
        body={"model": MODEL_ID, "source": "hf-mirror"},
    )
    assert status == 400
    assert payload["error_code"] == "unsupported_model_source"
    assert payload["message"] == asr_models.HF_MIRROR_REMOVED_MESSAGE
