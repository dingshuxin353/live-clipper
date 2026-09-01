from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_migration_uses_bearer_grant_and_main_owned_app_home() -> None:
    backend = (ROOT / "desktop" / "backend-client.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert "/backup-grant" in backend
    assert "/backup-action" not in backend
    assert "getMigrationBackupGrant" in backend
    assert "LIVE_CLIPPER_HOME" in main
    assert "appHome" in main
    assert 'app.setPath("userData", electronHome)' in main
    assert main.index('app.setPath("userData", electronHome)') < main.index("app.requestSingleInstanceLock()")
    assert "electronRuntimeHome" in runtime
    assert "grant_version" in runtime
    assert "migration_backup_reveal" in runtime
    assert "realpathSync" in runtime
    assert "lstatSync" in runtime
    assert "shell.showItemInFolder" in runtime


def test_desktop_migration_restricts_project_badge_tray_and_updates_until_acknowledged() -> None:
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert "migration_completed_unacknowledged" in runtime
    assert "canUseProjectFeatures" in runtime
    assert "runtime.isRestricted()" in main
    assert "runtime.canUseProjectFeatures()" in main
    assert "isAllowed: () => runtime.canUseProjectFeatures()" in main
    assert "refreshDesktopCapabilities" in main


def test_desktop_migration_bridge_remains_id_only_and_renderer_never_receives_path() -> None:
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    preload = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
    types = (ROOT / "frontend" / "src" / "vite-env.d.ts").read_text(encoding="utf-8")

    expected = 'showBackup: (migrationId) => ipcRenderer.invoke("lc:show-migration-backup", migrationId)'
    assert expected in preload
    assert "migrationActions.showBackup(migrationId)" in main
    assert "showBackup?(migrationId: string): Promise<{ ok: true }>" in types
    for forbidden in ("backupPath", "backup_path", "showMigrationBackup"):
        assert forbidden not in preload
