import { afterEach, describe, expect, it, vi } from "vitest";
import { createLiveKitSessionClient } from "./liveKitSessionClient";

/**
 * createJsonResponse 用于构造带 JSON body 的标准 Response。
 * 核心入参是后端响应对象与 HTTP 状态码；预期输出是 fetch mock 可以直接返回的 Response。
 * 边界：这里不模拟网络异常，网络异常由 fetch 自身 rejected promise 表达，本测试专注 session client 的解析和状态码处理。
 */
function createJsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json"
    }
  });
}

describe("LiveKit session client", () => {
  afterEach(() => vi.useRealTimers());

  it.each([
    [{ detail: "livekit is not configured" }, "livekit_not_configured", "语音服务尚未配置，请重新安装完整版本后重试。"],
    [{ code: "livekit_not_configured", retryable: false, detail: "secret-value" }, "livekit_not_configured", "语音服务尚未配置，请重新安装完整版本后重试。"],
    [{ code: "assistant_not_ready", retryable: true, detail: "secret-value" }, "assistant_not_ready", "助手正在准备，请稍候，连接会自动恢复。"],
    [{ code: "livekit_connection_failed", retryable: true, detail: "secret-value" }, "livekit_connection_failed", "语音连接暂时不可用，正在自动重试。"],
    [{ detail: "private-server-error secret-value" }, "livekit_connection_failed", "语音连接暂时不可用，正在自动重试。"]
  ])("服务失败只投影安全分类，不透传响应正文：%j", async (body, code, message) => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse(body, 503))
    });
    await expect(client.createSession()).rejects.toMatchObject({ code, message });
  });

  it.each(["headers", "success-body", "error-body"])("请求在 %s 卡住时仍在 20 秒内失败并取消传输", async phase => {
    vi.useFakeTimers();
    let requestSignal: AbortSignal | undefined;
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async (_url, init) => {
        requestSignal = init?.signal ?? undefined;
        if (phase === "headers") return new Promise<Response>(() => {});
        return {
          ok: phase === "success-body",
          status: phase === "success-body" ? 200 : 503,
          json: () => new Promise<unknown>(() => {})
        } as Response;
      })
    });
    const result = client.createSession().catch(error => error);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(await result).toMatchObject({ code: "livekit_request_timeout" });
    expect(requestSignal?.aborted).toBe(true);
  });

  it("底层网络异常不泄露地址或凭据", async () => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => { throw new Error("wss://user:secret-value@example.test refused"); })
    });
    await expect(client.createSession()).rejects.toMatchObject({
      code: "livekit_connection_failed", message: "语音连接暂时不可用，正在自动重试。"
    });
  });
  it("会请求控制服务 session 接口，并把嵌套桌面身份解析成主进程内部结构", async () => {
    const fetchMock = vi.fn(async () => createJsonResponse({
      url: "wss://livekit.example.test",
      room_name: "desktop-room",
      desktop_ui: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      },
      desktop_aec: {
        identity: "desktop-aec-identity",
        token: "desktop-aec-token"
      },
      aec_sidecar_debug_audio: {
        enabled: true,
        output_dir:
          "G:\\Workspace\\projects\\virtual_companion\\backend\\data\\debug-audio\\aec-sidecar",
        max_runs: 7
      },
      audio_devices: {
        mode: "manual",
        microphone_id: "mic-usb",
        microphone_name: "麦克风 (USB Camera)",
        speaker_loopback_id: "speaker-loopback",
        speaker_loopback_name: "扬声器 (Realtek USB Audio)"
      }
    }));
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });

    const session = await client.createSession();

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/livekit/session", { signal: expect.any(AbortSignal) });
    expect(session).toEqual({
      url: "wss://livekit.example.test",
      roomName: "desktop-room",
      desktopUi: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      },
      desktopAec: {
        identity: "desktop-aec-identity",
        token: "desktop-aec-token"
      },
      aecSidecarDebugAudio: {
        enabled: true,
        outputDir:
          "G:\\Workspace\\projects\\virtual_companion\\backend\\data\\debug-audio\\aec-sidecar",
        maxRuns: 7
      },
      audioDeviceSelection: {
        mode: "manual",
        microphoneId: "mic-usb",
        microphoneName: "麦克风 (USB Camera)",
        speakerLoopbackId: "speaker-loopback",
        speakerLoopbackName: "扬声器 (Realtek USB Audio)"
      }
    });
  });

  it("未知非 2xx 错误使用安全的连接失败分类", async () => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse({ detail: "service unavailable" }, 503))
    });

    await expect(client.createSession()).rejects.toThrow(
      "语音连接暂时不可用，正在自动重试。"
    );
  });

  it.each([
    {
      name: "url 为空字符串",
      payload: {
        url: "",
        room_name: "desktop-room",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        },
        desktop_aec: {
          identity: "desktop-aec-identity",
          token: "desktop-aec-token"
        }
      }
    },
    {
      name: "room_name 为空字符串",
      payload: {
        url: "wss://livekit.example.test",
        room_name: "   ",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        },
        desktop_aec: {
          identity: "desktop-aec-identity",
          token: "desktop-aec-token"
        }
      }
    }
  ])("后端返回 $name 时，会按缺失房间基础字段处理", async ({ payload }) => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse(payload))
    });

    await expect(client.createSession()).rejects.toThrow(
      "语音服务响应异常，请检查安装版本。"
    );
  });

  it("后端缺少 desktop_ui 或 desktop_aec 嵌套身份时，会抛出清晰的响应结构错误", async () => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse({
        url: "wss://livekit.example.test",
        room_name: "desktop-room",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        }
      }))
    });

    await expect(client.createSession()).rejects.toThrow(
      "语音服务响应异常，请检查安装版本。"
    );
  });

  it("后端缺少 sidecar 调试配置时，会回退到默认关闭配置，避免前后端短暂版本错位导致 session 失败", async () => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse({
        url: "wss://livekit.example.test",
        room_name: "desktop-room",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        },
        desktop_aec: {
          identity: "desktop-aec-identity",
          token: "desktop-aec-token"
        }
      }))
    });

    await expect(client.createSession()).resolves.toEqual({
      url: "wss://livekit.example.test",
      roomName: "desktop-room",
      desktopUi: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      },
      desktopAec: {
        identity: "desktop-aec-identity",
        token: "desktop-aec-token"
      },
      aecSidecarDebugAudio: {
        enabled: false,
        outputDir: "",
        maxRuns: 10
      },
      audioDeviceSelection: {
        mode: "system_default",
        microphoneId: null,
        microphoneName: null,
        speakerLoopbackId: null,
        speakerLoopbackName: null
      }
    });
  });

  it("后端写坏已启用的 sidecar 调试配置时，会抛出清晰的响应结构错误", async () => {
    const client = createLiveKitSessionClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => createJsonResponse({
        url: "wss://livekit.example.test",
        room_name: "desktop-room",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        },
        desktop_aec: {
          identity: "desktop-aec-identity",
          token: "desktop-aec-token"
        },
        aec_sidecar_debug_audio: {
          enabled: true,
          output_dir: "   ",
          max_runs: 0
        }
      }))
    });

    await expect(client.createSession()).rejects.toThrow(
      "语音服务响应异常，请检查安装版本。"
    );
  });
});
