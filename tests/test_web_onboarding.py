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


def test_onboarding_react_exposes_five_steps_and_real_k_endpoints():
    source = _react()
    expected_steps = ['label: "开始"', 'label: "语音识别"', 'label: "AI 服务"', 'label: "第一个项目"', 'label: "完成"']
    assert [source.index(label) for label in expected_steps] == sorted(source.index(label) for label in expected_steps)
    api = Path("frontend/src/project-api.ts").read_text(encoding="utf-8")
    for endpoint in [
        "/api/onboarding/start",
        "/api/onboarding/session",
        "/api/onboarding/pause",
        "/api/onboarding/resume",
        "/api/onboarding/environment-check",
        "/api/onboarding/resources/asr/local",
        "/api/onboarding/resources/asr/cloud",
        "/api/onboarding/resources/ai",
        "/api/onboarding/project/validate",
        "/api/onboarding/finish",
        "/api/onboarding/service/retry",
        "/api/asr/models/download",
        "/api/jobs/",
    ]:
        assert endpoint in api


def test_startup_gate_pause_completion_and_trial_are_explicit():
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    onboarding = _react()
    studio = Path("frontend/src/StudioProjects.tsx").read_text(encoding="utf-8")
    for token in ["migration_required", "diagnostic_required", "正在准备 Venus", "暂时无法确认数据状态"]:
        assert token in app
    assert "MigrationFlow" in app
    assert "Venus 没有初始化或修改这些数据。请等待迁移工具准备完成后再继续。" not in app
    for token in ["首次设置尚未完成", "继续首次设置"]:
        assert token in studio
    for token in ["项目已保存，本机服务尚未启动", "重新启动服务", "选择一条录像试运行", '"selected"', "selected_relative_paths"]:
        assert token in onboarding or token in Path("frontend/src/project-api.ts").read_text(encoding="utf-8")


def test_migration_react_uses_real_o_contract_and_keeps_state_private():
    migration = Path("frontend/src/features/migration/MigrationFlow.tsx").read_text(encoding="utf-8")
    api = Path("frontend/src/project-api.ts").read_text(encoding="utf-8")
    shell = Path("frontend/src/vite-env.d.ts").read_text(encoding="utf-8")

    for endpoint in [
        "/api/migration",
        "/api/migration/inspect",
        "/api/migration/validate",
        "/api/migration/execute",
        "/api/migration/retry",
        "/api/migration/acknowledge",
    ]:
        assert endpoint in api
    for token in [
        'role="dialog"',
        'aria-modal="true"',
        'aria-live="polite"',
        'event.key === "Escape"',
        'event.key !== "Tab"',
        'document.addEventListener("visibilitychange"',
        "document.hidden ? 4000 : 1000",
        'status={fields.project_name ? { type: "error", message: fields.project_name } : undefined}',
    ]:
        assert token in migration
    for forbidden in ["localStorage", "sessionStorage", "indexedDB", "console."]:
        assert forbidden not in migration
    assert "showBackup(id)" in migration
    assert "showBackup?(migrationId: string)" in shell
    assert "quitApp?()" in shell


def test_secret_inputs_are_uncontrolled_and_never_logged_or_persisted():
    source = _react()
    assert "console." not in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "data-api-key" not in source
    assert 'asrKeyRef = useRef("")' in source
    assert 'aiKeyRef = useRef("")' in source
    assert "asrKeyRef.current" in source
    assert "aiKeyRef.current" in source
    secret_inputs = re.findall(r'<input ref=\{keyInput\} className="form-control form-secret-input".*?/>', source)
    assert len(secret_inputs) == 2
    for input_source in secret_inputs:
        assert 'type="password"' in input_source
        assert " value=" not in input_source
        assert "defaultValue=" not in input_source
        assert "name=" not in input_source


def test_accessibility_and_live_state_contracts_remain_strong():
    source = _react()
    for token in [
        'role="dialog"',
        'aria-modal="true"',
        'role="progressbar"',
        "aria-valuenow={progress}",
        'aria-errormessage={error ? "onboarding-action-error" : undefined}',
        'role="alert"',
        'event.key === "Escape"',
        'event.key !== "Tab"',
        'document.addEventListener("visibilitychange"',
    ]:
        assert token in source
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "@media (max-width: 980px)" in styles


def test_old_onboarding_product_contract_is_absent_from_frontend():
    files = [
        Path("frontend/src/Onboarding.tsx"),
        Path("frontend/src/App.tsx"),
        Path("frontend/src/styles.css"),
        Path("frontend/tests/Onboarding.test.tsx"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for forbidden in [
        "/api/onboarding/test-source",
        "/api/onboarding/test-llm",
        "/api/onboarding/complete",
        "/api/onboarding/skip",
        "onboardingSkipDialog",
        "showEnter",
        "V9a",
        "1 录播文件夹",
        "Small 默认",
    ]:
        assert forbidden not in combined
