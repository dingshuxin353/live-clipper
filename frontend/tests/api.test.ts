import { api, ApiError } from "../src/api";
import { jsonResponse } from "./helpers";

describe("typed API client", () => {
  it("keeps same-origin credentials and maps non-2xx errors", async () => {
    const fetchMock = vi.fn(() => jsonResponse({ message: "配置错误" }, 400));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api("/api/config")).rejects.toEqual(
      expect.objectContaining({ name: "ApiError", message: "配置错误", status: 400 }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/config",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("maps network and invalid JSON failures without logging payloads", async () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("secret-token"))));
    await expect(api("/api/config")).rejects.toEqual(new ApiError("网络连接失败"));
    expect(consoleSpy).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it("forwards AbortSignal so unmounted callers can ignore stale results", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn((_path: string, options: RequestInit) => {
      expect(options.signal).toBe(controller.signal);
      return jsonResponse({ ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);
    await api("/api/service", {}, controller.signal);
  });

  it("maps frozen project API error envelopes without stringifying objects", async () => {
    vi.stubGlobal("fetch", vi.fn(() => jsonResponse({
      ok: false,
      error: { code: "validation_failed", message: "项目配置未通过校验", fields: { name: "必填字段" } },
    }, 422)));

    await expect(api("/api/projects")).rejects.toEqual(expect.objectContaining({
      name: "ApiError",
      message: "项目配置未通过校验",
      status: 422,
      code: "validation_failed",
      fields: { name: "必填字段" },
    }));
  });
});
