// Minimal, deliberately tiny bridge. The page must never see Node APIs.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("liveClipperShell", {
  selectFolder: (title) => ipcRenderer.invoke("lc:select-folder", title),
  readClipboardText: () => ipcRenderer.invoke("lc:read-clipboard-text"),
});
