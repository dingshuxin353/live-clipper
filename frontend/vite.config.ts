import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/static/react/",
  plugins: [react()],
  build: {
    outDir: "../src/live_clipper/web_static/react",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./tests/setup.ts",
    clearMocks: true,
    globals: true,
  },
});
