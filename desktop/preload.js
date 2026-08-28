// Minimal, deliberately tiny bridge. The page must never see Node APIs.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("liveClipperShell", {
  selectFolder: (title) => ipcRenderer.invoke("lc:select-folder", title),
  readClipboardText: () => ipcRenderer.invoke("lc:read-clipboard-text"),
  openOutput: (outputId) => ipcRenderer.invoke("lc:open-output", outputId),
  revealOutput: (outputId) => ipcRenderer.invoke("lc:reveal-output", outputId),
  selectIssueSource: (issueId) => ipcRenderer.invoke("lc:select-issue-source", issueId),
  selectRecoveryOutput: (issueId) => ipcRenderer.invoke("lc:select-recovery-output", issueId),
  writeClipboardText: (text) => ipcRenderer.invoke("lc:write-clipboard-text", text),
});
