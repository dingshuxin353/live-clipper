from __future__ import annotations

import re
from pathlib import Path

from live_clipper import onboarding
from live_clipper.config import write_default_config
from live_clipper.config_editor import load_editable_config
from live_clipper.web import WebPaths, handle_api_request


def _paths(tmp_path):
    return WebPaths(
        output_root=tmp_path / "output",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        service_dir=tmp_path / "service",
        config_path=tmp_path / "live-clipper.toml",
    )


def test_get_onboarding_needs_true_when_unconfigured(tmp_path):
    status, _headers, payload = handle_api_request("GET", "/api/onboarding", _paths(tmp_path))
    assert status == 200
    assert payload["needs_onboarding"] is True
    assert payload["initial_asr_mode"] == "local"
    assert payload["initial_local_model"] == "mlx-community/whisper-small-mlx-q4"


def test_post_onboarding_test_source(tmp_path):
    source = tmp_path / "recordings"
    source.mkdir()
    status, _headers, payload = handle_api_request(
        "POST", "/api/onboarding/test-source", _paths(tmp_path), body={"source_dir": str(source)}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["video_count"] == 0


def test_post_onboarding_complete_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = _paths(tmp_path)
    write_default_config(paths.config_path)
    source = tmp_path / "recordings"
    source.mkdir()

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/onboarding/complete",
        paths,
        body={
            "source_dir": str(source),
            "llm_api_base": "https://example.test/v1",
            "llm_model": "test-model",
            "llm_api_key": "sk-llm-test",
            "asr_mode": "cloud",
            "asr_api_base": "https://asr.example.test/v1",
            "asr_model": "whisper-1",
            "asr_api_key": "sk-asr-test",
        },
    )
    assert status == 200
    assert payload["ok"] is True

    status, _headers, payload = handle_api_request("GET", "/api/onboarding", paths)
    assert status == 200
    assert payload["needs_onboarding"] is False
    assert payload["completed"] is True
    assert payload["asr_api_base"] == "https://asr.example.test/v1"
    assert payload["asr_model"] == "whisper-1"
    assert payload["asr_key_present"] is True


def test_post_onboarding_complete_local_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = _paths(tmp_path)
    write_default_config(paths.config_path)
    source = tmp_path / "recordings"
    source.mkdir()
    monkeypatch.setattr(onboarding.asr_models, "local_path_for", lambda model_id: tmp_path / "installed")

    status, _headers, payload = handle_api_request(
        "POST",
        "/api/onboarding/complete",
        paths,
        body={
            "source_dir": str(source),
            "llm_api_base": "https://example.test/v1",
            "llm_model": "test-model",
            "llm_api_key": "sk-llm-test",
            "asr_mode": "local",
            "asr_model": "mlx-community/whisper-small-mlx-q4",
            "asr_model_source": "modelscope",
        },
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["asr_mode"] == "local"
    assert payload["current_backend"] == "mlx_whisper"
    assert payload["current_model"] == "mlx-community/whisper-small-mlx-q4"
    loaded = load_editable_config(config_path=paths.config_path)["config"]
    assert loaded["asr"]["backend"] == "mlx_whisper"
    assert loaded["asr"]["model"] == "mlx-community/whisper-small-mlx-q4"
    assert loaded["asr"]["model_source"] == "modelscope"
    assert "ASR_API_KEY" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_onboarding_html_exposes_four_steps_and_local_asr_controls():
    html = Path("src/live_clipper/web_static/index.html").read_text(encoding="utf-8")
    expected_steps = ["1 录播文件夹", "2 语音识别", "3 AI 服务", "4 完成"]

    assert [html.index(label) for label in expected_steps] == sorted(html.index(label) for label in expected_steps)
    for stable_id in [
        "onboardingAsrLocal",
        "onboardingAsrCloud",
        "onboardingAsrLocalPanel",
        "onboardingAsrCloudPanel",
        "onboardingAsrSource",
        "onboardingAsrModels",
        "onboardingAsrProgress",
        "onboardingAsrResult",
        "onboardingToStep4Btn",
        "onboardingBackTo3Btn",
        "onboardingEnterAppBtn",
    ]:
        assert f'id="{stable_id}"' in html
    assert 'name="onboardingAsrMode"' in html
    assert 'value="local"' in html
    assert 'value="cloud"' in html
    assert "本机识别（默认）" in html
    assert "云端识别（需要 API Key）" in html
    assert "onboarding.js?v=20260724-0.3.1" in html


def test_onboarding_js_reuses_model_job_apis_and_never_auto_downloads():
    script = Path("src/live_clipper/web_static/onboarding.js").read_text(encoding="utf-8")

    for endpoint in [
        '"/api/asr/models"',
        '"/api/asr/models/download"',
        '"/api/jobs/',
    ]:
        assert endpoint in script
    assert "asr_model_source: wizard.modelSource" in script
    assert "source: wizard.modelSource" in script
    assert "setInterval" not in script
    assert "partial_bytes" in script
    assert "bytes_total" in script
    assert "job_id" in script
    assert "state === \"installed\"" in script
    assert "job.status === \"failed\"" in script
    assert "job.status === \"interrupted\"" in script
    assert "anyModelDownloading" in script
    init_body = script.split("async function init()", 1)[1]
    assert 'startModelDownload(' not in init_body
    for forbidden in [
        "modelscope.cn",
        "huggingface.co",
        "hf-mirror",
        "sha256",
        "weights.npz",
    ]:
        assert forbidden not in script


def test_onboarding_js_invalidates_checks_and_uses_safe_text_nodes():
    script = Path("src/live_clipper/web_static/onboarding.js").read_text(encoding="utf-8")

    assert 'wizard.sourceOk = false' in script
    assert 'wizard.llmOk = false' in script
    assert 'addEventListener("input", invalidateSource)' in script
    for field_id in ["onboardingLlmBase", "onboardingLlmModel", "onboardingLlmKey"]:
        assert f'el("{field_id}").addEventListener("input", invalidateLlm)' in script
    assert "function appendSummaryRow" in script
    assert "textContent" in script
    summary = script.split("function renderSummary", 1)[1].split("function", 1)[0]
    assert "innerHTML" not in summary
    assert "showResult" in script


def test_onboarding_js_locks_active_download_and_handles_saved_service_failure():
    script = Path("src/live_clipper/web_static/onboarding.js").read_text(encoding="utf-8")

    assert "wizard.downloadActive" in script
    assert "onboardingAsrSource" in script
    assert ".disabled = wizard.downloadActive" in script
    assert "已有模型正在下载，请等待完成" in script
    assert "wizard.completed = true" in script
    assert "设置已保存，但自动化服务未启动，可进入主界面后手动启动" in script
    assert "onboardingEnterAppBtn" in script
    complete_body = script.split("async function complete()", 1)[1].split("async function init()", 1)[0]
    assert "if (wizard.completed) return" in complete_body
    assert re.search(r"serviceStart\.ok\s*!==\s*true", complete_body)


def test_onboarding_hidden_css_regression_guard_and_scoped_model_styles():
    styles = Path("src/live_clipper/web_static/styles.css").read_text(encoding="utf-8")

    assert ".onboarding-overlay[hidden]" in styles
    assert re.search(r"\.onboarding-overlay\[hidden\]\s*\{\s*display:\s*none;", styles)
    for class_name in [
        ".onboarding-asr-modes",
        ".onboarding-model-grid",
        ".onboarding-model-card",
        ".onboarding-progress",
    ]:
        assert class_name in styles
