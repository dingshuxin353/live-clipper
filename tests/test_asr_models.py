from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from live_clipper import asr_models, config_editor, jobs
from live_clipper.config import DEFAULT_CONFIG_TEMPLATE
from live_clipper.web import WebPaths, handle_api_request

MODEL_ID = "mlx-community/whisper-large-v3-turbo"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))


class _FakeResponse:
    def __init__(self, *, json_data=None, chunks=None):
        self._json = json_data
        self._chunks = chunks or []

    def raise_for_status(self):
        return None

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_requests(monkeypatch, files):
    def fake_get(url, **kwargs):
        if "/api/models/" in url:
            siblings = [{"rfilename": name, "size": len(data)} for name, data in files.items()]
            siblings.append({"rfilename": ".gitattributes", "size": 10})
            return _FakeResponse(json_data={"siblings": siblings})
        name = url.rsplit("/resolve/main/", 1)[1]
        return _FakeResponse(chunks=[files[name]])

    monkeypatch.setattr(asr_models.requests, "get", fake_get)


def test_download_model_installs_into_models_root(monkeypatch):
    _fake_requests(monkeypatch, {"config.json": b"{}", "weights.npz": b"x" * 32})
    result = asr_models.download_model(MODEL_ID)
    assert result["ok"] is True
    assert result["status"] == "installed"
    target = asr_models.install_dir(MODEL_ID)
    assert (target / "config.json").is_file()
    assert (target / "weights.npz").read_bytes() == b"x" * 32
    assert not (target / ".gitattributes").exists()
    assert not asr_models.partial_dir(MODEL_ID).exists()
    assert asr_models.local_path_for(MODEL_ID) == target


def test_download_model_skips_when_installed():
    asr_models.install_dir(MODEL_ID).mkdir(parents=True)
    result = asr_models.download_model(MODEL_ID)
    assert result["ok"] is True
    assert result["status"] == "already_installed"


def test_download_model_rejects_unknown_model():
    with pytest.raises(ValueError):
        asr_models.download_model("evil/repo")


def test_delete_model_removes_dirs():
    asr_models.install_dir(MODEL_ID).mkdir(parents=True)
    result = asr_models.delete_model(MODEL_ID)
    assert result["removed"] is True
    assert asr_models.local_path_for(MODEL_ID) is None


def test_list_models_reports_partial_progress(tmp_path):
    staging = asr_models.partial_dir(MODEL_ID)
    staging.mkdir(parents=True)
    (staging / "_manifest.json").write_text(json.dumps({"total_bytes": 100}), encoding="utf-8")
    (staging / "weights.npz").write_bytes(b"x" * 40)
    items = asr_models.list_models(tmp_path / "service")
    entry = next(item for item in items if item["id"] == MODEL_ID)
    assert entry["installed"] is False
    assert entry["bytes_total"] == 100
    assert entry["bytes_downloaded"] >= 40


def _paths(tmp_path: Path) -> WebPaths:
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "work" / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def test_get_api_asr_models_lists_registry(tmp_path):
    paths = _paths(tmp_path)

    status, _headers, payload = handle_api_request("GET", "/api/asr/models", paths)

    assert status == 200
    assert payload["ok"] is True
    entry = next(item for item in payload["models"] if item["id"] == MODEL_ID)
    assert entry["installed"] is False
    assert payload["download_source"] == "huggingface"


def test_post_api_asr_model_download_rejects_unknown_model(tmp_path):
    status, _headers, _payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        _paths(tmp_path),
        body={"model": "evil/repo"},
    )

    assert status == 400


def test_post_api_asr_model_download_uses_configured_source(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    paths.config_path.write_text('[asr]\nmodel_source = "hf-mirror"\n', encoding="utf-8")
    sources = []

    def fake_download(model_id, source="huggingface"):
        sources.append(source)
        return {"ok": True, "model": model_id, "status": "installed"}

    monkeypatch.setattr(asr_models, "download_model", fake_download)

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/download",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 202
    job_id = payload["job"]["id"]
    deadline = time.time() + 5.0
    final_job = None
    while time.time() < deadline:
        final_job = jobs.read_job(paths.service_dir, job_id)
        if final_job and final_job["status"] in jobs.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    assert final_job is not None
    assert final_job["status"] == "succeeded"
    assert sources == ["hf-mirror"]


def test_post_api_asr_model_delete_removes_install(tmp_path):
    paths = _paths(tmp_path)
    asr_models.install_dir(MODEL_ID).mkdir(parents=True)

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/asr/models/delete",
        paths,
        body={"model": MODEL_ID},
    )

    assert status == 200
    assert payload["removed"] is True
    assert not asr_models.install_dir(MODEL_ID).exists()


def test_model_source_round_trips_for_legacy_config(tmp_path):
    config_path = tmp_path / "live-clipper.toml"
    config_path.write_text(
        DEFAULT_CONFIG_TEMPLATE.replace('model_source = "huggingface"\n', ""),
        encoding="utf-8",
    )

    loaded = config_editor.load_editable_config(config_path=config_path)
    assert loaded["config"]["asr"]["model_source"] == "huggingface"

    draft = loaded["config"]
    draft["asr"]["model_source"] = "hf-mirror"
    saved = config_editor.save_editable_config(
        draft,
        config_path=config_path,
        backup_root=tmp_path / "work" / "config_backups",
        base_dir=tmp_path,
    )

    assert saved["ok"] is True
    reloaded = config_editor.load_editable_config(config_path=config_path)
    assert reloaded["config"]["asr"]["model_source"] == "hf-mirror"
    assert 'model_source = "hf-mirror"' in config_path.read_text(encoding="utf-8")
