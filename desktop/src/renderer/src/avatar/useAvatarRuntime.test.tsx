// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectionStatus } from "../types/events";
import type { SettingsSnapshot } from "../types/settingsSnapshot";
import { useAvatarRuntime, type AvatarRuntime } from "./useAvatarRuntime";
import type { ShortcutFeedback } from "../../../shared/shortcutFeedback";

type LiveKitRoomHandlersFixture = {
  onStatusChange(status: ConnectionStatus, reason?: string): void;
  onAssistantTrackSubscribed(identity: string): void;
  onLocalListeningHintChanged?(active: boolean): void;
};

type LiveKitRoomConnectCall = [
  {
    url?: string;
    token?: string;
    autoPublishMicrophone?: boolean;
  },
  LiveKitRoomHandlersFixture
];

type LiveKitRoomClientMockInstance = {
  connect: ReturnType<typeof vi.fn<(...args: LiveKitRoomConnectCall) => Promise<void>>>;
  disconnect: ReturnType<typeof vi.fn<() => Promise<void>>>;
  config?: {
    assistantAudioPlayback?: {
      playTrack(track: unknown): Promise<void>;
      release(): void;
    };
  };
};

type FakeControlWebSocketInstance = {
  onopen: (() => void) | null;
  onmessage: ((message: { data: string }) => void) | null;
};

const liveKitRoomMockState = vi.hoisted(() => ({
  instances: [] as LiveKitRoomClientMockInstance[]
}));
const controlWebSocketMockState = vi.hoisted(() => ({
  instances: [] as FakeControlWebSocketInstance[]
}));

vi.mock("../lib/livekitRoom", () => ({
  LiveKitRoomClient: class LiveKitRoomClient {
    readonly connect = vi.fn<(...args: LiveKitRoomConnectCall) => Promise<void>>(
      async () => undefined
    );
    readonly disconnect = vi.fn<() => Promise<void>>(async () => undefined);

    constructor(config?: LiveKitRoomClientMockInstance["config"]) {
      this.config = config;
      liveKitRoomMockState.instances.push(this);
    }

    readonly config?: LiveKitRoomClientMockInstance["config"];
  }
}));

const settingsSnapshotFixture: SettingsSnapshot = {
  settings_revision: 1,
  show_speech_bubbles: true,
  active_character_caption_timing: {
    ms_per_unit: 100,
    calibration_text: "我在这里，听见你说话了。",
    duration_ms: 4000,
    unit_count: 40,
    measured_at: 1710000000
  },
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  avatar_visibility_shortcut_enabled: true,
  avatar_visibility_shortcut: "CommandOrControl+Q",
  selection_capture_shortcut_enabled: true,
  selection_capture_shortcut: "CommandOrControl+Alt+C",
  selected_live2d_model_id: "Haru",
  user_self_intro: "",
  user_extra_notes: "",
  streamvox_device: "auto",
  streamvox_llama_backend: null,
  speaker_verification_enabled: false,
  speaker_whitelist_audio_paths: [],
  audio_device_mode: "system_default",
  audio_input_device_id: null,
  audio_input_device_name: null,
  audio_output_reference_device_id: null,
  audio_output_reference_device_name: null,
  background_agent: {
    environment: {},
    model: "",
    effort_level: null,
    max_turns: 30,
    permission_mode: "acceptEdits",
    skills: {}
  }
};

/**
 * 测试用 LiveKit session 载荷。
 * 核心字段覆盖 renderer 进房所需的地址和 token，额外身份字段用于保持与 main broker 返回结构一致。
 */
type LiveKitSessionPayload = {
  url: string;
  room_name: string;
  audio_input_status?: "available" | "audio_unavailable";
  desktop_ui: {
    identity: string;
    token: string;
  };
  desktop_aec?: {
    identity: string;
    token: string;
  };
};

const liveKitSessionFixture: LiveKitSessionPayload = {
  url: "wss://livekit.example.test",
  room_name: "avatar-room-1",
  desktop_ui: {
    identity: "avatar-user-1",
    token: "token-avatar-1"
  }
};

const originalFetch = globalThis.fetch;
const originalWebSocket = globalThis.WebSocket;

/**
 * 构造可由测试手动完成的 Promise。
 * 核心输出是延后控制异步请求结果，便于复现组件卸载后的迟到 session 响应或失败。
 */
function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });

  return {
    promise,
    reject,
    resolve
  };
}

/**
 * FakeControlWebSocket 是控制面连接测试替身。
 * 核心入参是控制网关地址，预期输出是一个不会真实联网但能满足 ControlSocket 生命周期的 WebSocket。
 * 边界异常：这里不主动触发 open，避免 hook 测试把注意力放到控制通道状态事件上。
 */
class FakeControlWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  /** 当前连接状态只用于满足 ControlSocket 的 readyState 判断。 */
  readyState = FakeControlWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;

  /**
   * 初始化假 WebSocket。
   * 核心入参是目标 URL；测试不需要读取它，但保留字段方便失败时排查连接目标。
   */
  constructor(readonly url: string) {
    controlWebSocketMockState.instances.push(this);
  }

  /**
   * 关闭假连接。
   * 业务意图是让 hook 卸载时的控制面清理可以安全执行，不残留真实网络行为。
   */
  close(): void {
    this.readyState = FakeControlWebSocket.CLOSED;
    this.onclose?.();
  }
}

type AvatarRuntimeProbeProps = {
  bridge: {
    createLiveKitSession?: () => Promise<LiveKitSessionPayload>;
    reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
    getChatMode?(): Promise<"collapsed" | "expanded" | null>;
    onChatModeChanged?(listener: (mode: "collapsed" | "expanded") => void): () => void;
    onShortcutFeedback?(listener: (feedback: ShortcutFeedback) => void): () => void;
    openBackgroundAgentInteraction?(requestId: string): void;
    runtime: {
      controlGateway?: {
        websocketUrl: string;
        httpBaseUrl: string;
      };
    };
  };
  onAssistantPlaybackElementChanged?(element: HTMLMediaElement | null): void;
  onLocalListeningHintChanged?(active: boolean): void;
  onRuntime?: (runtime: AvatarRuntime) => void;
};

/**
 * AvatarRuntimeProbe 只负责真实挂载 hook。
 * 核心入参是 preload bridge fixture；预期输出为空 DOM，因为测试关注副作用而不是 UI。
 */
function AvatarRuntimeProbe({
  bridge,
  onAssistantPlaybackElementChanged,
  onLocalListeningHintChanged,
  onRuntime
}: AvatarRuntimeProbeProps) {
  const runtime = useAvatarRuntime(bridge, {
    onAssistantPlaybackElementChanged,
    onLocalListeningHintChanged
  });
  onRuntime?.(runtime);
  return null;
}

/**
 * 等待 React effect 里连续排队的 Promise 完成。
 * 业务意图是让 settings fetch 与 LiveKit session fetch 都有机会落到断言可见状态。
 */
async function flushHookEffects(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

/**
 * 向头像运行时注入后端控制事件。
 * 核心入参是事件对象；预期输出是 Fake WebSocket 的 message handler 被同步触发。
 */
function emitControlEvent(event: unknown): void {
  const socket = controlWebSocketMockState.instances[0];

  if (!socket) {
    throw new Error("Control WebSocket was not created.");
  }

  act(() => {
    socket.onmessage?.({
      data: JSON.stringify(event)
    });
  });
}

/**
 * 创建 avatar runtime 测试 bridge。
 * 核心输出是固定控制网关地址、main 代理 session mock 和诊断 spy，便于断言产品路径不再直连 session 接口。
 * 边界异常：传入 null 时刻意移除 createLiveKitSession，用于覆盖非 Electron 或测试环境的 direct fetch fallback。
 */
function createBridge(
  reportDiagnostic = vi.fn(),
  createLiveKitSession: (() => Promise<LiveKitSessionPayload>) | null = vi.fn(
    async () => liveKitSessionFixture
  )
): AvatarRuntimeProbeProps["bridge"] {
  return {
    createLiveKitSession: createLiveKitSession ?? undefined,
    reportDiagnostic,
    runtime: {
      controlGateway: {
        websocketUrl: "ws://127.0.0.1:8765/ws/control",
        httpBaseUrl: "http://127.0.0.1:8765"
      }
    }
  };
}

/**
 * 创建按 URL 分流的 fetch fake。
 * 核心入参是 LiveKit session 响应；预期输出能同时满足 settings 水合与 session 建连。
 */
function installFetchMock(
  sessionResponse: Response | Promise<Response>,
  dismissResponse?: (init?: RequestInit) => Promise<Response>
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);

    if (url === "http://127.0.0.1:8765/api/settings") {
      return new Response(JSON.stringify(settingsSnapshotFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url === "http://127.0.0.1:8765/api/livekit/session") {
      return sessionResponse;
    }

    if (url === "http://127.0.0.1:8765/api/audio/devices/apply-result") {
      return new Response(JSON.stringify({ accepted: true }), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url === "http://127.0.0.1:8765/api/timed-reminders/dismiss") {
      if (dismissResponse) {
        return dismissResponse(init);
      }
      const body = JSON.parse(String(init?.body)) as {
        runtime_id: string;
        reminder_ids: string[];
      };
      return new Response(JSON.stringify({
        runtime_id: body.runtime_id,
        dismissed_ids: body.reminder_ids
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }

    throw new Error(`unexpected fetch url: ${url}`);
  });

  globalThis.fetch = fetchMock as typeof fetch;
  return fetchMock;
}

/**
 * 读取测试期间上报给后端的音频设备应用结果。
 * 核心入参是 fetch mock；预期输出是已经 JSON 解析的请求体列表。
 */
function readAudioDeviceApplyResultBodies(fetchMock: ReturnType<typeof vi.fn>): unknown[] {
  return fetchMock.mock.calls
    .filter(([input]) => String(input) === "http://127.0.0.1:8765/api/audio/devices/apply-result")
    .map(([, init]) => {
      const body = (init as RequestInit | undefined)?.body;
      return JSON.parse(String(body));
    });
}

describe("useAvatarRuntime LiveKit session", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    liveKitRoomMockState.instances.length = 0;
    controlWebSocketMockState.instances.length = 0;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    globalThis.WebSocket = FakeControlWebSocket as unknown as typeof WebSocket;
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
    globalThis.fetch = originalFetch;
    globalThis.WebSocket = originalWebSocket;
  });

  it.each([false, true])("新人物设置先推送后，迟到初始化读取不能覆盖它（默认回退：%s）", async useDefaultFallback => {
    const initialSettingsResponse = createDeferred<Response>();
    const fetchMock = installFetchMock(Response.json(liveKitSessionFixture));
    fetchMock.mockImplementation(async () => initialSettingsResponse.promise);
    const bridge = createBridge();
    let latestRuntime: AvatarRuntime | undefined;
    act(() => root.render(<AvatarRuntimeProbe bridge={bridge} onRuntime={runtime => { latestRuntime = runtime; }} />));
    expect(latestRuntime?.settingsHydrated).toBe(false);

    emitControlEvent({
      type: "settings.changed",
      payload: { ...settingsSnapshotFixture, settings_revision: 2, selected_live2d_model_id: "Natori", show_speech_bubbles: false }
    });
    expect(latestRuntime?.settings?.selected_live2d_model_id).toBe("Natori");
    expect(latestRuntime?.settingsHydrated).toBe(true);

    // 网络失败会由真实 HTTP 客户端回退共享默认值；它同样不能推翻已收到的新设置事件。
    initialSettingsResponse.resolve(useDefaultFallback
      ? new Response("{}", { status: 503 })
      : Response.json(settingsSnapshotFixture));
    await flushHookEffects();
    expect(latestRuntime?.settings?.selected_live2d_model_id).toBe("Natori");
    expect(latestRuntime?.settings?.show_speech_bubbles).toBe(false);
    expect(latestRuntime?.settingsHydrated).toBe(true);
  });

  it("启动时通过 preload/main 获取 LiveKit session 并禁止浏览器原始麦克风发布，卸载时断开房间", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const bridge = createBridge();

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={bridge} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(bridge.createLiveKitSession).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/livekit/session",
      expect.anything()
    );
    expect(liveKitRoom.connect).toHaveBeenCalledWith(
      {
        url: "wss://livekit.example.test",
        token: "token-avatar-1",
        autoPublishMicrophone: false
      },
      expect.objectContaining({
        onStatusChange: expect.any(Function),
        onAssistantTrackSubscribed: expect.any(Function)
      })
    );

    act(() => {
      root.unmount();
    });

    expect(liveKitRoom.disconnect).toHaveBeenCalledOnce();
  });

  it("真实快捷键 IPC 先停止共享音轨，Backend 回执后只有新交付恢复声音", async () => {
    installFetchMock(Response.json(liveKitSessionFixture));
    let feedback!: (event: ShortcutFeedback) => void;
    const unsubscribe = vi.fn();
    const bridge = { ...createBridge(), onShortcutFeedback: (listener: typeof feedback) => {
      feedback = listener;
      return unsubscribe;
    } };
    act(() => root.render(<AvatarRuntimeProbe bridge={bridge} />));
    await flushHookEffects();
    const audio = document.createElement("audio");
    audio.play = vi.fn(async () => undefined);
    await liveKitRoomMockState.instances[0].config?.assistantAudioPlayback?.playTrack({ attach: () => audio });
    expect(audio.muted).toBe(false);
    act(() => feedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 }));
    expect(audio.muted).toBe(true);
    act(() => feedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: ["old"] }));
    emitControlEvent({ type: "assistant.playout_delivery_ready", payload: {
      request_id: "old", generation_id: "generation-old", delivery_id: "delivery-old"
    } });
    expect(audio.muted).toBe(true);
    emitControlEvent({ type: "assistant.playout_delivery_ready", payload: {
      request_id: "new", generation_id: "generation-new", delivery_id: "delivery-new"
    } });
    expect(audio.muted).toBe(false);
    act(() => feedback({ phase: "pressed", shortcutId: "selection_capture", sequence: 2 }));
    act(() => feedback({ phase: "pressed", shortcutId: "avatar_visibility", sequence: 3 }));
    expect(audio.muted).toBe(false);
    act(() => root.unmount());
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("助手播放元素变化会通过运行时选项传给头像层", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const onAssistantPlaybackElementChanged = vi.fn();
    const bridge = createBridge();
    const audioElement = document.createElement("audio");
    audioElement.play = vi.fn(async () => undefined);
    const track = {
      attach: vi.fn(() => audioElement),
      detach: vi.fn(() => [audioElement])
    };

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={bridge}
          onAssistantPlaybackElementChanged={onAssistantPlaybackElementChanged}
        />
      );
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    await liveKitRoom.config?.assistantAudioPlayback?.playTrack(track);
    liveKitRoom.config?.assistantAudioPlayback?.release();

    expect(onAssistantPlaybackElementChanged).toHaveBeenNthCalledWith(1, audioElement);
    expect(onAssistantPlaybackElementChanged).toHaveBeenNthCalledWith(2, null);
  });

  it("clean-mic 聆听状态只更新 Avatar 本地运行时和本地回调", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const onLocalListeningHintChanged = vi.fn();
    let latestRuntime: AvatarRuntime | undefined;

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onLocalListeningHintChanged={onLocalListeningHintChanged}
          onRuntime={runtime => {
            latestRuntime = runtime;
          }}
        />
      );
    });
    await flushHookEffects();

    const handlers = liveKitRoomMockState.instances[0].connect.mock.calls[0]?.[1];
    act(() => {
      handlers.onLocalListeningHintChanged?.(true);
    });

    expect(latestRuntime?.localListeningHintActive).toBe(true);
    expect(onLocalListeningHintChanged).toHaveBeenCalledWith(true);

    act(() => {
      handlers.onLocalListeningHintChanged?.(false);
    });
    expect(latestRuntime?.localListeningHintActive).toBe(false);
  });

  it("助手气泡存在且未进入隐藏 TTL 时会按本地 tick 推进字幕窗口", async () => {
    vi.useFakeTimers();
    let latestRuntime: AvatarRuntime | undefined;
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onRuntime={runtime => {
            latestRuntime = runtime;
          }}
        />
      );
    });
    await flushHookEffects();

    emitControlEvent({
      type: "assistant.text_final",
      payload: {
        request_id: "request-caption",
        session_id: "session-voice",
        generation_id: "generation-caption",
        text: "一二三四五六七八九十"
      }
    });

    expect(latestRuntime?.assistantBubble?.progressUnitCount).toBe(0);

    await act(async () => {
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });

    expect(latestRuntime?.assistantBubble?.progressUnitCount).toBe(3);
    vi.useRealTimers();
  });

  it("音频输入不可用时显示轻提示但仍连接 LiveKit UI 会话", async () => {
    const reportDiagnostic = vi.fn();
    let latestRuntime: AvatarRuntime | undefined;
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const unavailableSessionFixture: LiveKitSessionPayload = {
      ...liveKitSessionFixture,
      audio_input_status: "audio_unavailable"
    };
    const bridge = createBridge(reportDiagnostic, vi.fn(async () => unavailableSessionFixture));

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={bridge}
          onRuntime={runtime => {
            latestRuntime = runtime;
          }}
        />
      );
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(latestRuntime).toBeDefined();
    const capturedRuntime = latestRuntime as AvatarRuntime;
    expect(capturedRuntime.bottomBubble).toEqual({
      kind: "notice",
      text: "麦克风不可用，去 Console 选择设备。",
      ttlMs: 12000
    });
    expect(liveKitRoom.connect).toHaveBeenCalledWith(
      {
        url: "wss://livekit.example.test",
        token: "token-avatar-1",
        autoPublishMicrophone: false
      },
      expect.objectContaining({
        onStatusChange: expect.any(Function),
        onAssistantTrackSubscribed: expect.any(Function)
      })
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.audio_input_unavailable",
      {
        message: "麦克风不可用，已继续连接 LiveKit UI 会话。"
      }
    );
  });

  it("收到音频设备变更请求后会重建 LiveKit 会话并回报应用成功", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const createLiveKitSession = vi
      .fn<() => Promise<LiveKitSessionPayload>>()
      .mockResolvedValueOnce(liveKitSessionFixture)
      .mockResolvedValueOnce({
        ...liveKitSessionFixture,
        desktop_ui: {
          ...liveKitSessionFixture.desktop_ui,
          token: "token-avatar-2"
        }
      });

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(vi.fn(), createLiveKitSession)} />
      );
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    emitControlEvent({
      type: "audio.devices_change_requested",
      payload: {
        request_id: "audio-change-1",
        selection: {
          mode: "manual",
          microphone_id: "mic-usb",
          microphone_name: "麦克风 (USB Camera)",
          speaker_loopback_id: "speaker-loopback",
          speaker_loopback_name: "扬声器 (Realtek USB Audio)"
        },
        requested_at: 1
      }
    });
    await flushHookEffects();

    expect(createLiveKitSession).toHaveBeenCalledTimes(2);
    expect(liveKitRoom.disconnect).toHaveBeenCalledOnce();
    expect(liveKitRoom.connect).toHaveBeenCalledTimes(2);
    expect(liveKitRoom.connect).toHaveBeenLastCalledWith(
      {
        url: "wss://livekit.example.test",
        token: "token-avatar-2",
        autoPublishMicrophone: false
      },
      expect.objectContaining({
        onStatusChange: expect.any(Function),
        onAssistantTrackSubscribed: expect.any(Function)
      })
    );
    expect(readAudioDeviceApplyResultBodies(fetchMock)).toContainEqual({
      request_id: "audio-change-1",
      status: "applied"
    });
  });

  it("连续收到音频设备变更请求时只回报最新请求的应用结果", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const firstReloadSession = createDeferred<LiveKitSessionPayload>();
    const createLiveKitSession = vi
      .fn<() => Promise<LiveKitSessionPayload>>()
      .mockResolvedValueOnce(liveKitSessionFixture)
      .mockReturnValueOnce(firstReloadSession.promise)
      .mockResolvedValueOnce({
        ...liveKitSessionFixture,
        desktop_ui: {
          ...liveKitSessionFixture.desktop_ui,
          token: "token-avatar-3"
        }
      });

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(vi.fn(), createLiveKitSession)} />
      );
    });
    await flushHookEffects();

    emitControlEvent({
      type: "audio.devices_change_requested",
      payload: {
        request_id: "audio-change-old",
        selection: {
          mode: "manual",
          microphone_id: "mic-old",
          microphone_name: "旧麦克风",
          speaker_loopback_id: "speaker-old",
          speaker_loopback_name: "旧扬声器"
        },
        requested_at: 1
      }
    });
    emitControlEvent({
      type: "audio.devices_change_requested",
      payload: {
        request_id: "audio-change-new",
        selection: {
          mode: "system_default",
          microphone_id: null,
          microphone_name: null,
          speaker_loopback_id: null,
          speaker_loopback_name: null
        },
        requested_at: 2
      }
    });
    await flushHookEffects();

    firstReloadSession.resolve({
      ...liveKitSessionFixture,
      desktop_ui: {
        ...liveKitSessionFixture.desktop_ui,
        token: "token-avatar-old"
      }
    });
    await flushHookEffects();

    const bodies = readAudioDeviceApplyResultBodies(fetchMock);
    expect(bodies).toContainEqual({
      request_id: "audio-change-new",
      status: "applied"
    });
    expect(bodies).not.toContainEqual({
      request_id: "audio-change-old",
      status: "applied"
    });
  });

  it("音频设备热更新失败时会回报 failed 和失败原因", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const createLiveKitSession = vi
      .fn<() => Promise<LiveKitSessionPayload>>()
      .mockResolvedValueOnce(liveKitSessionFixture)
      .mockRejectedValueOnce(new Error("AEC sidecar failed to start."));

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(vi.fn(), createLiveKitSession)} />
      );
    });
    await flushHookEffects();

    emitControlEvent({
      type: "audio.devices_change_requested",
      payload: {
        request_id: "audio-change-failed",
        selection: {
          mode: "manual",
          microphone_id: "mic-failed",
          microphone_name: "失败测试麦克风",
          speaker_loopback_id: "speaker-failed",
          speaker_loopback_name: "失败测试扬声器"
        },
        requested_at: 1
      }
    });
    await flushHookEffects();

    expect(readAudioDeviceApplyResultBodies(fetchMock)).toContainEqual({
      request_id: "audio-change-failed",
      status: "failed",
      reason: "语音连接暂时不可用，正在自动重试。"
    });
  });

  it("effect 清理完成后不会再发起迟到的 preload/main session 请求", async () => {
    const reportDiagnostic = vi.fn();
    const deferredSession = createDeferred<LiveKitSessionPayload>();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    /** 标记 LiveKit effect 的 cleanup 是否已经运行，用于识别迟到 broker 调用。 */
    let cleanupHasRun = false;
    /** 记录 broker 是否发生在 cleanup 之后，避免只看调用次数而漏掉 StrictMode 时序风险。 */
    let brokerCalledAfterCleanup = false;
    const createLiveKitSession = vi.fn(() => {
      brokerCalledAfterCleanup = cleanupHasRun;
      return deferredSession.promise;
    });

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(reportDiagnostic, createLiveKitSession)} />
      );
    });

    const liveKitRoom = liveKitRoomMockState.instances[0];
    liveKitRoom.disconnect.mockImplementationOnce(async () => {
      cleanupHasRun = true;
    });

    act(() => {
      root.unmount();
    });

    deferredSession.resolve(liveKitSessionFixture);
    await flushHookEffects();

    expect(createLiveKitSession).toHaveBeenCalledOnce();
    expect(brokerCalledAfterCleanup).toBe(false);
    expect(liveKitRoom.connect).not.toHaveBeenCalled();
    expect(reportDiagnostic).not.toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      expect.anything()
    );
  });

  it("preload/main session 获取失败时上报错误诊断且不连接房间", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const createLiveKitSession = vi.fn(async () => {
      throw new Error("main broker down");
    });

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(reportDiagnostic, createLiveKitSession)} />
      );
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(liveKitRoom.connect).not.toHaveBeenCalled();
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      {
        code: "livekit_connection_failed", message: "语音连接暂时不可用，正在自动重试。", retryable: true
      }
    );
  });

  it("direct fetch fallback 的 session 接口返回非 2xx 时上报错误诊断且不连接房间", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(new Response(null, { status: 503 }));

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(reportDiagnostic, null)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(liveKitRoom.connect).not.toHaveBeenCalled();
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      {
        code: "livekit_connection_failed", message: "语音连接暂时不可用，正在自动重试。", retryable: true
      }
    );
  });

  it("preload/main session 能力缺失时保留 direct fetch fallback 且不发布浏览器原始麦克风", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(vi.fn(), null)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/livekit/session", {
      signal: expect.any(AbortSignal)
    });
    expect(liveKitRoom.connect).toHaveBeenCalledWith(
      {
        url: "wss://livekit.example.test",
        token: "token-avatar-1",
        autoPublishMicrophone: false
      },
      expect.objectContaining({
        onStatusChange: expect.any(Function),
        onAssistantTrackSubscribed: expect.any(Function)
      })
    );
  });

  it("HTTP 会话边界收到 desktop_aec 时只使用 desktop_ui token 连接房间", async () => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify({
        url: "wss://livekit.example.test",
        room_name: "avatar-room-1",
        desktop_ui: {
          identity: "desktop-ui-identity",
          token: "desktop-ui-token"
        },
        desktop_aec: {
          identity: "custom-aec-identity",
          token: "desktop-aec-token"
        }
      }), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(vi.fn(), null)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/livekit/session", {
      signal: expect.any(AbortSignal)
    });
    expect(liveKitRoom.connect).toHaveBeenCalledWith(
      {
        url: "wss://livekit.example.test",
        token: "desktop-ui-token",
        autoPublishMicrophone: false
      },
      expect.objectContaining({
        onStatusChange: expect.any(Function),
        onAssistantTrackSubscribed: expect.any(Function)
      })
    );
    expect(JSON.stringify(liveKitRoom.connect.mock.calls)).not.toContain("desktop-aec-token");
  });

  it("direct fetch fallback 的 pending session 请求会在卸载时 abort", async () => {
    const deferredSessionResponse = createDeferred<Response>();
    /** 捕获 session 请求 signal，验证 cleanup 是否真正取消旧 fallback HTTP 请求。 */
    let sessionAbortSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url === "http://127.0.0.1:8765/api/settings") {
        return Promise.resolve(
          new Response(JSON.stringify(settingsSnapshotFixture), {
            status: 200,
            headers: {
              "Content-Type": "application/json"
            }
          })
        );
      }

      if (url === "http://127.0.0.1:8765/api/livekit/session") {
        sessionAbortSignal = init?.signal instanceof AbortSignal ? init.signal : undefined;
        return deferredSessionResponse.promise;
      }

      throw new Error(`unexpected fetch url: ${url}`);
    });
    globalThis.fetch = fetchMock as typeof fetch;

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(vi.fn(), null)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/livekit/session", {
      signal: expect.any(AbortSignal)
    });
    expect(sessionAbortSignal?.aborted).toBe(false);

    act(() => {
      root.unmount();
    });

    expect(sessionAbortSignal?.aborted).toBe(true);

    deferredSessionResponse.resolve(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    await flushHookEffects();

    expect(liveKitRoom.connect).not.toHaveBeenCalled();
  });

  it("LiveKit 状态和助手音轨回调会上报 info 诊断", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(reportDiagnostic)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    const handlers = liveKitRoom.connect.mock.calls[0]?.[1];

    handlers.onStatusChange("connected", "ready");
    handlers.onAssistantTrackSubscribed("assistant-voice-1");

    expect(reportDiagnostic).toHaveBeenCalledWith(
      "info",
      "avatar.livekit_status_changed",
      {
        status: "connected"
      }
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "info",
      "avatar.livekit_assistant_track_subscribed",
      {
        identity: "assistant-voice-1"
      }
    );
  });

  it("LiveKit failed 状态会同时上报状态诊断和连接失败诊断", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(reportDiagnostic)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    const handlers = liveKitRoom.connect.mock.calls[0]?.[1];

    act(() => handlers.onStatusChange("failed", "room unavailable"));

    expect(reportDiagnostic).toHaveBeenCalledWith(
      "info",
      "avatar.livekit_status_changed",
      {
        status: "failed"
      }
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      {
        code: "livekit_connection_failed", message: "语音连接暂时不可用，正在自动重试。", retryable: true
      }
    );
  });

  it("LiveKit failed 状态缺少 reason 时会使用默认连接失败诊断文案", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(reportDiagnostic)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    const handlers = liveKitRoom.connect.mock.calls[0]?.[1];

    act(() => handlers.onStatusChange("failed"));

    expect(reportDiagnostic).toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      {
        code: "livekit_connection_failed", message: "语音连接暂时不可用，正在自动重试。", retryable: true
      }
    );
  });

  it("session 响应在卸载后才返回时不会连接房间或上报连接失败", async () => {
    const reportDiagnostic = vi.fn();
    const deferredSession = createDeferred<LiveKitSessionPayload>();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const createLiveKitSession = vi.fn(() => deferredSession.promise);

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(reportDiagnostic, createLiveKitSession)} />
      );
    });
    await act(async () => {
      await Promise.resolve();
    });

    const liveKitRoom = liveKitRoomMockState.instances[0];

    act(() => {
      root.unmount();
    });

    deferredSession.resolve(liveKitSessionFixture);
    await flushHookEffects();

    expect(liveKitRoom.connect).not.toHaveBeenCalled();
    expect(reportDiagnostic).not.toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      expect.anything()
    );
  });

  it("session 请求在卸载后才失败时不会上报连接失败", async () => {
    const reportDiagnostic = vi.fn();
    const deferredSession = createDeferred<LiveKitSessionPayload>();
    /**
     * 该 Promise 会在用例中被手动 reject。
     * 这里预先挂一个观察者，确保测试失败时优先暴露行为断言，而不是未处理 rejection 噪音。
     */
    deferredSession.promise.catch(() => undefined);
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const createLiveKitSession = vi.fn(() => deferredSession.promise);

    act(() => {
      root.render(
        <AvatarRuntimeProbe bridge={createBridge(reportDiagnostic, createLiveKitSession)} />
      );
    });
    await act(async () => {
      await Promise.resolve();
    });

    const liveKitRoom = liveKitRoomMockState.instances[0];

    act(() => {
      root.unmount();
    });

    deferredSession.reject(new Error("late session failure"));
    await flushHookEffects();

    expect(liveKitRoom.connect).not.toHaveBeenCalled();
    expect(reportDiagnostic).not.toHaveBeenCalledWith(
      "error",
      "avatar.livekit_connect_failed",
      expect.anything()
    );
  });

  it("卸载时 LiveKit 断开失败会被诊断捕获", async () => {
    const reportDiagnostic = vi.fn();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={createBridge(reportDiagnostic)} />);
    });
    await flushHookEffects();

    const liveKitRoom = liveKitRoomMockState.instances[0];
    liveKitRoom.disconnect.mockRejectedValueOnce(new Error("disconnect down"));

    act(() => {
      root.unmount();
    });
    await flushHookEffects();

    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.livekit_disconnect_failed",
      {
        message: "语音连接清理失败。"
      }
    );
  });

  it("标准化 Activity 快照会更新桌宠活动数量", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();

    emitControlEvent({
      type: "activities.snapshot_changed",
      payload: {
        revision: 1,
        runtime_id: "00000000-0000-4000-8000-0000000000aa",
        active: [{
          activity_id: "123e4567-e89b-42d3-a456-426614174000",
          kind: "cognitive",
          type_label: "认知任务",
          status: "running",
          goal_summary: "总结材料",
          failure_summary: null,
          stage_code: "processing",
          stage_label: "分析与处理",
          started_at: "2026-07-18T10:00:00+08:00",
          updated_at: "2026-07-18T10:00:00+08:00",
          finished_at: null,
          can_cancel: true
        }],
        recent: []
      }
    });

    expect(runtimeHolder.current?.activeActivityCount).toBe(1);
    expect(runtimeHolder.current?.activitySnapshot.revision).toBe(1);
  });

  it.each([1, 3])("第 %i 次播放期间气泡仍可关闭，成功后等待权威快照收起", async ringCount => {
    const fetchMock = installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };
    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();

    emitControlEvent(reminderSnapshotEvent(1, REMINDER_RUNTIME_A, [
      { ...ringingReminder(REMINDER_ID), ring_count: ringCount, next_ring_at: null }
    ]));
    expect(runtimeHolder.current?.ringingReminders.map(item => item.reminder_id)).toEqual([
      REMINDER_ID
    ]);

    await act(async () => {
      await runtimeHolder.current?.requestDismissReminders(REMINDER_RUNTIME_A, [REMINDER_ID]);
    });
    expect(runtimeHolder.current?.ringingReminders).toHaveLength(1);
    expect(runtimeHolder.current?.requestingReminderIds.has(REMINDER_ID)).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/timed-reminders/dismiss",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          runtime_id: REMINDER_RUNTIME_A,
          reminder_ids: [REMINDER_ID]
        })
      })
    );

    emitControlEvent(reminderSnapshotEvent(2, REMINDER_RUNTIME_A, [], [
      dismissedReminder(REMINDER_ID)
    ]));
    expect(runtimeHolder.current?.ringingReminders).toEqual([]);
    expect(runtimeHolder.current?.requestingReminderIds.size).toBe(0);
  });

  it("关闭失败保留响铃气泡并允许重试", async () => {
    const dismissResponse = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        runtime_id: REMINDER_RUNTIME_A,
        dismissed_ids: [REMINDER_ID]
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }),
      dismissResponse
    );
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };
    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();
    emitControlEvent(reminderSnapshotEvent(1, REMINDER_RUNTIME_A, [
      ringingReminder(REMINDER_ID)
    ]));

    await act(async () => {
      await runtimeHolder.current?.requestDismissReminders(REMINDER_RUNTIME_A, [REMINDER_ID]);
    });
    expect(runtimeHolder.current?.ringingReminders).toHaveLength(1);
    expect(runtimeHolder.current?.requestingReminderIds.size).toBe(0);
    expect(runtimeHolder.current?.reminderDismissError).toContain("暂不可用");

    await act(async () => {
      await runtimeHolder.current?.requestDismissReminders(REMINDER_RUNTIME_A, [REMINDER_ID]);
    });
    expect(dismissResponse).toHaveBeenCalledTimes(2);
    expect(runtimeHolder.current?.reminderDismissError).toBeNull();
    expect(runtimeHolder.current?.requestingReminderIds.has(REMINDER_ID)).toBe(true);
  });

  it("Reminder runtime 切换会清空旧气泡和请求账本，并忽略旧请求的迟到失败", async () => {
    const lateDismiss = createDeferred<Response>();
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }),
      () => lateDismiss.promise
    );
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };
    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={createBridge()}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();
    emitControlEvent(reminderSnapshotEvent(1, REMINDER_RUNTIME_A, [
      ringingReminder(REMINDER_ID)
    ]));

    act(() => {
      void runtimeHolder.current?.requestDismissReminders(REMINDER_RUNTIME_A, [REMINDER_ID]);
    });
    expect(runtimeHolder.current?.requestingReminderIds.has(REMINDER_ID)).toBe(true);
    emitControlEvent(reminderSnapshotEvent(0, REMINDER_RUNTIME_B, []));
    expect(runtimeHolder.current?.timedReminderSnapshot.runtime_id).toBe(REMINDER_RUNTIME_B);
    expect(runtimeHolder.current?.ringingReminders).toEqual([]);
    expect(runtimeHolder.current?.requestingReminderIds.size).toBe(0);

    lateDismiss.resolve(new Response(null, { status: 503 }));
    await flushHookEffects();
    expect(runtimeHolder.current?.reminderDismissError).toBeNull();
    expect(runtimeHolder.current?.ringingReminders).toEqual([]);
  });

  it("按聊天模式保存待答队列，并在 resolved 后移除对应气泡", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    let modeListener: ((mode: "collapsed" | "expanded") => void) | null = null;
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };
    const bridge = {
      ...createBridge(),
      getChatMode: vi.fn(async () => "collapsed" as const),
      onChatModeChanged: vi.fn((listener: (mode: "collapsed" | "expanded") => void) => {
        modeListener = listener;
        return () => {
          modeListener = null;
        };
      })
    };

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={bridge}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();

    emitControlEvent({
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001",
        interaction_session_id: "session-a",
        kind: "question",
        tool_name: "AskUserQuestion",
        input_data: {},
        questions: [{
          question: "请补充目标格式",
          header: "格式",
          options: [],
          multiSelect: false
        }]
      }
    });

    expect(runtimeHolder.current?.chatMode).toBe("collapsed");
    expect(runtimeHolder.current?.pendingInteraction).toEqual({
      requestId: "123e4567-e89b-42d3-a456-426614174000",
      kind: "question"
    });

    emitControlEvent({
      type: "background_agent.interaction_submitted",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001"
      }
    });
    expect(runtimeHolder.current?.pendingInteraction).toBeNull();

    emitControlEvent({
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001",
        interaction_session_id: "session-a",
        kind: "question",
        tool_name: "AskUserQuestion",
        input_data: {},
        questions: [{
          question: "请补充目标格式",
          header: "格式",
          options: [],
          multiSelect: false
        }]
      }
    });

    emitControlEvent({
      type: "background_agent.interactions_snapshot",
      payload: { items: [] }
    });
    expect(runtimeHolder.current?.pendingInteraction).toBeNull();

    emitControlEvent({
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001",
        interaction_session_id: "session-a",
        kind: "question",
        tool_name: "AskUserQuestion",
        input_data: {},
        questions: [{
          question: "请补充目标格式",
          header: "格式",
          options: [],
          multiSelect: false
        }]
      }
    });

    act(() => {
      modeListener?.("expanded");
    });
    expect(runtimeHolder.current?.chatMode).toBe("expanded");

    emitControlEvent({
      type: "background_agent.interaction_resolved",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001",
        outcome: "answered"
      }
    });
    expect(runtimeHolder.current?.pendingInteraction).toBeNull();
  });

  it("新的权限请求自动打开一次对应交互卡，重复事件不会再次打扰用户", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    const openBackgroundAgentInteraction = vi.fn();
    const bridge = {
      ...createBridge(),
      openBackgroundAgentInteraction
    };

    act(() => {
      root.render(<AvatarRuntimeProbe bridge={bridge} />);
    });
    await flushHookEffects();

    const permissionEvent = {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174010",
        activity_id: "123e4567-e89b-42d3-a456-426614174011",
        interaction_session_id: "session-permission",
        kind: "permission",
        tool_name: "Bash",
        input_data: { command: "ffmpeg -i input.wav output.mp3" },
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    };
    emitControlEvent(permissionEvent);
    emitControlEvent(permissionEvent);

    expect(openBackgroundAgentInteraction).toHaveBeenCalledTimes(1);
    expect(openBackgroundAgentInteraction).toHaveBeenCalledWith(
      "123e4567-e89b-42d3-a456-426614174010"
    );
  });

  it("Chat 展开且语音气泡关闭时，真实控制快照仍更新独立状态图案", async () => {
    installFetchMock(Response.json(liveKitSessionFixture));
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };
    const bridge = { ...createBridge(), getChatMode: async () => "expanded" as const };
    act(() => root.render(
      <AvatarRuntimeProbe bridge={bridge} onRuntime={runtime => { runtimeHolder.current = runtime; }} />
    ));
    await flushHookEffects();
    act(() => controlWebSocketMockState.instances[0].onopen?.());
    emitControlEvent({
      type: "settings.changed",
      payload: { ...settingsSnapshotFixture, show_speech_bubbles: false }
    });
    const initialSnapshot = {
      input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
      input_runtime_epoch: 1,
      revision: 1,
      followup_state: "followup_hot",
      turn_state: "idle",
      session_id: "session-current"
    };
    emitControlEvent({ type: "interaction.state_changed", payload: initialSnapshot });
    expect(runtimeHolder.current?.chatMode).toBe("expanded");
    expect(runtimeHolder.current?.companionStatus.state).toBe("available");
    expect(runtimeHolder.current?.companionStatus.wakeMode).toBe("followup");

    emitControlEvent({
      type: "interaction.state_changed",
      payload: { ...initialSnapshot, revision: 2, turn_state: "processing" }
    });
    expect(runtimeHolder.current?.companionStatus.state).toBe("thinking");
    expect(runtimeHolder.current?.bottomBubble).toBeNull();
    expect(runtimeHolder.current?.assistantBubble).toBeNull();
    expect(runtimeHolder.current?.settings?.show_speech_bubbles).toBe(false);

    // 同一个 Backend 连接中的旧快照不能让图案退回空闲，状态变化也不能重建控制连接。
    emitControlEvent({ type: "interaction.state_changed", payload: initialSnapshot });
    expect(runtimeHolder.current?.companionStatus.state).toBe("thinking");
    expect(controlWebSocketMockState.instances).toHaveLength(1);

    // 设置 HTTP 保存失败不会断开真实 WebSocket，也不能把角色状态永久切成错误图案。
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("settings endpoint down"));
    await act(async () => {
      await expect(runtimeHolder.current!.updateSettings({ selected_live2d_model_id: "Rice" }))
        .rejects.toThrow("settings endpoint down");
    });
    expect(runtimeHolder.current?.companionStatus.state).toBe("thinking");
    emitControlEvent({ type: "interaction.state_changed",
      payload: { ...initialSnapshot, revision: 3, turn_state: "listening" } });
    expect(runtimeHolder.current?.companionStatus.state).toBe("listening");
  });

  it("聊天模式 push 早于初始 pull 返回时不会被旧状态覆盖", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    const initialMode = createDeferred<"collapsed" | "expanded" | null>();
    let modeListener: ((mode: "collapsed" | "expanded") => void) | null = null;
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={{
            ...createBridge(),
            getChatMode: () => initialMode.promise,
            onChatModeChanged(listener) {
              modeListener = listener;
              return () => undefined;
            }
          }}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });

    act(() => {
      modeListener?.("expanded");
    });
    initialMode.resolve("collapsed");
    await flushHookEffects();

    expect(runtimeHolder.current?.chatMode).toBe("expanded");
  });

  it("关闭 Chat 的模式事件先于紧随其后的唤醒事件生效", async () => {
    installFetchMock(
      new Response(JSON.stringify(liveKitSessionFixture), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    let modeListener: ((mode: "collapsed" | "expanded") => void) | null = null;
    const runtimeHolder: { current: AvatarRuntime | null } = { current: null };

    act(() => {
      root.render(
        <AvatarRuntimeProbe
          bridge={{
            ...createBridge(),
            getChatMode: async () => "expanded" as const,
            onChatModeChanged(listener) {
              modeListener = listener;
              return () => {
                modeListener = null;
              };
            }
          }}
          onRuntime={nextRuntime => {
            runtimeHolder.current = nextRuntime;
          }}
        />
      );
    });
    await flushHookEffects();
    expect(runtimeHolder.current?.chatMode).toBe("expanded");

    // 真实窗口关闭时，主进程会连续推送 collapsed 和后端的 wake.detected。
    // 两条消息在同一轮 React flush 中到达，正好覆盖 setState 尚未触发 effect 的窗口。
    act(() => {
      modeListener?.("collapsed");
      const socket = controlWebSocketMockState.instances[0];
      socket.onmessage?.({
        data: JSON.stringify({
          type: "wake.detected",
          payload: {
            input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
            input_runtime_epoch: 1,
            session_id: "session-voice",
            text: "我在，正在听。"
          }
        })
      });
    });

    expect(runtimeHolder.current?.chatMode).toBe("collapsed");
    expect(runtimeHolder.current?.bottomBubble).toEqual({
      kind: "speech",
      text: "我在，正在听。",
      ttlMs: 2400
    });

    // 重复的 collapsed 推送只是幂等确认，不能把刚显示的新唤醒气泡清掉。
    act(() => {
      modeListener?.("collapsed");
    });
    expect(runtimeHolder.current?.bottomBubble).toEqual({
      kind: "speech",
      text: "我在，正在听。",
      ttlMs: 2400
    });
  });
});

const REMINDER_ID = "123e4567-e89b-42d3-a456-426614174020";
const REMINDER_RUNTIME_A = "00000000-0000-4000-8000-0000000000cc";
const REMINDER_RUNTIME_B = "00000000-0000-4000-8000-0000000000dd";

function ringingReminder(reminderId: string) {
  return {
    reminder_id: reminderId,
    content: "该去面试了",
    state: "ringing" as const,
    trigger_at: "2026-09-04T15:00:00+08:00",
    created_at: "2026-09-04T10:00:00+08:00",
    updated_at: "2026-09-04T15:00:00+08:00",
    finished_at: null,
    ring_count: 1,
    next_ring_at: "2026-09-04T15:01:00+08:00" as string | null,
    finish_reason: null,
    can_modify: false,
    can_cancel: false,
    can_dismiss: true
  };
}

function dismissedReminder(reminderId: string) {
  return {
    ...ringingReminder(reminderId),
    state: "dismissed" as const,
    finished_at: "2026-09-04T15:00:10+08:00",
    next_ring_at: null,
    finish_reason: "user_dismissed" as const,
    can_dismiss: false
  };
}

function reminderSnapshotEvent(
  revision: number,
  runtimeId: string,
  active: ReturnType<typeof ringingReminder>[],
  recent: ReturnType<typeof dismissedReminder>[] = []
) {
  return {
    type: "reminders.snapshot_changed",
    payload: {
      runtime_id: runtimeId,
      revision,
      active,
      recent
    }
  };
}
