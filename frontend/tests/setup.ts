import "@testing-library/dom";
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  delete window.liveClipperShell;
  document.body.className = "";
});
