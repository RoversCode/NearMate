// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveKitRoomHandlers } from "../lib/livekitRoomTypes";
import { LiveKitSessionRequestError } from "../../../shared/liveKitSessionRequest";
import { useAvatarLiveKitSession, type CompanionAvatarLiveKitBridge } from "./useAvatarLiveKitSession";

const room = vi.hoisted(() => ({ connect: vi.fn(), disconnect: vi.fn() }));
vi.mock("../lib/livekitRoom", () => ({ LiveKitRoomClient: class { connect = room.connect; disconnect = room.disconnect; } }));
vi.mock("../lib/assistantAudioPlayback", () => ({ AssistantAudioPlayback: class {} }));
const session = { url: "ws://localhost:7880", room_name: "test", desktop_ui: { identity: "ui", token: "test-token" } };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function createInput(createSession = vi.fn(async () => session)): Parameters<typeof useAvatarLiveKitSession>[0] {
  const bridge: CompanionAvatarLiveKitBridge = {
    createLiveKitSession: createSession, reportDiagnostic: vi.fn(),
    runtime: { controlGateway: { httpBaseUrl: "http://localhost:8765", websocketUrl: "ws://localhost:8765/ws/control" } }
  };
  return {
    avatarBridge: bridge, dispatch: vi.fn(), audioDeviceSessionReloadRef: { current: null },
    latestAudioDeviceChangeRequestIdRef: { current: null }, onAssistantPlaybackElementChangedRef: { current: undefined },
    onLocalListeningHintChangedRef: { current: undefined }, playoutDeliveryCoordinatorRef: { current: null }
  };
}

function changeDevice(input: Parameters<typeof useAvatarLiveKitSession>[0], requestId: string) {
  act(() => input.audioDeviceSessionReloadRef.current?.({
    request_id: requestId, requested_at: 1,
    selection: { mode: "system_default", microphone_id: null, microphone_name: null, speaker_loopback_id: null, speaker_loopback_name: null }
  }));
}

async function flush() { await act(async () => { await Promise.resolve(); }); }
async function advance(milliseconds: number) { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); }); }

describe("Avatar 媒体会话恢复", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    room.connect.mockReset().mockImplementation(async (_config, handlers: LiveKitRoomHandlers) => handlers.onStatusChange("connected"));
    room.disconnect.mockReset().mockResolvedValue(undefined);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("首次瞬时失败后退避重连成功，提示不包含 SDK 私密原文", async () => {
    const createSession = vi.fn(async () => session).mockRejectedValueOnce(new Error("secret-value"));
    const input = createInput(createSession);
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    expect(createSession).toHaveBeenCalledTimes(1);
    await advance(999);
    expect(createSession).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(createSession).toHaveBeenCalledTimes(2);
    expect(room.connect).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(vi.mocked(input.dispatch).mock.calls)).not.toContain("secret-value");
    expect(JSON.stringify(vi.mocked(input.avatarBridge!.reportDiagnostic).mock.calls)).not.toContain("secret-value");
    expect(input.dispatch).toHaveBeenCalledWith(expect.objectContaining({ payload: expect.objectContaining({ text: "语音连接暂时不可用，正在自动重试。" }) }));
  });

  it("缺少配置明确提示并停止自动重试", async () => {
    const createSession = vi.fn(async () => session).mockRejectedValue(new LiveKitSessionRequestError("livekit_not_configured"));
    const input = createInput(createSession);
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    await advance(300_000);
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(input.dispatch).toHaveBeenCalledWith(expect.objectContaining({ payload: expect.objectContaining({ text: "语音服务尚未配置，请重新安装完整版本后重试。" }) }));
  });

  it("长期故障按 1/2/4/8/16/30 秒退避，之后频率不超过每 30 秒一次", async () => {
    const createSession = vi.fn(async () => session).mockRejectedValue(new Error("offline"));
    renderHook(() => useAvatarLiveKitSession(createInput(createSession)));
    await flush();
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      const previousCount = createSession.mock.calls.length;
      await advance(delay - 1);
      expect(createSession).toHaveBeenCalledTimes(previousCount);
      await advance(1);
      expect(createSession).toHaveBeenCalledTimes(previousCount + 1);
    }
  });

  it("初始化请求未返回时切换设备，只等待旧调用退出，再申请最新会话", async () => {
    const oldSession = deferred<typeof session>();
    const createSession = vi.fn(async () => session).mockReturnValueOnce(oldSession.promise);
    const input = createInput(createSession);
    renderHook(() => useAvatarLiveKitSession(input));
    changeDevice(input, "new-device");
    await flush();
    expect(createSession).toHaveBeenCalledTimes(1);
    oldSession.resolve({ ...session, desktop_ui: { ...session.desktop_ui, token: "old-token" } });
    await flush();
    expect(createSession).toHaveBeenCalledTimes(2);
    expect(room.connect).toHaveBeenCalledTimes(1);
    expect(room.connect.mock.calls[0][0].token).toBe("test-token");
  });

  it("已被新设备取代的 room 回调不能再排重试或覆盖播放状态", async () => {
    const input = createInput();
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    const oldHandlers = room.connect.mock.calls[0][1] as LiveKitRoomHandlers;
    changeDevice(input, "new-device");
    await flush();
    vi.mocked(input.avatarBridge!.reportDiagnostic).mockClear();
    oldHandlers.onStatusChange("failed", "old-private-error");
    await advance(60_000);
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(2);
    expect(input.avatarBridge!.reportDiagnostic).not.toHaveBeenCalled();
  });

  it("SDK 最终断开后重新获取会话，重复断开事件不创建并发请求", async () => {
    const input = createInput();
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    const handlers = room.connect.mock.calls[0][1] as LiveKitRoomHandlers;
    handlers.onStatusChange("disconnected");
    handlers.onStatusChange("disconnected");
    await advance(1000);
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(2);
  });

  it("麦克风失败回执未返回时，下一次设备切换仍能建立新会话", async () => {
    const input = createInput(vi.fn(async () => ({ ...session, audio_input_status: "audio_unavailable" as const })));
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    const receipt = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => receipt.promise));
    changeDevice(input, "first-device");
    await flush();
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(2);
    changeDevice(input, "second-device");
    await flush();
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(3);
    receipt.resolve(new Response("{}"));
    await flush();
  });

  it("卸载清除退避计时器且迟到请求不能连接", async () => {
    const createSession = vi.fn(async () => session).mockRejectedValueOnce(new Error("offline"));
    const input = createInput(createSession);
    const view = renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    view.unmount();
    await advance(60_000);
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(input.audioDeviceSessionReloadRef.current).toBeNull();
  });

  it("窗口卸载将取消送到 main broker，迟到会话不能进入房间", async () => {
    const pending = deferred<typeof session>();
    const input = createInput(vi.fn(async () => pending.promise));
    const cancel = vi.fn(async () => {});
    input.avatarBridge!.cancelLiveKitSession = cancel;
    const view = renderHook(() => useAvatarLiveKitSession(input));
    view.unmount();
    expect(cancel).toHaveBeenCalledOnce();
    pending.resolve(session);
    await flush();
    expect(room.connect).not.toHaveBeenCalled();
  });

  it("新挂载遇到旧 broker 的取消错误时重试，不把新一代一起永久取消", async () => {
    const createSession = vi.fn(async () => session).mockRejectedValueOnce(new DOMException("cancelled previous broker", "AbortError"));
    const input = createInput(createSession);
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    await advance(1000);
    expect(createSession).toHaveBeenCalledTimes(2);
    expect(room.connect).toHaveBeenCalledTimes(1);
  });

  it("旧连接清理卡住时只等待同一个清理操作，不叠加请求，恢复后才申请新会话", async () => {
    const input = createInput();
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    const pendingDisconnect = deferred<void>();
    room.disconnect.mockReturnValueOnce(pendingDisconnect.promise);
    changeDevice(input, "new-device");
    await advance(20_000);
    expect(input.dispatch).toHaveBeenCalledWith(expect.objectContaining({ payload: expect.objectContaining({ text: "语音连接等待超时，正在自动重试。" }) }));
    await advance(1000);
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(1);
    expect(room.disconnect).toHaveBeenCalledTimes(1);
    pendingDisconnect.resolve();
    await flush();
    expect(input.avatarBridge!.createLiveKitSession).toHaveBeenCalledTimes(2);
  });

  it("新设备请求取消旧 HTTP body reader，并忽略旧响应", async () => {
    let oldSignal: AbortSignal | null = null;
    const body = deferred<unknown>();
    // 首次响应故意卡在正文；后续请求按 URL 返回可用会话及设备应用响应。
    let count = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (!url.endsWith("/api/livekit/session")) return new Response("{}");
      if (count++ === 0) {
        oldSignal = init?.signal ?? null;
        return { ok: true, json: () => body.promise } as Response;
      }
      return new Response(JSON.stringify(session));
    }));
    const input = createInput();
    delete input.avatarBridge!.createLiveKitSession;
    renderHook(() => useAvatarLiveKitSession(input));
    await flush();
    changeDevice(input, "replacement");
    await flush();
    expect((oldSignal as AbortSignal | null)?.aborted).toBe(true);
    expect(count).toBe(2);
    body.resolve({ ...session, desktop_ui: { ...session.desktop_ui, token: "stale" } });
    await flush();
    expect(room.connect).toHaveBeenCalledTimes(1);
    expect(room.connect.mock.calls[0][0].token).toBe("test-token");
  });
});
