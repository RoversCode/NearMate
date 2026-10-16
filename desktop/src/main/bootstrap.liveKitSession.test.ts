import { describe, expect, it, vi } from "vitest";
import { setupBootstrapHarness } from "./bootstrap.testHarness";
describe("bootstrap LiveKit session", () => {
  it("livekit:create-session 会通过主进程请求后端，启动 sidecar，并只向 renderer 返回 UI payload", async () => {
    const startAecSidecar = vi.fn(async () => undefined);
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: startAecSidecar,
      stop: vi.fn()
    }));
    const createSession = vi.fn(async () => ({
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
    }));
    const createLiveKitSessionClient = vi.fn(() => ({
      createSession
    }));

    /**
     * 主进程 IPC 只关心 session client 的窄接口。
     * 这里 mock 服务层，是为了让本用例聚焦 bootstrap 是否正确解析环境变量并返回 UI 专用结构。
     */
    vi.doMock("./services/liveKitSessionClient", () => ({
      createLiveKitSessionClient
    }));
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));

    const { ipcMainMock } = await setupBootstrapHarness({
      controlHttpUrl: "http://127.0.0.1:9000/console/"
    });

    const rendererPayload = await ipcMainMock.invoke("livekit:create-session");

    expect(rendererPayload).toEqual({
      url: "wss://livekit.example.test",
      room_name: "desktop-room",
      desktop_ui: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      },
      audio_input_status: "available"
    });
    expect(JSON.stringify(rendererPayload)).not.toContain("desktop-aec-token");
    expect(JSON.stringify(rendererPayload)).not.toContain("desktop_aec");
    expect(createLiveKitSessionClient).toHaveBeenCalledWith({
      httpBaseUrl: "http://127.0.0.1:9000"
    });
    expect(createSession).toHaveBeenCalledOnce();
    expect(startAecSidecar).toHaveBeenCalledWith({
      livekitUrl: "wss://livekit.example.test",
      roomName: "desktop-room",
      identity: "desktop-aec-identity",
      token: "desktop-aec-token",
      debugAudio: {
        enabled: false,
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

  it("AEC sidecar supervisor 默认使用仓库根目录 .venv 作为 Python 环境", async () => {
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: vi.fn(),
      stop: vi.fn()
    }));

    /**
     * 业务意图是把桌面端和后端统一到仓库根目录的 Python 虚拟环境。
     * 如果这里再次指向 backend 子目录虚拟环境，开发机会悄悄恢复成多虚拟环境状态。
     */
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));

    await setupBootstrapHarness();

    expect(createAecSidecarSupervisor).toHaveBeenCalledWith(expect.objectContaining({
      pythonExe: expect.stringContaining(".venv\\Scripts\\python.exe")
    }));
    expect(createAecSidecarSupervisor).not.toHaveBeenCalledWith(expect.objectContaining({
      pythonExe: expect.stringContaining("backend\\.venv")
    }));
  });

  it("livekit:create-session sidecar 启动失败时，会返回 UI session 并标记输入不可用", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const startAecSidecar = vi.fn(async () => {
      throw new Error("AEC sidecar failed to start.");
    });
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: startAecSidecar,
      stop: vi.fn()
    }));
    const createSession = vi.fn(async () => ({
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
        outputDir:
          "G:\\Workspace\\projects\\virtual_companion\\backend\\data\\debug-audio\\aec-sidecar",
        maxRuns: 7
      },
      audioDeviceSelection: {
        mode: "system_default",
        microphoneId: null,
        microphoneName: null,
        speakerLoopbackId: null,
        speakerLoopbackName: null
      }
    }));
    const createLiveKitSessionClient = vi.fn(() => ({
      createSession
    }));

    /**
     * 这个用例把后端 session 成功与 sidecar 启动失败分开。
     * 业务意图是证明 renderer 仍能连接房间播放助手音频，但输入侧会被明确标记为不可用。
     */
    vi.doMock("./services/liveKitSessionClient", () => ({
      createLiveKitSessionClient
    }));
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));

    const { ipcMainMock } = await setupBootstrapHarness();

    await expect(ipcMainMock.invoke("livekit:create-session")).resolves.toMatchObject({
      audio_input_status: "audio_unavailable",
      desktop_ui: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      }
    });
    expect(createSession).toHaveBeenCalledOnce();
    expect(startAecSidecar).toHaveBeenCalledOnce();
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[companion-main] 麦克风组件不可用，继续建立语音播放连接。"
    );
  });

  it("livekit:create-session 后端请求失败时，会把 session client 的稳定错误传播给调用方", async () => {
    const startAecSidecar = vi.fn();
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: startAecSidecar,
      stop: vi.fn()
    }));
    const originalFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ detail: "service unavailable" }),
      {
        status: 503,
        headers: {
          "Content-Type": "application/json"
        }
      }
    ));

    /**
     * 本用例使用真实 session client，仅替换网络边界。
     * 业务意图是证明 bootstrap handler 不吞掉后端会话失败，也不把错误改写成 renderer 无法诊断的形态。
     */
    vi.doUnmock("./services/liveKitSessionClient");
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));
    globalThis.fetch = fetchMock as typeof fetch;

    try {
      const { ipcMainMock } = await setupBootstrapHarness();

      await expect(ipcMainMock.invoke("livekit:create-session")).rejects.toThrow(
        "语音连接暂时不可用，正在自动重试。"
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8765/api/livekit/session", { signal: expect.any(AbortSignal) }
      );
      expect(startAecSidecar).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

});
