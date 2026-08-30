from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path

import pytest

from live_clipper import asr_models, jobs
from live_clipper import web as web_module
from live_clipper.web import WebPaths, handle_api_request

MODEL_ID = "mlx-community/whisper-large-v3-turbo"
SMALL_MODEL_ID = "mlx-community/whisper-small-mlx-q4"
MEDIUM_MODEL_ID = "mlx-community/whisper-medium-mlx-q4"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))


@pytest.fixture
def small_registry(monkeypatch):
    files = {
        "config.json": b'{"model":"test"}',
        "weights.safetensors": b"weights-for-tests",
    }
    entry = deepcopy(asr_models.model_entry(MODEL_ID))
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


def test_registry_pins_three_model_matrix():
    assert [entry["id"] for entry in asr_models.REGISTRY] == [
        SMALL_MODEL_ID,
        MEDIUM_MODEL_ID,
        MODEL_ID,
    ]
    expected = {
        SMALL_MODEL_ID: {
            "tier": "light",
            "tier_label": "轻量",
            "hf_revision": "cd85bf0648ec125b9cae1eb6b617a41e58721704",
            "ms_revision": "fbd894a9ff818d41c663a36ade75b068776925cf",
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
            "total": 196_537_691,
        },
        MEDIUM_MODEL_ID: {
            "tier": "balanced",
            "tier_label": "平衡",
            "hf_revision": "1b8a6ee7f882cb5ec97d7e93fee4b7f22405bf87",
            "ms_revision": "011c90813369d9c15bfd3c7aaa7ce412f4724a70",
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
            "total": 512_230_981,
        },
        MODEL_ID: {
            "tier": "high_accuracy",
            "tier_label": "高精度",
            "hf_revision": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            "ms_revision": "bf7cb825f64339244fffda3a5c514db6493a6ee8",
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
            "total": 1_613_977_880,
        },
    }

    for entry in asr_models.REGISTRY:
        contract = expected[entry["id"]]
        assert entry["backend"] == "mlx_whisper"
        assert entry["tier"] == contract["tier"]
        assert entry["tier_label"] == contract["tier_label"]
        assert entry["recommended"] is (entry["id"] == MEDIUM_MODEL_ID)
        assert set(entry["sources"]) == {"modelscope", "huggingface"}
        assert entry["sources"]["huggingface"]["revision"] == contract["hf_revision"]
        assert entry["sources"]["modelscope"]["revision"] == contract["ms_revision"]
        assert entry["files"] == contract["files"]
        assert asr_models._total_bytes(entry) == contract["total"]

    registry_text = json.dumps(asr_models.REGISTRY)
    for forbidden in ["Qwen3", "whisper-tiny", "whisper-base", "8bit"]:
        assert forbidden not in registry_text


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
        "current",
    ]:
        assert field in item
    assert item["download_source"] == "modelscope"
    assert payload["current_backend"] == "mlx_whisper"
    assert payload["current_model"] == MODEL_ID
    assert item["current"] is True

    _install_with_fake_hf(monkeypatch, files)
    assert asr_models.list_models(paths.service_dir)[0]["state"] == "installed"
    (asr_models.install_dir(MODEL_ID) / "config.json").unlink()
    assert asr_models.list_models(paths.service_dir)[0]["state"] == "damaged"
    monkeypatch.setattr(jobs, "active_job_for", lambda *args: {"id": "active"})
    downloading = asr_models.list_models(paths.service_dir)[0]
    assert downloading["state"] == "downloading"
    assert downloading["downloading"] is True
    assert downloading["installed"] is False


def test_get_api_marks_exactly_one_current_model_and_none_for_openai(tmp_path):
    paths = _paths(tmp_path)
    paths.config_path.write_text(
        f'[asr]\nbackend = "mlx_whisper"\nmodel = "{MEDIUM_MODEL_ID}"\n',
        encoding="utf-8",
    )

    status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)

    assert status == 200
    assert payload["current_backend"] == "mlx_whisper"
    assert payload["current_model"] == MEDIUM_MODEL_ID
    assert [item["id"] for item in payload["models"] if item["current"]] == [MEDIUM_MODEL_ID]

    paths.config_path.write_text(
        '[asr]\nbackend = "openai"\nmodel = "whisper-1"\n',
        encoding="utf-8",
    )
    _status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)
    assert payload["current_backend"] == "openai"
    assert payload["current_model"] == "whisper-1"
    assert not any(item["current"] for item in payload["models"])


def test_current_model_remains_marked_when_missing_or_damaged(tmp_path, small_registry):
    paths = _paths(tmp_path)
    paths.config_path.write_text(
        f'[asr]\nbackend = "mlx_whisper"\nmodel = "{MODEL_ID}"\n',
        encoding="utf-8",
    )

    _status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)
    assert payload["models"][0]["state"] == "not_installed"
    assert payload["models"][0]["current"] is True

    asr_models.install_dir(MODEL_ID).mkdir(parents=True)
    _status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)
    assert payload["models"][0]["state"] == "damaged"
    assert payload["models"][0]["current"] is True


def test_select_rejects_unknown_not_ready_and_damaged_models(tmp_path, small_registry):
    paths = _paths(tmp_path)
    paths.config_path.write_text("", encoding="utf-8")

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": "unknown/model"},
    )
    assert status == 400
    assert payload["error_code"] == "unknown_model"

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": MODEL_ID},
    )
    assert status == 409
    assert payload["error_code"] == "model_not_ready"

    asr_models.install_dir(MODEL_ID).mkdir(parents=True)
    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": MODEL_ID},
    )
    assert status == 409
    assert payload["error_code"] == "model_not_ready"


@pytest.mark.parametrize("variable", ["ASR_MODEL", "ASR_BACKEND"])
def test_select_rejects_environment_override_without_writing(
    monkeypatch,
    tmp_path,
    small_registry,
    variable,
):
    monkeypatch.chdir(tmp_path)
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    original = '[asr]\nbackend = "openai"\nmodel = "whisper-1"\n'
    paths.config_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv(variable, "forced-value")

    try:
        status, _headers, payload = handle_api_request(
            "POST",
            "/api/asr/models/select",
            paths,
            body={"model": MODEL_ID},
        )
    finally:
        os.environ.pop("ASR_MODEL", None)
        os.environ.pop("ASR_BACKEND", None)

    assert status == 409
    assert payload["error_code"] == "asr_overridden_by_environment"
    assert paths.config_path.read_text(encoding="utf-8") == original


def test_select_rejects_override_loaded_from_cwd_dotenv(monkeypatch, tmp_path, small_registry):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASR_MODEL", raising=False)
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    (tmp_path / ".env").write_text(f"ASR_MODEL={MODEL_ID}\n", encoding="utf-8")
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    original = '[asr]\nbackend = "openai"\nmodel = "whisper-1"\n'
    paths.config_path.write_text(original, encoding="utf-8")

    try:
        status, _headers, payload = handle_api_request(
            "POST",
            "/api/asr/models/select",
            paths,
            body={"model": MODEL_ID},
        )
    finally:
        os.environ.pop("ASR_MODEL", None)
        os.environ.pop("ASR_BACKEND", None)

    assert status == 409
    assert payload["error_code"] == "asr_overridden_by_environment"
    assert paths.config_path.read_text(encoding="utf-8") == original


def test_select_healthy_model_saves_both_fields_and_reloads(monkeypatch, tmp_path, small_registry):
    monkeypatch.chdir(tmp_path)
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    paths.config_path.write_text(
        '[asr]\nbackend = "openai"\nmodel = "whisper-1"\nmodel_source = "modelscope"\n',
        encoding="utf-8",
    )
    reloads = []
    monkeypatch.setattr(
        web_module,
        "_restart_service_from_config",
        lambda received: reloads.append(received) or {
            "ok": True,
            "restarted": False,
            "reason": "service_not_running",
        },
    )

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["saved"] is True
    assert payload["current_backend"] == "mlx_whisper"
    assert payload["current_model"] == MODEL_ID
    assert payload["reload"]["reason"] == "service_not_running"
    assert reloads == [paths]
    written = paths.config_path.read_text(encoding="utf-8")
    assert 'backend = "mlx_whisper"' in written
    assert f'model = "{MODEL_ID}"' in written


def test_select_save_failure_keeps_current_and_skips_reload(monkeypatch, tmp_path, small_registry):
    monkeypatch.chdir(tmp_path)
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    original = '[asr]\nbackend = "openai"\nmodel = "whisper-1"\n'
    paths.config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        web_module.config_editor,
        "save_asr_model_selection",
        lambda *args, **kwargs: {"ok": False, "saved": False, "message": "write failed"},
    )
    monkeypatch.setattr(
        web_module,
        "_restart_service_from_config",
        lambda paths: pytest.fail("reload must not run after save failure"),
    )

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 400
    assert payload["error_code"] == "config_save_failed"
    assert payload["saved"] is False
    assert payload["current_backend"] == "openai"
    assert payload["current_model"] == "whisper-1"
    assert paths.config_path.read_text(encoding="utf-8") == original


def test_select_reload_failure_reports_saved_and_get_reflects_current(monkeypatch, tmp_path, small_registry):
    monkeypatch.chdir(tmp_path)
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    paths.config_path.write_text(
        '[asr]\nbackend = "openai"\nmodel = "whisper-1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        web_module,
        "_restart_service_from_config",
        lambda paths: {"ok": False, "error": "restart failed"},
    )

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/select",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 500
    assert payload["error_code"] == "service_reload_failed"
    assert payload["saved"] is True
    _status, _headers, current = handle_api_request("GET", "/api/asr/models", paths)
    assert current["current_backend"] == "mlx_whisper"
    assert current["current_model"] == MODEL_ID
    assert current["models"][0]["current"] is True


def test_delete_protects_current_model_and_allows_non_current(
    monkeypatch,
    tmp_path,
    small_registry,
):
    monkeypatch.chdir(tmp_path)
    _entry, files = small_registry
    _install_with_fake_hf(monkeypatch, files)
    paths = _paths(tmp_path)
    paths.config_path.write_text(
        f'[asr]\nbackend = "mlx_whisper"\nmodel = "{MODEL_ID}"\n',
        encoding="utf-8",
    )

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/delete",
        paths,
        body={"model": MODEL_ID},
    )
    assert status == 409
    assert payload["error_code"] == "current_model_in_use"
    assert asr_models.install_dir(MODEL_ID).is_dir()

    paths.config_path.write_text(
        f'[asr]\nbackend = "openai"\nmodel = "{MODEL_ID}"\n',
        encoding="utf-8",
    )
    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/delete",
        paths,
        body={"model": MODEL_ID},
    )
    assert status == 200
    assert payload["removed"] is True
    assert not asr_models.install_dir(MODEL_ID).exists()


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
