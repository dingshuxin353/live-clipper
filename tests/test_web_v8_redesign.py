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

    assert len(fields) == 50
    assert len(fields) == len(set(fields))
    for field in [
        "recording_source_default.source_dir",
        "service.auto_render_after_selection",
        "scheduler.tick_seconds",
        "review_automation_model.temperature",
        "web.host",
    ]:
        assert field in fields


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
    for label in ["继续下载", "损坏需修复", "修复", "将使用：", "last_error", "partial_bytes"]:
        assert label in app
    assert "设为当前模型" not in app


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
