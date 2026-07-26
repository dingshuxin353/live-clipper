from __future__ import annotations

import re
from pathlib import Path


def _html() -> str:
    return Path("src/live_clipper/web_static/index.html").read_text(encoding="utf-8")


def _app() -> str:
    return Path("src/live_clipper/web_static/app.js").read_text(encoding="utf-8")


def test_v8_console_uses_user_task_navigation():
    html = _html()

    assert "直播切片 · 本地控制台" in html
    assert 'data-tab="clips"' in html
    assert 'data-tab="automation"' in html
    assert 'data-tab="confirmations"' in html
    assert 'data-tab="settings"' in html
    assert 'data-tab="service"' not in html
    assert 'data-tab="runs"' not in html
    assert 'data-tab="logs"' not in html
    assert 'data-tab="config"' not in html


def test_v8_console_exposes_core_actions_without_prototype_runtime():
    html = _html()
    app = _app()

    for label in ["切片结果", "自动化", "确认", "设置", "立即扫描录播", "运行日志", "AI 自动审阅", "没有待确认的操作"]:
        assert label in html
    for forbidden in ["sc-if", "sc-for", "DCLogic"]:
        assert forbidden not in html
        assert forbidden not in app
    assert "/api/service/scan-now" in app
    assert "/api/review-automation/run-due" in app
    assert "/api/runs/" in app
    assert "/ai-review" in app
    assert "reviewAutomationActionStatus" in html
    assert "review_automation_disabled" in app
    assert "自动 AI 审阅还没有启用" in app


def test_v8_config_fields_remain_unique_and_complete():
    html = _html()
    fields = re.findall(r'data-config-field="([^"]+)"', html)

    assert len(fields) == 47
    assert len(fields) == len(set(fields))
    for field in [
        "recording_source_default.source_dir",
        "paths.workspace_root",
        "service.auto_render_after_selection",
        "scheduler.tick_seconds",
        "review_automation_model.temperature",
        "web.host",
    ]:
        assert field in fields
    model_field = re.findall(r'<input[^>]+data-config-field="asr\.model"[^>]*>', html)
    assert len(model_field) == 1
    assert "readonly" in model_field[0]
    assert "当前识别模型（请在上方模型列表切换）" in html
    for hidden_legacy_field in [
        "recording_source_default.input_dir",
        "recording_source_default.output_root",
        "paths.input_dir",
        "paths.output_root",
    ]:
        assert hidden_legacy_field not in fields
    assert "任务工作区位置" in html
    assert "应用内部状态目录" in html


def test_v8_settings_keep_advanced_fields_collapsed():
    html = _html()

    advanced_match = re.search(r'<details[^>]+data-config-layer="advanced"[^>]*>', html)
    assert advanced_match
    assert " open" not in advanced_match.group(0)
    quick_start = html.split('data-config-layer="quick-start"', 1)[1].split('data-config-layer="automation"', 1)[0]
    for advanced_label in ["tick 秒数", "Temperature", "Max tokens"]:
        assert advanced_label not in quick_start


def test_v8_model_download_sources_and_states():
    html = _html()
    app = _app()

    source_block = html.split('data-config-field="asr.model_source"', 1)[1].split("</select>", 1)[0]
    expected = [
        'value="modelscope">ModelScope（中国大陆推荐）',
        'value="huggingface">Hugging Face（国际官方）',
    ]
    assert all(label in source_block for label in expected)
    assert [source_block.index(label) for label in expected] == sorted(source_block.index(label) for label in expected)
    assert "hf-mirror" not in source_block
    assert "HF Mirror" not in source_block
    assert 'model_source: "modelscope"' in app
    for label in [
        "继续下载",
        "损坏需修复",
        "修复",
        "将使用：",
        "last_error",
        "partial_bytes",
        "设为当前模型",
        "当前使用 · 尚未下载",
        "当前使用 · 模型损坏",
    ]:
        assert label in app
    assert "model.recommended" not in app
    assert " · 推荐" not in app


def test_v8_model_matrix_order_tiers_and_safe_current_actions():
    models = Path("src/live_clipper/asr_models.py").read_text(encoding="utf-8")
    app = _app()
    expected_ids = [
        "mlx-community/whisper-small-mlx-q4",
        "mlx-community/whisper-medium-mlx-q4",
        "mlx-community/whisper-large-v3-turbo",
    ]

    assert [models.index(model_id) for model_id in expected_ids] == sorted(
        models.index(model_id) for model_id in expected_ids
    )
    for tier_label in ['"tier_label": "轻量"', '"tier_label": "平衡"', '"tier_label": "高精度"']:
        assert tier_label in models
    assert "model.tier_label" in app
    assert "if (!model.current)" in app
    assert 'data-action="select"' in app
    assert 'data-action="delete"' in app
    assert "currentBadge" in app
    for forbidden in ["Qwen3", "mlx_audio", "ForcedAligner"]:
        assert forbidden not in models
        assert forbidden not in app


def test_v8_select_waits_for_server_and_refreshes_models_and_config():
    app = _app()
    selection = app.split("async function selectAsrModel", 1)[1].split(
        "async function deleteAsrModel",
        1,
    )[0]

    assert 'api("/api/asr/models/select"' in selection
    assert 'button.disabled = true' in selection
    assert "finally" in selection
    assert "Promise.all([refreshAsrModels(), loadConfig(true)])" in selection
    assert ".current =" not in selection


def test_v8_mobile_nav_is_contained_inside_viewport():
    styles = Path("src/live_clipper/web_static/styles.css").read_text(encoding="utf-8")
    mobile_styles = styles.split("@media (max-width: 920px)", 1)[1]

    assert ".app-shell" in mobile_styles
    assert "max-width: 100vw" in mobile_styles
    assert ".nav-list" in mobile_styles
    assert "overflow-x: auto" in mobile_styles
    assert ".nav-item" in mobile_styles
    assert "min-width: max-content" not in mobile_styles


def test_v8_uses_misans_default_font_and_inherited_form_controls():
    styles = Path("src/live_clipper/web_static/styles.css").read_text(encoding="utf-8")
    root_match = re.search(r":root\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    form_match = re.search(r"button,\s*input,\s*select\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert root_match
    assert (
        'font-family: "MiSans", "SF Pro Text", "PingFang SC", "Microsoft YaHei", '
        "system-ui, -apple-system, BlinkMacSystemFont, sans-serif;"
    ) in root_match.group(1)
    assert form_match
    assert "font: inherit;" in form_match.group(1)
