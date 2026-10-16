import { describe, expect, it, vi } from "vitest";

import {
  fetchBootstrapStatus,
  fetchTtsPreview,
  initializeBootstrap,
  updateQWeatherApiKey
} from "./bootstrap";
describe("bootstrap api", () => {
  it("读取 bootstrap 状态并提交初始化表单", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            llm: {
              is_ready: false,
              missing: ["qwen.base_url", "qwen.api_key"],
              errors: []
            },
            voice: { provider: "local_streamvox", is_ready: false, phase: "unconfigured", model_id: null, role_name: null, has_confirmed_voice: false },
            has_qweather_api_key: false,
            is_initialized: false
          }),
          {
            headers: {
              "Content-Type": "application/json"
            }
          }
        )
      )
      .mockResolvedValueOnce(new Response(null, { status: 202 }));

    vi.stubGlobal("fetch", fetchMock);

    const status = await fetchBootstrapStatus("http://127.0.0.1:8765");
    await initializeBootstrap("http://127.0.0.1:8765", {
      qweather_api_key: "qweather-key",
      qweather_api_host: "https://abc1234xyz.def.qweatherapi.com"
    });

    expect(status).toEqual({
      llm: {
        is_ready: false,
        missing: ["qwen.base_url", "qwen.api_key"],
        errors: []
      },
      voice: { provider: "local_streamvox", is_ready: false, phase: "unconfigured", model_id: null, role_name: null, has_confirmed_voice: false },
      has_qweather_api_key: false,
      is_initialized: false
    });
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/bootstrap/status");
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/bootstrap/initialize", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        qweather_api_key: "qweather-key",
        qweather_api_host: "https://abc1234xyz.def.qweatherapi.com"
      })
    });

    vi.unstubAllGlobals();
  });

  it("验证并保存和风天气密钥后返回最新服务密钥状态", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          llm: {
            is_ready: true,
            missing: [],
            errors: []
          },
          voice: { provider: "local_streamvox", is_ready: true, phase: "ready", model_id: "qwen3-tts-clone-0.6b-gguf", role_name: "huohua", has_confirmed_voice: true },
          has_qweather_api_key: true,
          is_initialized: true
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    const status = await updateQWeatherApiKey("http://127.0.0.1:8765", {
      qweather_api_key: "qweather-key",
      qweather_api_host: "https://abc1234xyz.def.qweatherapi.com"
    });

    expect(status).toEqual({
      llm: {
        is_ready: true,
        missing: [],
        errors: []
      },
      voice: { provider: "local_streamvox", is_ready: true, phase: "ready", model_id: "qwen3-tts-clone-0.6b-gguf", role_name: "huohua", has_confirmed_voice: true },
      has_qweather_api_key: true,
      is_initialized: true
    });
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/service-keys/qweather", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        qweather_api_key: "qweather-key",
        qweather_api_host: "https://abc1234xyz.def.qweatherapi.com"
      })
    });

    vi.unstubAllGlobals();
  });

  it("bootstrap 状态响应缺少布尔字段时抛出契约错误", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          llm: {
            is_ready: true,
            missing: [],
            errors: []
          },
          voice: { provider: "local_streamvox", is_ready: false, phase: "unconfigured", model_id: null, role_name: null, has_confirmed_voice: false },
          has_qweather_api_key: false,
          is_initialized: "yes"
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchBootstrapStatus("http://127.0.0.1:8765")).rejects.toThrow(
      "读取初始化状态失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });

  it("初始化和试听失败时保留 Backend 的 detail", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "必需模型服务尚未配置" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: "试听资源已被旧任务占用" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(initializeBootstrap("http://127.0.0.1:8765", {
      qweather_api_key: "key",
      qweather_api_host: "https://weather.example.com"
    })).rejects.toThrow("提交初始化信息失败：必需模型服务尚未配置");
    await expect(fetchTtsPreview("http://127.0.0.1:8765", "task-1"))
      .rejects.toThrow("读取试听失败：试听资源已被旧任务占用");

    vi.unstubAllGlobals();
  });
});
