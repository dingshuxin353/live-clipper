from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_first_run_uses_validated_startup_snapshot_before_showing_studio():
    backend = (ROOT / "desktop" / "backend-client.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert "requireOnboardingSnapshot" in backend
    assert 'snapshot.entry.mode === "workbench"' in backend
    assert "await this.getStudio()" in backend
    assert "const startup = await backendClient.waitUntilReady" in main
    assert 'showWindow("/studio")' in main
    startup_index = main.index("const startup = await backendClient.waitUntilReady")
    assert 'showWindow("/studio")' in main[startup_index:]


def test_desktop_first_run_folder_bridge_stays_narrow_and_runtime_guarded():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    preload = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert "createFolderSelection" in main
    assert 'ipcMain.handle("lc:select-folder"' in main
    assert "assertTrustedRenderer(event)" in main
    assert "folderSelection.select(title)" in main
    assert 'properties: ["openDirectory", "createDirectory"]' in runtime
    assert "if (!runtime.canStart())" in runtime
    assert 'selectFolder: (title) => ipcRenderer.invoke("lc:select-folder", title)' in preload
    for forbidden in ["readFile", "writeFile", "readdir", "exec:", "spawn:", "ipcRenderer:"]:
        assert forbidden not in preload


def test_desktop_first_run_keeps_single_instance_hide_activate_and_explicit_quit_contracts():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert "app.requestSingleInstanceLock()" in main
    assert 'app.on("second-instance"' in main
    assert 'mainWindow.on("close"' in main
    assert "event.preventDefault()" in main
    assert "mainWindow.hide()" in main
    assert 'app.on("activate"' in main
    assert 'app.on("before-quit"' in main
    assert "await shutdownBackend()" in main
