from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    assert payload["entry"]["mode"] == "onboarding"
    assert payload["entry"]["onboarding"] == "new"
    assert payload["session"] is None
    assert set(payload) >= {"environment", "resources", "model_catalog", "provider_presets", "suggestions"}


@pytest.mark.parametrize("route", ["test-source", "test-llm", "complete", "skip"])
def test_deprecated_onboarding_routes_are_tombstoned_without_side_effects(tmp_path, route, monkeypatch):
    paths = _paths(tmp_path)
    config = paths.config_path
    env = config.parent / ".env"
    config.write_bytes(b"config-before\n")
    env.write_bytes(b"KEEP=before\n")
    before = sorted((item.relative_to(tmp_path), item.stat().st_size, item.stat().st_mtime_ns) for item in tmp_path.rglob("*") if item.is_file())
    monkeypatch.setattr("live_clipper.onboarding_resources.requests.post", lambda *args, **kwargs: pytest.fail("deprecated route must not call network"))
    status, _headers, payload = handle_api_request(
        "POST", f"/api/onboarding/{route}", paths, body={"source_dir": str(tmp_path / "missing"), "api_key": "sentinel"}
    )
    assert status == 410
    assert payload["error_code"] == "onboarding_contract_replaced"
    assert payload["ok"] is False
    after = sorted((item.relative_to(tmp_path), item.stat().st_size, item.stat().st_mtime_ns) for item in tmp_path.rglob("*") if item.is_file())
    assert after == before
    assert not (paths.service_dir / "venus.sqlite3").exists()


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
    assert 'llmKeyRef = useRef("")' in source
    assert 'asrKeyRef = useRef("")' in source
    assert "llmKeyRef.current" in source
    assert "asrKeyRef.current" in source
    assert 'from "@astryxdesign/core/Field"' in source
    secret_inputs = re.findall(r"<input\b[\s\S]*?/>", source)
    assert len(secret_inputs) == 2
    for input_id, input_source in zip(
        ["onboardingAsrKey", "onboardingLlmKey"], secret_inputs, strict=True
    ):
        assert f'id="{input_id}"' in input_source
        assert 'type="password"' in input_source
        assert 'className="onboarding-secret-input"' in input_source
        assert " value=" not in input_source
        assert "defaultValue=" not in input_source
        assert "data-" not in input_source
        assert "name=" not in input_source
        assert f'inputID="{input_id}"' in source


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


def test_onboarding_browse_button_has_positive_spacing():
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")
    source = _react()
    browse_rule = re.search(r"\.onboarding-browse\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert 'className="onboarding-browse"' in source
    assert browse_rule
    assert not re.search(r"margin(?:-top)?:\s*-", browse_rule.group(1))
    margin = re.search(r"margin:\s*([0-9.]+)px\s+0\s+([0-9.]+)px", browse_rule.group(1))
    assert margin
    assert float(margin.group(1)) > 0
    assert float(margin.group(2)) > 0


def test_onboarding_source_error_is_unique_and_not_field_status_message():
    onboarding = _react()

    assert 'id="onboardingSourceError"' in onboarding
    assert 'aria-errormessage={sourceError ? "onboardingSourceError" : undefined}' in onboarding
    assert "aria-invalid={Boolean(sourceError)}" in onboarding
    assert 'status={sourceError ? { type: "error" } : undefined}' in onboarding
    assert "message: sourceResult.message" not in onboarding


def test_onboarding_busy_buttons_use_chinese_live_status_without_loading_props():
    source = _react()

    assert 'from "@astryxdesign/core/Spinner"' in source
    assert 'from "@astryxdesign/core/VisuallyHidden"' in source
    assert "isLoading=" not in source
    assert "aria-busy=" not in source
    assert source.count("data-busy=") == 4
    assert source.count("<VisuallyHidden") == 3
    for message in ["正在检查录播文件夹", "正在测试 AI 服务连接", "正在保存设置"]:
        assert message in source
