import "@testing-library/dom";
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
}

if (!HTMLDialogElement.prototype.close) {
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
}

if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => true,
  });
}

window.scrollTo = () => undefined;
HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
  arc: () => undefined,
  beginPath: () => undefined,
  globalAlpha: 1,
  lineCap: "round",
  lineWidth: 1,
  measureText: () => ({ width: 0 }),
  stroke: () => undefined,
  strokeStyle: "",
})) as unknown as typeof HTMLCanvasElement.prototype.getContext;

afterEach(() => {
  cleanup();
  delete window.liveClipperShell;
  document.body.className = "";
});
