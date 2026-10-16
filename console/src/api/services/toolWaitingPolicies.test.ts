import { describe, expect, it, vi } from "vitest";

import {
  fetchToolWaitingPolicies,
  updateToolWaitingFamilyPolicy,
  updateToolWaitingToolPolicy
} from "./toolWaitingPolicies";
describe("tool waiting policy api", () => {
  it("读取后端工具目录和等待策略，不需要前端硬编码工具名", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          families: [
            {
              family_id: "weather",
              display_name: "天气",
              latency_class: "external_api",
              default_waiting_enabled: true,
              default_waiting_phrases: ["好，我查一下天气。"],
              builtin_policy: { enabled: true, phrases: ["好，我查一下天气。"] },
              user_policy: null,
              effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" },
              tools: [
                {
                  name: "weather_overview",
                  display_name: "实时天气",
                  family_id: "weather",
                  family_display_name: "天气",
                  latency_class: "external_api",
                  user_override: null,
                  effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" }
                }
              ]
            }
          ],
          user_config: { families: {}, tools: {} }
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const payload = await fetchToolWaitingPolicies("http://127.0.0.1:8765");

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/tool-waiting-policies");
    expect(payload.families[0].tools[0].name).toBe("weather_overview");
    expect(payload.families[0].effective_policy.source).toBe("builtin");

    vi.unstubAllGlobals();
  });

  it("保存 family 策略时使用 PUT 并返回最新策略视图", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("PUT");
      expect(JSON.parse(String(init?.body))).toEqual({
        enabled: true,
        phrases: ["我看一下天气。"]
      });
      return new Response(
        JSON.stringify({
          families: [
            {
              family_id: "weather",
              display_name: "天气",
              latency_class: "external_api",
              default_waiting_enabled: true,
              default_waiting_phrases: ["好，我查一下天气。"],
              builtin_policy: { enabled: true, phrases: ["好，我查一下天气。"] },
              user_policy: { enabled: true, phrases: ["我看一下天气。"] },
              effective_policy: { enabled: true, phrases: ["我看一下天气。"], source: "family" },
              tools: []
            }
          ],
          user_config: { families: { weather: { enabled: true, phrases: ["我看一下天气。"] } }, tools: {} }
        }),
        { status: 202, headers: { "Content-Type": "application/json" } }
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const payload = await updateToolWaitingFamilyPolicy("http://127.0.0.1:8765", "weather", {
      enabled: true,
      phrases: ["我看一下天气。"]
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/tool-waiting-policies/families/weather",
      expect.objectContaining({ method: "PUT" })
    );
    expect(payload.families[0].user_policy?.phrases).toEqual(["我看一下天气。"]);

    vi.unstubAllGlobals();
  });

  it("保存和清除单工具 override 时使用后端 tool 名称", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return new Response(
        JSON.stringify({
          families: [],
          user_config: { families: {}, tools: {} }
        }),
        { status: 202, headers: { "Content-Type": "application/json" } }
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await updateToolWaitingToolPolicy("http://127.0.0.1:8765", "weather_overview", {
      inherit_family: false,
      enabled: true,
      phrases: ["我单独查实时天气。"]
    });
    await updateToolWaitingToolPolicy("http://127.0.0.1:8765", "weather_overview", {
      inherit_family: true
    });

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/tool-waiting-policies/tools/weather_overview");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      inherit_family: false,
      enabled: true,
      phrases: ["我单独查实时天气。"]
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      inherit_family: true
    });

    vi.unstubAllGlobals();
  });
});

