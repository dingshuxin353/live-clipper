/// <reference types="vite/client" />

interface Window {
  liveClipperShell?: {
    selectFolder(title: string): Promise<string | null>;
  };
}
