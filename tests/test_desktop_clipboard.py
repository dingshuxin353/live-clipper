from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_exposes_only_a_narrow_clipboard_read_bridge():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    preload = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")

    assert 'ipcMain.handle("lc:read-clipboard-text", () => clipboard.readText())' in main
    assert 'readClipboardText: () => ipcRenderer.invoke("lc:read-clipboard-text")' in preload
    assert "nodeIntegration: false" in main
    assert "contextIsolation: true" in main
    assert "send:" not in preload
    assert "invoke:" not in preload


def test_desktop_application_menu_restores_standard_paste_actions():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")

    assert '{ role: "paste", label: "粘贴" }' in main
    assert '{ role: "pasteAndMatchStyle", label: "粘贴并匹配样式" }' in main
    assert "Menu.setApplicationMenu(Menu.buildFromTemplate(template))" in main
