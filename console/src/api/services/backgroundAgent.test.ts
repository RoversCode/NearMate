import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchBackgroundAgentConfig, updateBackgroundAgentConfig } from "./backgroundAgent";

const safeConfig = {
  environment: { ANTHROPIC_BASE_URL: "https://endpoint.example.com" },
  model: "claude-sonnet-4-5",
  effort_level: "high" as const,
  max_turns: 30,
  permission_mode: "acceptEdits" as const,
  credential_type: "api_key" as const,
  credential_configured: true
};

describe("background agent config service", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("读取配置时只消费安全视图", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(safeConfig)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchBackgroundAgentConfig("http://127.0.0.1:8765")).resolves.toEqual(safeConfig);
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/background-agent/config");
  });

  it("保存时把凭据与普通设置分字段提交", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(safeConfig), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const payload = {
      settings: {
        environment: safeConfig.environment,
        model: safeConfig.model,
        effort_level: safeConfig.effort_level,
        max_turns: 0,
        permission_mode: "acceptEdits" as const
      },
      credential_type: "api_key" as const,
      credential_value: "secret-value",
      clear_credential: false
    };

    await updateBackgroundAgentConfig("http://127.0.0.1:8765", payload);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/background-agent/config",
      expect.objectContaining({ method: "PUT", body: JSON.stringify(payload) })
    );
  });

  it("保留 FastAPI 422 字段错误，避免只显示无意义的 HTTP 状态码", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["body", "settings", "max_turns"], msg: "Input should be a valid integer" },
              { loc: ["body", "settings", "model"], msg: "后台工作主模型不能为空" }
            ]
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    await expect(
      updateBackgroundAgentConfig("http://127.0.0.1:8765", {
        settings: {
          environment: safeConfig.environment,
          model: safeConfig.model,
          effort_level: safeConfig.effort_level,
          max_turns: 30,
          permission_mode: safeConfig.permission_mode
        },
        credential_type: safeConfig.credential_type,
        credential_value: "",
        clear_credential: false
      })
    ).rejects.toThrow("body.settings.max_turns: Input should be a valid integer");
  });
});
