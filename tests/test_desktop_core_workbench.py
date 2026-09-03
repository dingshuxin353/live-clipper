from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_uses_testable_runtime_and_authenticated_backend_client():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    builder = (ROOT / "desktop" / "electron-builder.yml").read_text(encoding="utf-8")

    assert 'require("./backend-client")' in main
    assert 'require("./runtime-state")' in main
    assert "httpOnly: true" in main
    assert "backend-client.js" in builder
    assert "runtime-state.js" in builder


def test_desktop_starts_on_studio_and_keeps_deep_routes_inside_the_app():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert 'showWindow("/studio")' in main
    assert 'showWindow("/projects")' in main
    assert "setWindowOpenHandler" in main
    assert "isInternalAppUrl" in main


def test_tray_has_only_project_scoped_navigation_and_update_actions():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    for label in ["打开工作室", "查看项目", "检查更新", "退出 Venus"]:
        assert f'label: "{label}"' in main
    assert "立即扫描录播" not in main
    assert "/api/service/scan-now" not in main


def test_shutdown_and_activate_share_runtime_guards_and_badge_refresh():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert "runtime.beginQuit()" in main
    assert 'if (!runtime.isQuitting())' in main
    assert "if (!quitting)" not in main
    assert "badgePoller?.stop()" in main
    assert "badgePoller?.activate()" in main
    assert 'proc.kill("SIGTERM")' in main
    assert 'proc.kill("SIGKILL")' in main


def test_dock_badge_uses_only_unseen_results_and_keeps_failed_refreshes_stable():
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert "studio.unseen_result_count" in runtime
    assert "pending_review_count" not in runtime
    assert "dock.setBadge(formatBadgeCount(count))" in runtime
    assert "catch (_error)" in runtime


def test_electron_titlebar_keeps_traffic_lights_clear_of_workbench_navigation():
    styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

    shell_navigation = re.search(
        r"body\.in-app-shell \.top-navigation\s*\{(?P<rule>[^}]*)\}",
        styles,
    )
    assert shell_navigation is not None

    safe_inset = re.search(
        r"padding-(?:left|inline-start):\s*(?P<pixels>\d+)px",
        shell_navigation.group("rule"),
    )
    assert safe_inset is not None
    assert int(safe_inset.group("pixels")) >= 80

    interactive_navigation = re.search(
        r"body\.in-app-shell \.top-navigation a\s*,\s*"
        r"body\.in-app-shell \.top-navigation button\s*\{(?P<rule>[^}]*)\}",
        styles,
    )
    assert interactive_navigation is not None
    assert "-webkit-app-region: no-drag" in interactive_navigation.group("rule")
    assert ".astryx-app-shell-sidenav .venus-side-nav" not in styles


def test_forms_share_astryx_controls_path_fields_and_container_layout():
    frontend = ROOT / "frontend" / "src"
    shared = (frontend / "workbench-shared.tsx").read_text(encoding="utf-8")
    dialogs = (frontend / "ProjectDialogs.tsx").read_text(encoding="utf-8")
    onboarding = (frontend / "Onboarding.tsx").read_text(encoding="utf-8")
    migration = (frontend / "features" / "migration" / "MigrationFlow.tsx").read_text(encoding="utf-8")
    results = (frontend / "RunResultPage.tsx").read_text(encoding="utf-8")
    settings = (frontend / "Settings.tsx").read_text(encoding="utf-8")
    styles = (frontend / "styles.css").read_text(encoding="utf-8")

    assert "export function PathField" in shared
    for source in [dialogs, onboarding, migration]:
        assert "PathField" in source
    for source in [dialogs, onboarding, migration, results]:
        assert "@astryxdesign/core/Field" in source or "@astryxdesign/core/TextInput" in source
    combined = "\n".join([dialogs, onboarding, migration, styles])
    for obsolete in ["input-action", "toggle-field", "onboarding-folder", "migration-field", "migration-schedule"]:
        assert f'className="{obsolete}"' not in combined
        assert re.search(rf"(?<![-\w])\.{re.escape(obsolete)}(?![-\w])", styles) is None
    assert "container-type: inline-size" in styles
    assert "@container" in styles
    assert "form-control form-secret-input" in onboarding
    assert "form-control form-secret-input" in results
    assert "form-control form-secret-input" in settings
    assert "const [apiKey, setApiKey]" not in results
