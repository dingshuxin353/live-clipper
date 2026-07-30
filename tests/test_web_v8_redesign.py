from __future__ import annotations

import re
from pathlib import Path

FRONTEND_SRC = Path("frontend/src")


def _source(*names: str) -> str:
    return "\n".join((FRONTEND_SRC / name).read_text(encoding="utf-8") for name in names)


def _styles() -> str:
    return (FRONTEND_SRC / "styles.css").read_text(encoding="utf-8")


def test_v8_console_contract_moved_to_react_dom_tests():
    app = _source("App.tsx")

    expected_tabs = ['["clips", "切片结果"]', '["automation", "自动化"]', '["confirmations", "确认"]', '["settings", "设置"]']
    assert [app.index(tab) for tab in expected_tabs] == sorted(app.index(tab) for tab in expected_tabs)
    for component in ["AppShell", "SideNav", "SideNavHeading", "SideNavItem", "SideNavSection"]:
        assert component in app
    for old_tab in [
        '["clips", "▶", "切片结果"]',
        '["automation", "◷", "自动化"]',
        '["confirmations", "✓", "确认"]',
        '["settings", "☰", "设置"]',
    ]:
        assert old_tab not in app
    for legacy_tab in ['"service"', '"runs"', '"logs"', '"config"']:
        assert f"data-tab={legacy_tab}" not in app
    for label in ["立即扫描录播", "运行日志", "AI 自动审阅", "没有待确认的操作"]:
        assert label in app
    for endpoint in ["/api/service/scan-now", "/api/review-automation/run-due", "/api/runs/", "/ai-review"]:
        assert endpoint in app


def test_v8_config_fields_remain_unique_and_complete():
    config = _source("config.ts")
    fields_block = config.split("export const CONFIG_FIELDS = [", 1)[1].split("] as const", 1)[0]
    fields = re.findall(r'"([a-z0-9_.]+)"', fields_block)

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
    for hidden_legacy_field in [
        "recording_source_default.input_dir",
        "recording_source_default.output_root",
        "paths.input_dir",
        "paths.output_root",
    ]:
        assert hidden_legacy_field not in fields


def test_v8_settings_keep_advanced_fields_collapsed():
    settings = _source("Settings.tsx")
    advanced = settings.split('data-config-layer="advanced"', 1)[0]
    quick_start = settings.split('data-config-layer="quick-start"', 1)[1].split(
        'data-config-layer="automation"', 1
    )[0]

    assert "<details" in advanced
    for advanced_label in ["tick 秒数", "Temperature", "Max tokens"]:
        assert advanced_label not in quick_start
    assert "当前识别模型（请在上方模型列表切换）" in settings


def test_v8_model_sources_states_order_and_safe_actions():
    settings = _source("Settings.tsx")
    models = Path("src/live_clipper/asr_models.py").read_text(encoding="utf-8")
    expected_ids = [
        "mlx-community/whisper-small-mlx-q4",
        "mlx-community/whisper-medium-mlx-q4",
        "mlx-community/whisper-large-v3-turbo",
    ]

    assert [models.index(model_id) for model_id in expected_ids] == sorted(
        models.index(model_id) for model_id in expected_ids
    )
    for label in [
        "ModelScope（中国大陆推荐）",
        "Hugging Face（国际官方）",
        "继续下载",
        "损坏需修复",
        "修复",
        "将使用：",
        "设为当前模型",
        "当前使用 · 尚未下载",
        "当前使用 · 模型损坏",
    ]:
        assert label in settings
    assert 'model.state === "installed" && !model.current' in settings
    assert "Promise.all([refreshModels()" in settings
    for forbidden in ["Qwen3", "mlx_audio", "ForcedAligner", "hf-mirror", "HF Mirror"]:
        assert forbidden not in settings


def test_v8_model_list_uses_astryx_rows_and_safe_responsive_layout():
    styles = _styles()
    settings = _source("Settings.tsx")
    list_rule = re.search(r"\.asr-model-list\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    side_rule = re.search(r"\.asr-model-side\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    actions_rule = re.search(r"\.asr-model-actions\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    mobile = styles.split("@media (max-width: 920px)", 1)[1]

    assert list_rule and "grid-column: 1 / -1" in list_rule.group(1)
    assert "width: 100%" in list_rule.group(1)
    assert "min-width: 0" in list_rule.group(1)
    assert 'from "@astryxdesign/core/List"' in settings
    assert "<List " in settings
    assert "<ListItem" in settings
    assert side_rule and "justify-content: flex-end" in side_rule.group(1)
    assert actions_rule and "flex-wrap: nowrap" in actions_rule.group(1)
    assert re.search(r"\.asr-model-side\s*\{[^}]*flex-wrap:\s*wrap", mobile, flags=re.DOTALL)


def test_v8_model_actions_use_astryx_buttons_with_only_layout_adapters():
    styles = _styles()
    settings = _source("Settings.tsx")
    action_rule = re.search(r"\.asr-model-action\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert action_rule
    for declaration in ["white-space: nowrap", "min-width: 72px", "min-height: 34px", "flex: 0 0 auto"]:
        assert declaration in action_rule.group(1)
    assert 'from "@astryxdesign/core/Button"' in settings
    assert ".asr-model-action:focus-visible" not in styles
    assert ".asr-model-action:disabled" not in styles
    for label in ["设为当前模型", "删除", "修复", "继续下载", "下载"]:
        assert label in settings


def test_v8_astryx_shell_and_misans_contract():
    styles = _styles()
    app = _source("App.tsx")
    mobile_styles = styles.split("@media (max-width: 920px)", 1)[1]
    root_match = re.search(r":root\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert "max-width: 100vw" in mobile_styles
    assert "min-width: max-content" not in mobile_styles
    assert "<AppShell" in app
    assert 'mobileNav={{ breakpoint: "sm" }}' in app
    assert 'mobileNav={{ breakpoint: "none"' not in app
    assert "hasToggle: false" not in app
    assert root_match
    assert (
        'font-family: "MiSans", "SF Pro Text", "PingFang SC", "Microsoft YaHei", '
        "system-ui, -apple-system, BlinkMacSystemFont, sans-serif;"
    ) in root_match.group(1)
    assert not re.search(r"button,\s*input,\s*select\s*\{", styles)


def test_v8_automation_service_grids_have_positive_spacing():
    styles = _styles()
    app = _source("App.tsx")
    summary_rule = re.search(r"\.service-summary-grid\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert 'id="serviceMetrics" className="metrics-grid"' in app
    assert 'id="serviceSummary" className="info-grid service-summary-grid"' in app
    assert summary_rule
    margin = re.search(r"margin-top:\s*([0-9.]+)px", summary_rule.group(1))
    assert margin
    assert float(margin.group(1)) > 0


def test_v8_automation_nested_grids_can_shrink_without_clipping():
    styles = _styles()
    mobile_styles = styles.split("@media (max-width: 920px)", 1)[1]
    responsive_grid_rule = re.search(
        r"\.metrics-grid,\s*\.info-grid,\s*\.scheduler-summary,\s*\.health-grid,\s*\.env-grid,[^}]*\{([^}]+)\}",
        mobile_styles,
        flags=re.DOTALL,
    )
    content_rule = re.search(r"\.content-card\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    grids_rule = re.search(
        r"\.info-grid,\s*\.metrics-grid,\s*\.scheduler-summary,\s*\.health-grid,\s*\.env-grid\s*\{([^}]+)\}",
        styles,
        flags=re.DOTALL,
    )
    direct_items_rule = re.search(
        r"\.info-grid\s*>\s*\*,\s*\.metrics-grid\s*>\s*\*,\s*\.scheduler-summary\s*>\s*\*,\s*\.health-grid\s*>\s*\*,\s*\.env-grid\s*>\s*\*\s*\{([^}]+)\}",
        styles,
        flags=re.DOTALL,
    )
    row_rule = re.search(r"\.metric,\s*\.info-row\s*\{([^}]+)\}", styles, flags=re.DOTALL)
    technical_row_rule = re.search(
        r"\.info-row\s*>\s*\.technical-value\s*\{([^}]+)\}", styles, flags=re.DOTALL
    )
    technical_rule = re.search(r"\.technical-value\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    for rule in [content_rule, grids_rule, direct_items_rule, row_rule]:
        assert rule
        assert "min-width: 0" in rule.group(1)
        assert "max-width: 100%" in rule.group(1)
    assert technical_row_rule
    assert "min-width: 0" in technical_row_rule.group(1)
    assert responsive_grid_rule
    assert "grid-template-columns: minmax(0, 1fr)" in responsive_grid_rule.group(1)
    assert not re.search(r"grid-template-columns:\s*1fr", responsive_grid_rule.group(1))
    assert technical_rule
    assert "overflow: hidden" in technical_rule.group(1)
    assert "text-overflow: ellipsis" in technical_rule.group(1)
    assert "white-space: nowrap" in technical_rule.group(1)
    assert "word-break: break-all" not in technical_rule.group(1)
    assert not re.search(r"overflow-x:\s*(hidden|clip)", styles)


def test_v8_settings_sections_span_the_outer_grid():
    styles = _styles()
    settings = _source("Settings.tsx")
    content_rule = re.search(
        r"\.settings-section\s*>\s*\.settings-field-grid\s*\{([^}]+)\}",
        styles,
        flags=re.DOTALL,
    )
    description_rule = re.search(r"\.field-note\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert '<FormLayout className="settings-field-grid">' in settings
    assert 'description && <p className="muted field-note">' in settings
    assert content_rule
    assert "grid-column: 1 / -1" in content_rule.group(1)
    assert "min-width: 0" in content_rule.group(1)
    assert description_rule
    assert "grid-column: 1 / -1" in description_rule.group(1)
    assert "margin: 0 0 4px" in description_rule.group(1)


def test_v8_status_text_uses_only_stone_semantic_colors():
    sources = _source("App.tsx", "Onboarding.tsx", "Settings.tsx")
    presentation = _source("ui/presentation.ts")

    assert "@astryxdesign/core/Banner" not in sources
    assert "<Banner" not in sources
    assert 'from "@astryxdesign/core/theme"' in presentation
    assert "colorVars" in presentation
    for token in [
        "--color-text-secondary",
        "--color-text-green",
        "--color-text-yellow",
        "--color-text-red",
    ]:
        assert token in presentation
    assert not re.search(
        r"(background|border|radius|shadow|padding|width|display)\s*:",
        presentation,
    )


def test_v8_busy_and_toast_accessibility_use_supported_astryx_contracts():
    app = _source("App.tsx")
    onboarding = _source("Onboarding.tsx")
    settings = _source("Settings.tsx")

    assert app.count("@astryx.") == 6
    assert '"@astryx.toast.dismiss": "关闭通知"' in app
    assert 'from "@astryxdesign/core/Spinner"' in app
    assert 'from "@astryxdesign/core/VisuallyHidden"' in app
    assert "AI 审阅正在进行" in app
    assert "isLoading=" not in app + onboarding
    assert "isActionLoading=" not in settings
    assert "aria-busy=" not in app + onboarding + settings
    assert app.count("data-busy=") == 1
    assert onboarding.count("data-busy=") == 4
    assert app.count("<VisuallyHidden") == 1
    assert onboarding.count("<VisuallyHidden") == 3
    assert "setDeleteModel(null);" in settings
    assert 'void act(model, "delete")' in settings
