import { CONFIG_FIELDS, defaultConfig, getConfigValue, setConfigValue } from "../src/config";

describe("configuration mapping", () => {
  it("keeps all 47 unique backend fields", () => {
    expect(CONFIG_FIELDS).toHaveLength(47);
    expect(new Set(CONFIG_FIELDS)).toHaveLength(47);
    expect(CONFIG_FIELDS).toContain("recording_source_default.source_dir");
    expect(CONFIG_FIELDS).toContain("review_automation_model.temperature");
    expect(CONFIG_FIELDS).toContain("web.host");
  });

  it("reads and updates a nested field without mutating the loaded config", () => {
    const original = defaultConfig();
    const updated = setConfigValue(original, "web.port", 9999);
    expect(getConfigValue(updated, "web.port")).toBe(9999);
    expect(getConfigValue(original, "web.port")).toBe(8765);
  });
});
