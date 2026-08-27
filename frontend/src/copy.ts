export async function copyText(value: string): Promise<void> {
  if (window.liveClipperShell?.writeClipboardText) {
    await window.liveClipperShell.writeClipboardText(value);
    return;
  }
  if (!navigator.clipboard?.writeText) throw new Error("当前环境不支持复制，请手动选择文本");
  await navigator.clipboard.writeText(value);
}
