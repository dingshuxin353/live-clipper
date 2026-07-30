import type { StyleXStyles } from "@stylexjs/stylex";
import { colorVars } from "@astryxdesign/core/theme";

export type SemanticTone = "info" | "success" | "warning" | "error";

export const semanticToneStyles = {
  info: { color: colorVars["--color-text-secondary"] },
  success: { color: colorVars["--color-text-green"] },
  warning: { color: colorVars["--color-text-yellow"] },
  error: { color: colorVars["--color-text-red"] },
} as unknown as Record<SemanticTone, StyleXStyles>;

export function formatLocalTime(value: unknown): string {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const pick = (type: Intl.DateTimeFormatPartTypes) => (
    parts.find((part) => part.type === type)?.value ?? ""
  );
  return `${pick("year")}-${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}`;
}
