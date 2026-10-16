import { describe, expect, it, vi } from "vitest";

import { HttpApiError, fetchJson } from "./httpClient";

describe("httpClient", () => {
  it("保留设置版本冲突的结构化 detail，供页面刷新权威快照", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "settings_revision_conflict",
              expected_revision: 8,
              actual_revision: 9
            }
          }),
          {
            status: 409,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    const request = fetchJson("http://localhost", "/api/settings", "保存设置失败");

    await expect(request).rejects.toMatchObject({
      name: "HttpApiError",
      status: 409,
      detail: "设置已被其他操作更新（当前版本 9，提交版本 8），请刷新后重试"
    });
    await expect(request).rejects.toThrow("保存设置失败：设置已被其他操作更新");
    vi.unstubAllGlobals();
  });

  it("把 FastAPI 校验错误数组转换成字段和原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["body", "settings", "max_turns"], msg: "Input should be a valid integer" },
              { loc: ["body", "settings", "model"], msg: "String should have at least 1 character" }
            ]
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    try {
      await fetchJson("http://localhost", "/api/background-agent/config", "保存后台助手配置失败");
      throw new Error("expected fetchJson to reject");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpApiError);
      expect((error as HttpApiError).message).toContain("body.settings.max_turns: Input should be a valid integer");
      expect((error as HttpApiError).message).toContain("body.settings.model: String should have at least 1 character");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("保留外部天气服务 503 的可重试提示，不把它折叠成 HTTP 状态码", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "qweather_tls_connection_failed",
              message: "和风天气验证暂时失败，密钥尚未保存：天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。",
              retryable: true
            }
          }),
          {
            status: 503,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    try {
      await fetchJson("http://localhost", "/api/service-keys/qweather", "保存和风天气密钥失败");
      throw new Error("expected fetchJson to reject");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpApiError);
      expect(error).toMatchObject({
        status: 503,
        detail: "和风天气验证暂时失败，密钥尚未保存：天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。"
      });
      expect((error as HttpApiError).message).toContain("请检查 API Host、系统代理和网络后重试");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
