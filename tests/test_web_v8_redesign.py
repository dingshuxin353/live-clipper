from __future__ import annotations

import re
from pathlib import Path

FRONTEND_SRC = Path("frontend/src")


def _source(*names: str) -> str:
    return "\n".join((FRONTEND_SRC / name).read_text(encoding="utf-8") for name in names)


def _styles() -> str:
    return (FRONTEND_SRC / "styles.css").read_text(encoding="utf-8")


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
