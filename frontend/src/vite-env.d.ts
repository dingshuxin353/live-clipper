/// <reference types="vite/client" />

interface Window {
  liveClipperShell?: {
    selectFolder?(title: string): Promise<string | null>;
    readClipboardText?(): Promise<string>;
    writeClipboardText?(text: string): Promise<{ ok: true }>;
    openOutput?(outputId: string): Promise<{ ok: true }>;
    revealOutput?(outputId: string): Promise<{ ok: true }>;
    selectIssueSource?(issueId: string): Promise<{ selectionToken: string; expiresAt: string } | null>;
    selectRecoveryOutput?(issueId: string): Promise<{ selectionToken: string; expiresAt: string } | null>;
    showBackup?(migrationId: string): Promise<{ ok: true }>;
    quitApp?(): Promise<{ ok: true }>;
  };
}
