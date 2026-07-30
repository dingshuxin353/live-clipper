from __future__ import annotations

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


def _react() -> str:
    return Path("frontend/src/Onboarding.tsx").read_text(encoding="utf-8")


def test_get_onboarding_needs_true_when_unconfigured(tmp_path):
    status, _headers, payload = handle_api_request("GET", "/api/onboarding", _paths(tmp_path))
    assert status == 200
    assert payload["needs_onboarding"] is True
    assert payload["completed"] is False
    assert payload["skipped"] is False
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


def test_post_onboarding_skip_round_trip(tmp_path):
    paths = _paths(tmp_path)
    status, _headers, payload = handle_api_request("POST", "/api/onboarding/skip", paths, body={})
    assert status == 200
    assert payload == {"ok": True, "skipped": True, "completed": False}
    status, _headers, payload = handle_api_request("GET", "/api/onboarding", paths)
    assert status == 200
    assert payload["needs_onboarding"] is False
    assert payload["completed"] is False
    assert payload["skipped"] is True


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
    assert payload["skipped"] is False
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


def test_onboarding_react_exposes_four_steps_controls_and_shared_skip_dialog():
    source = _react()
    expected_steps = ["1 录播文件夹", "2 语音识别", "3 AI 服务", "4 完成"]
    assert [source.index(label) for label in expected_steps] == sorted(source.index(label) for label in expected_steps)
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
        "onboardingBrowseBtn",
        "onboardingSkipDialog",
        "onboardingSkipContinueBtn",
        "onboardingSkipConfirmBtn",
    ]:
        assert stable_id in source
    for consequence in [
        "未配置录像目录时不会自动发现新录像",
        "未配置语音识别时不能完成转写",
        "未配置 AI 服务时不能自动选片",
        "已经启动的模型下载不会因离开引导而取消",
    ]:
        assert consequence in source


def test_onboarding_react_preserves_validation_bridge_and_model_job_contracts():
    source = _react()
    for endpoint in [
        "/api/onboarding/test-source",
        "/api/onboarding/test-llm",
        "/api/onboarding/complete",
        "/api/onboarding/skip",
        "/api/asr/models",
        "/api/asr/models/download",
        "/api/jobs/",
    ]:
        assert endpoint in source
    assert 'selectFolder("选择录播文件夹")' in source
    assert "if (!selectedPath) return" in source
    assert "source: modelSource" in source
    assert 'job.status === "failed"' in source
    assert 'job.status === "interrupted"' in source
    assert "模型仍在下载，安装完成后才能保存设置" in source
    assert "下载未完成，不能保存本机识别设置" in source
    assert "设置已保存，但自动化服务未启动，可进入主界面后手动启动" in source
    for forbidden in ["modelscope.cn", "huggingface.co", "hf-mirror", "weights.npz"]:
        assert forbidden not in source


def test_onboarding_react_never_renders_or_logs_secret_values():
    source = _react()
    assert "console." not in source
    assert "data-api-key" not in source
    assert "已填写（只保存在本机 .env）" in source
    assert "llmKey ? " in source
    assert "asrKey ? " in source


def test_onboarding_styles_remain_scoped_and_keep_hidden_guard():
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")
    assert ".onboarding-overlay[hidden]" in styles
    for class_name in [
        ".onboarding-asr-modes",
        ".onboarding-model-grid",
        ".onboarding-model-card",
        ".onboarding-progress",
    ]:
        assert class_name in styles
    source = _react()
    assert 'from "@astryxdesign/core/Selector"' in source
    assert 'className="onboarding-source-selector"' in source
    assert "#onboardingAsrSource" not in styles
