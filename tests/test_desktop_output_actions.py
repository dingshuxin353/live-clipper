from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preload_exposes_only_named_output_selection_and_clipboard_methods():
    preload = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")

    for method in [
        "openOutput",
        "revealOutput",
        "selectIssueSource",
        "selectRecoveryOutput",
        "writeClipboardText",
    ]:
        assert f"{method}:" in preload
    for forbidden in ["openPath:", "revealPath:", "selectFile:", "ipcRenderer:", "invoke:", "send:"]:
        assert forbidden not in preload


def test_main_process_authenticates_new_ipc_and_uses_backend_client_factories():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert "function assertTrustedRenderer(event)" in main
    assert "event.sender !== mainWindow.webContents" in main
    assert "isInternalAppUrl(event.senderFrame?.url, backendPort)" in main
    assert "createOutputActions({ client: backendClient, shell, runtime })" in main
    assert "createFileSelections({" in main
    for channel in [
        "lc:open-output",
        "lc:reveal-output",
        "lc:select-issue-source",
        "lc:select-recovery-output",
        "lc:write-clipboard-text",
    ]:
        assert f'ipcMain.handle("{channel}"' in main


def test_output_and_selection_operations_use_ids_and_private_bearer_routes():
    backend = (ROOT / "desktop" / "backend-client.js").read_text(encoding="utf-8")
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert "/api/desktop/outputs/${encodeURIComponent(outputId)}/path" in backend
    assert 'this.request("/api/desktop/file-selections"' in backend
    assert "body: { issue_id: issueId, kind, selected_path: selectedPath }" in backend
    assert "client.resolveOutputPath(id)" in runtime
    assert "client.registerFileSelection(id, kind, result.filePaths[0])" in runtime
    assert "selectionToken: grant.selection_token" in runtime
    assert "selectedPath" not in (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")


def test_desktop_file_actions_are_blocked_while_quitting_and_dialogs_are_narrow():
    runtime = (ROOT / "desktop" / "runtime-state.js").read_text(encoding="utf-8")

    assert 'if (!runtime.canStart()) throw new Error("应用正在退出' in runtime
    assert 'properties: ["openFile"]' in runtime
    assert 'properties: ["openDirectory", "createDirectory"]' in runtime
    assert '["m4v", "mkv", "mov", "mp4", "webm"]' in runtime
