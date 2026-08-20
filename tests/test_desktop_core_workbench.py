from __future__ import annotations

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
