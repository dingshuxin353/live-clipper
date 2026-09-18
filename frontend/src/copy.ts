export async function copyText(value: string): Promise<void> {
  if (window.liveClipperShell?.writeClipboardText) {
    await window.liveClipperShell.writeClipboardText(value);
    return;
  }
  if (!navigator.clipboard?.writeText) throw new Error("无法自动复制，请手动选择文本复制。");
  await navigator.clipboard.writeText(value);
}
