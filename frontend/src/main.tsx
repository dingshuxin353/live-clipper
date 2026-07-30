import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Theme } from "@astryxdesign/core/theme";
import { stoneTheme } from "@astryxdesign/theme-stone/built";

import { App } from "./App";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-stone/theme.css";
import "./theme/venus-stone-overrides.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Theme theme={stoneTheme} mode="light">
      <App />
    </Theme>
  </StrictMode>,
);
