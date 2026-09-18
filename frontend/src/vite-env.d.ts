/// <reference types="vite/client" />

interface Window {
  liveClipperShell?: {
    getApplicationInfo?(): Promise<{ version: string; app_home: string; platform: string; arch: string }>;
    openDataDirectory?(id: 'app' | 'work'): Promise<{ ok: true }>;
    checkForUpdates?(): Promise<{ ok: boolean; code?: string; message?: string }>;
    selectFolder?(title: string): Promise<string | null>;
    readClipboardText?(): Promise<string>;
    writeClipboardText?(text: string): Promise<{ ok: true }>;
    openOutput?(outputId: string): Promise<{ ok: true }>;
    revealOutput?(outputId: string): Promise<{ ok: true }>;
    selectIssueSource?(issueId: string): Promise<{ selectionToken: string; expiresAt: string } | null>;
    selectRecoveryOutput?(issueId: string): Promise<{ selectionToken: string; expiresAt: string } | null>;
    showBackup?(migrationId: string): Promise<{ ok: boolean; message?: string; code?: string }>;
    quitApp?(): Promise<{ ok: true }>;
  };
}
