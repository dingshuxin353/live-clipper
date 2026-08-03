import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { expandColorScale } from "@astryxdesign/core/theme";

const requiredColorTokens = [
  "--color-accent",
  "--color-accent-muted",
  "--color-on-accent",
  "--color-text-accent",
  "--color-icon-accent",
];

const colors = expandColorScale({
  accent: "#4A3A72",
  neutralStyle: "neutral",
  contrast: "standard",
});

for (const token of requiredColorTokens) {
  if (!colors[token]) {
    throw new Error(`Astryx Stone color scale did not provide ${token}`);
  }
}

const expectedColors = {
  "--color-accent": "light-dark(#67519F, #D3BBFF)",
  "--color-accent-muted": "light-dark(color-mix(in srgb, var(--color-accent) 20%, transparent), color-mix(in srgb, var(--color-accent) 25%, transparent))",
  "--color-on-accent": "light-dark(#FFFFFF, #33236B)",
  "--color-text-accent": "var(--color-accent)",
  "--color-icon-accent": "var(--color-accent)",
};

for (const token of requiredColorTokens) {
  if (colors[token] !== expectedColors[token]) {
    throw new Error(`Unexpected Astryx Stone value for ${token}: ${colors[token]}`);
  }
}

const fontBody = '"MiSans", "SF Pro Text", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
const css = [
  "@layer theme {",
  '  [data-astryx-theme="stone"] {',
  ...requiredColorTokens.map((token) => `    ${token}: ${colors[token]};`),
  `    --font-family-body: ${fontBody};`,
  `    --font-family-heading: ${fontBody};`,
  '    --font-family-code: "SF Mono", Monaco, Consolas, monospace;',
  "  }",
  "}",
  "",
].join("\n");

const outputPath = fileURLToPath(new URL("../src/theme/venus-stone-overrides.css", import.meta.url));
await mkdir(fileURLToPath(new URL("../src/theme/", import.meta.url)), { recursive: true });
await writeFile(outputPath, css, "utf8");
