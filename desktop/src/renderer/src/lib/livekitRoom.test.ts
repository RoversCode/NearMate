import { RoomEvent } from "livekit-client";
import { describe, expect, it, vi } from "vitest";
import type { ConnectionStatus } from "../types/events";
import type { LocalListeningMonitor } from "./livekitLocalListeningMonitor";
import {
  DEFAULT_LIVEKIT_ROOM_OPTIONS,
  LiveKitRoomClient,
  type AssistantAudioPlaybackSink,
  type LiveKitRoomHandlers,
  type LiveKitRoomLike
} from "./livekitRoom";

/**
 * 测试用参与者只需要暴露 identity。
 * 核心输出是远端订阅事件可以把助手身份透传给 renderer 业务回调。
 */
type FakeParticipant = {
  identity: string;
};

/**
 * 测试用远端音轨只暴露媒体客户端识别与播放边界真正需要的字段。
 *
 * 核心字段：kind 表示媒体类型，name 对应 backend 发布的 assistant-audio 轨道名。
 * 预期用途：让测试表达“助手音频轨应该被播放”，而不依赖真实 LiveKit SDK。
 * 边界异常：attach/detach 由播放 sink 测试，不在 room fake 中实现真实 DOM。
 */
type FakeRemoteTrack = {
  kind?: string;
  name?: string;
};

/**
 * 测试用远端发布信息只保留 trackName。
 *
 * 核心字段：trackName 是 LiveKit publication 暴露的稳定轨道名。
 * 预期用途：覆盖真实 SDK 可能把轨道名放在 publication 而不是 track 上的情况。
 * 边界异常：字段可省略，便于测试非助手轨道被忽略。
 */
type FakeRemotePublication = {
  trackName?: string;
};

/**
 * 测试用事件处理器注册表。
 * 核心入参是 LiveKit 事件名，输出是 fake room 在测试中可主动触发的回调。
 */
type FakeRoomListeners = {
  [RoomEvent.TrackSubscribed]?: (
    track: FakeRemoteTrack,
    publication: FakeRemotePublication,
    participant: FakeParticipant
  ) => void;
  [RoomEvent.TrackUnsubscribed]?: (
    track: FakeRemoteTrack,
    publication: FakeRemotePublication,
    participant: FakeParticipant
  ) => void;
  [RoomEvent.Disconnected]?: () => void;
};

/**
 * 构造测试可控的 Promise。
 * 核心输出是让用例手动决定异步清理何时完成，从而验证客户端不会提前进入下一阶段。
 */
function createDeferred() {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
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
 * FakeLiveKitRoom 是 LiveKit Room 的最小测试替身。
 * 它只实现媒体客户端真正依赖的窄接口，避免单元测试连接真实 LiveKit 服务。
 */
class FakeLiveKitRoom implements LiveKitRoomLike {
  emitDisconnectedOnDisconnect: "none" | "sync" | "async" = "none";

  /** 记录 connect 入参，用于确认客户端没有篡改房间地址与 token。 */
  readonly connect = vi.fn<LiveKitRoomLike["connect"]>(async () => undefined);

  /** 记录 disconnect 调用，用于确认客户端断连时会释放当前 room。 */
  readonly disconnect = vi.fn<LiveKitRoomLike["disconnect"]>(async () => {
    if (this.emitDisconnectedOnDisconnect === "sync") {
      this.emitDisconnected();
      return;
    }

    if (this.emitDisconnectedOnDisconnect === "async") {
      await Promise.resolve();
      this.emitDisconnected();
    }
  });

  /** 本地参与者只模拟麦克风开关能力，覆盖自动发布麦克风的行为。 */
  readonly localParticipant = {
    setMicrophoneEnabled: vi.fn(async (_enabled: boolean) => undefined)
  };

  /** 保存事件监听器，方便测试主动触发远端订阅或断线事件。 */
  private readonly listeners: FakeRoomListeners = {};

  on(
    event: typeof RoomEvent.TrackSubscribed,
    listener: (track: unknown, publication: unknown, participant: { identity: string }) => void
  ): this;
  on(
    event: typeof RoomEvent.TrackUnsubscribed,
    listener: (track: unknown, publication: unknown, participant: { identity: string }) => void
  ): this;
  on(event: typeof RoomEvent.Disconnected, listener: () => void): this;
  /**
   * 注册 LiveKit 事件监听器。
   * 核心入参是事件名与对应回调，输出为 room 自身以匹配链式注册习惯。
   */
  on(
    event:
      | typeof RoomEvent.TrackSubscribed
      | typeof RoomEvent.TrackUnsubscribed
      | typeof RoomEvent.Disconnected,
    listener:
      | ((track: unknown, publication: unknown, participant: { identity: string }) => void)
      | (() => void)
  ): this {
    if (event === RoomEvent.TrackSubscribed) {
      this.listeners[RoomEvent.TrackSubscribed] = listener as FakeRoomListeners[RoomEvent.TrackSubscribed];
    }

    if (event === RoomEvent.TrackUnsubscribed) {
      this.listeners[RoomEvent.TrackUnsubscribed] = listener as FakeRoomListeners[RoomEvent.TrackUnsubscribed];
    }

    if (event === RoomEvent.Disconnected) {
      this.listeners[RoomEvent.Disconnected] = listener as FakeRoomListeners[RoomEvent.Disconnected];
    }

    return this;
  }

  /**
   * 主动触发远端轨道订阅事件。
   *
   * 核心入参：identity 是远端参与者身份，track/publication 可指定媒体类型和轨道名。
   * 预期输出：触发 LiveKitRoomClient 注册的订阅回调。
   * 边界异常：默认参数保留旧测试语义，仍可只验证 identity 诊断。
   */
  emitTrackSubscribed(
    identity: string,
    track: FakeRemoteTrack = {},
    publication: FakeRemotePublication = {}
  ): void {
    this.listeners[RoomEvent.TrackSubscribed]?.(track, publication, { identity });
  }

  /**
   * 主动触发远端轨道退订事件。
   *
   * Args:
   *   identity: 发布该轨道的远端参与者身份。
   *   track: 被退订的远端轨道对象。
   *   publication: 对应的轨道发布信息。
   */
  emitTrackUnsubscribed(
    identity: string,
    track: FakeRemoteTrack,
    publication: FakeRemotePublication = {}
  ): void {
    this.listeners[RoomEvent.TrackUnsubscribed]?.(track, publication, { identity });
  }

  /**
   * 主动触发 SDK 断线事件。
   * 业务意图是模拟 LiveKit 底层连接自行关闭，验证客户端会把当前 room 标记为不可继续使用。
   */
  emitDisconnected(): void {
    this.listeners[RoomEvent.Disconnected]?.();
  }
}

/**
 * 创建状态收集型 handlers。
 * 核心输出是状态序列和原因序列，便于断言连接生命周期是否严格按契约回调。
 */
function createHandlers() {
  const statuses: ConnectionStatus[] = [];
  const reasons: Array<string | undefined> = [];
  const assistantTrackIdentities: string[] = [];
  const localListeningStates: boolean[] = [];

  const handlers: LiveKitRoomHandlers = {
    onStatusChange(status, reason) {
      statuses.push(status);
      reasons.push(reason);
    },
    onAssistantTrackSubscribed(identity) {
      assistantTrackIdentities.push(identity);
    },
    onLocalListeningHintChanged(active) {
      localListeningStates.push(active);
    }
  };

  return {
    assistantTrackIdentities,
    handlers,
    localListeningStates,
    reasons,
    statuses
  };
}

/**
 * 创建测试用助手音频播放 sink。
 *
 * 核心输出：playTrack/release 均为可断言 mock，避免单元测试创建真实 audio 元素。
 * 边界异常：playTrack 默认成功；需要模拟自动播放失败时由测试覆盖 mock 实现。
 */
function createAssistantAudioPlaybackSink(): AssistantAudioPlaybackSink {
  return {
    playTrack: vi.fn(async (_track: unknown) => undefined),
    release: vi.fn()
  };
}

/**
 * 创建测试用本地聆听监测器。
 *
 * Returns:
 *   可断言 start 和 stop 调用的最小监测器。
 */
function createLocalListeningMonitor(): LocalListeningMonitor {
  return {
    start: vi.fn(),
    stop: vi.fn()
  };
}

describe("LiveKitRoomClient", () => {
  it("缺少 url 或 token 时进入 failed，并且不会创建可用 fallback", async () => {
    const createdRooms: FakeLiveKitRoom[] = [];
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => {
        const room = new FakeLiveKitRoom();
        createdRooms.push(room);
        return room;
      }
    });

    await client.connect({ url: "wss://livekit.example" }, handlers);

    expect(statuses).toEqual(["connecting", "failed"]);
    expect(reasons).toEqual([undefined, "未提供 LiveKit 房间地址或 token。"]);
    expect(statuses).not.toContain("fallback");
    expect(createdRooms).toHaveLength(0);
  });

  it("默认真实 Room 开启 WebRTC 回声消除、降噪和自动增益", async () => {
    /**
     * 默认真实 Room 配置单独导出供测试锁定。
     * 业务意图是避免连接生命周期测试碰真实 SDK，同时仍能防止默认采集约束被删。
     */
    expect(DEFAULT_LIVEKIT_ROOM_OPTIONS).toEqual({
      adaptiveStream: true,
      audioCaptureDefaults: {
        autoGainControl: true,
        echoCancellation: true,
        noiseSuppression: true
      },
      dynacast: true
    });
  });

  it("连接成功时调用 room.connect 并回调 connected", async () => {
    const room = new FakeLiveKitRoom();
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ url: "wss://livekit.example", token: "token-a" }, handlers);

    expect(room.connect).toHaveBeenCalledWith("wss://livekit.example", "token-a");
    expect(statuses).toEqual(["connecting", "connected"]);
  });

  it("启用 autoPublishMicrophone 时会在连接成功后发布本地麦克风", async () => {
    const room = new FakeLiveKitRoom();
    const { handlers } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect(
      { autoPublishMicrophone: true, token: "token-a", url: "wss://livekit.example" },
      handlers
    );

    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true);
  });

  it("自动发布麦克风失败时进入 failed，并断开已经连接成功的 room", async () => {
    const room = new FakeLiveKitRoom();
    room.localParticipant.setMicrophoneEnabled.mockRejectedValueOnce(new Error("microphone denied"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect(
      { autoPublishMicrophone: true, token: "token-a", url: "wss://livekit.example" },
      handlers
    );

    expect(statuses).toEqual(["connecting", "connected", "failed"]);
    expect(reasons).toEqual([undefined, undefined, "microphone denied"]);
    expect(room.disconnect).toHaveBeenCalledTimes(1);
  });

  it("只有助手音频轨订阅时才上报助手参与者 identity", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { assistantTrackIdentities, handlers } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("unknown-1", { kind: "audio", name: "screen-share" });
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });

    expect(assistantTrackIdentities).toEqual(["assistant-1"]);
  });

  it("clean-mic 订阅启动本地监测，退订时只停止同一条轨道", async () => {
    const room = new FakeLiveKitRoom();
    const localListeningMonitor = createLocalListeningMonitor();
    const { assistantTrackIdentities, handlers } = createHandlers();
    const cleanMicrophoneTrack = {
      kind: "audio",
      name: "clean-mic"
    };
    const client = new LiveKitRoomClient({
      createRoom: () => room,
      localListeningMonitor
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("desktop-aec", cleanMicrophoneTrack);
    room.emitTrackUnsubscribed("desktop-aec", cleanMicrophoneTrack);

    expect(localListeningMonitor.start).toHaveBeenCalledWith(
      cleanMicrophoneTrack,
      expect.any(Function)
    );
    expect(localListeningMonitor.stop).toHaveBeenCalledWith(cleanMicrophoneTrack);
    expect(assistantTrackIdentities).toEqual([]);
  });

  it("断开房间时停止本地 clean-mic 监测", async () => {
    const room = new FakeLiveKitRoom();
    const localListeningMonitor = createLocalListeningMonitor();
    const client = new LiveKitRoomClient({
      createRoom: () => room,
      localListeningMonitor
    });

    await client.connect(
      { token: "token-a", url: "wss://livekit.example" },
      createHandlers().handlers
    );
    room.emitTrackSubscribed("desktop-aec", { kind: "audio", name: "clean-mic" });
    await client.disconnect();

    expect(localListeningMonitor.stop).toHaveBeenLastCalledWith();
  });

  it("收到助手音频轨时交给播放 sink 并保留订阅诊断", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { assistantTrackIdentities, handlers } = createHandlers();
    const assistantTrack = {
      kind: "audio",
      name: "assistant-audio"
    };
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("assistant-1", assistantTrack);

    expect(audioPlayback.playTrack).toHaveBeenCalledWith(assistantTrack);
    expect(assistantTrackIdentities).toEqual(["assistant-1"]);
  });

  it("连接过程中早到的助手音频轨也会进入播放 sink，避免后端先发布时丢声", async () => {
    const room = new FakeLiveKitRoom();
    const connectDeferred = createDeferred();
    room.connect.mockReturnValueOnce(connectDeferred.promise);
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { assistantTrackIdentities, handlers } = createHandlers();
    const assistantTrack = {
      kind: "audio",
      name: "assistant-audio"
    };
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    const connectPromise = client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await vi.waitFor(() => {
      expect(room.connect).toHaveBeenCalledWith("wss://livekit.example", "token-a");
    });

    // LiveKit 在 connect 完成前可能已经把房间内既有的 backend 助手音轨下发给 desktop。
    // 业务意图是保留这条真实媒体事件，避免“后端先启动并已发布轨道”时首条语音永远无法 attach。
    room.emitTrackSubscribed("assistant-1", assistantTrack);

    expect(audioPlayback.playTrack).toHaveBeenCalledWith(assistantTrack);
    expect(assistantTrackIdentities).toEqual(["assistant-1"]);

    connectDeferred.resolve();
    await connectPromise;
  });

  it("新助手音频轨到达前释放旧播放资源，避免多路重叠播放", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, createHandlers().handlers);
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });

    expect(audioPlayback.release).toHaveBeenCalledTimes(1);
    expect(audioPlayback.playTrack).toHaveBeenCalledTimes(2);
  });

  it("非助手音频轨不会触发播放 sink", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, createHandlers().handlers);
    room.emitTrackSubscribed("assistant-1", { kind: "video", name: "assistant-audio" });
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "screen-share" });

    expect(audioPlayback.playTrack).not.toHaveBeenCalled();
  });

  it("助手音频播放失败时不打断房间连接，并通过状态原因暴露诊断", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    vi.mocked(audioPlayback.playTrack).mockRejectedValueOnce(new Error("autoplay denied"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });
    await Promise.resolve();

    expect(statuses).toEqual(["connecting", "connected", "failed"]);
    expect(reasons).toEqual([undefined, undefined, "autoplay denied"]);
    expect(room.disconnect).not.toHaveBeenCalled();
  });

  it("旧 room 的 late track subscribed 不会继续上报助手 identity", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    const rooms = [firstRoom, secondRoom];
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { assistantTrackIdentities, handlers } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example/a" }, handlers);
    await client.connect({ token: "token-b", url: "wss://livekit.example/b" }, handlers);
    firstRoom.emitTrackSubscribed("old-assistant", {
      kind: "audio",
      name: "assistant-audio"
    });
    secondRoom.emitTrackSubscribed("new-assistant", {
      kind: "audio",
      name: "assistant-audio"
    });

    expect(assistantTrackIdentities).toEqual(["new-assistant"]);
  });

  it("disconnect 会断开 room 并回调 idle", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });
    await client.disconnect();

    expect(audioPlayback.release).toHaveBeenCalledTimes(1);
    expect(room.disconnect).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["connecting", "connected", "idle"]);
  });

  it("SDK 断线事件触发后清理当前 room，后续发布麦克风不会继续使用旧 room", async () => {
    const room = new FakeLiveKitRoom();
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });
    room.emitDisconnected();
    await client.publishMicrophone();

    expect(audioPlayback.release).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["connecting", "connected", "disconnected"]);
    expect(room.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
  });

  it("第二次 connect 前会断开旧 room，并连接新 room 成功", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    const rooms = [firstRoom, secondRoom];
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example/a" }, handlers);
    await client.connect({ token: "token-b", url: "wss://livekit.example/b" }, handlers);

    expect(firstRoom.disconnect).toHaveBeenCalledTimes(1);
    expect(secondRoom.connect).toHaveBeenCalledWith("wss://livekit.example/b", "token-b");
    expect(statuses).toEqual(["connecting", "connected", "connecting", "connected"]);
  });

  it("第二次 connect 会等待旧 room 断开完成后才连接新 room", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    const rooms = [firstRoom, secondRoom];
    const disconnectDeferred = createDeferred();
    firstRoom.disconnect.mockReturnValueOnce(disconnectDeferred.promise);
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example/a" }, handlers);
    const reconnectPromise = client.connect(
      { token: "token-b", url: "wss://livekit.example/b" },
      handlers
    );
    await Promise.resolve();

    expect(firstRoom.disconnect).toHaveBeenCalledTimes(1);
    expect(secondRoom.connect).not.toHaveBeenCalled();
    expect(statuses).toEqual(["connecting", "connected", "connecting"]);

    disconnectDeferred.resolve();
    await reconnectPromise;

    expect(secondRoom.connect).toHaveBeenCalledWith("wss://livekit.example/b", "token-b");
    expect(statuses).toEqual(["connecting", "connected", "connecting", "connected"]);
  });

  it("第二次 connect 会先断开上一次 in-flight room，旧 connect late success 不会推进 connected", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    const rooms = [firstRoom, secondRoom];
    const firstConnectDeferred = createDeferred();
    firstRoom.connect.mockReturnValueOnce(firstConnectDeferred.promise);
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    const firstConnectPromise = client.connect(
      { token: "token-a", url: "wss://livekit.example/a" },
      handlers
    );
    await vi.waitFor(() => {
      expect(firstRoom.connect).toHaveBeenCalledWith("wss://livekit.example/a", "token-a");
    });

    const secondConnectPromise = client.connect(
      { token: "token-b", url: "wss://livekit.example/b" },
      handlers
    );
    await vi.waitFor(() => {
      expect(firstRoom.disconnect).toHaveBeenCalledTimes(1);
    });

    await vi.waitFor(() => {
      expect(secondRoom.connect).toHaveBeenCalledWith("wss://livekit.example/b", "token-b");
    });
    expect(statuses).toEqual(["connecting", "connecting", "connected"]);

    firstConnectDeferred.resolve();
    await firstConnectPromise;
    await secondConnectPromise;

    await client.publishMicrophone();

    expect(statuses).toEqual(["connecting", "connecting", "connected"]);
    expect(firstRoom.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    expect(secondRoom.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true);
  });

  it("旧 room 断开失败时第二次 connect 不连接新 room，并保留旧 room 供 public disconnect 重试", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    const rooms = [firstRoom, secondRoom];
    firstRoom.disconnect.mockRejectedValueOnce(new Error("cleanup failed"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example/a" }, handlers);
    await client.connect({ token: "token-b", url: "wss://livekit.example/b" }, handlers);
    await client.disconnect();

    expect(firstRoom.disconnect).toHaveBeenCalledTimes(2);
    expect(secondRoom.connect).not.toHaveBeenCalled();
    expect(statuses).toEqual(["connecting", "connected", "connecting", "failed", "idle"]);
    expect(reasons).toEqual([undefined, undefined, undefined, "cleanup failed", undefined]);
  });

  it("第二次 connect 失败时旧 room 已经被断开，不会泄漏旧连接", async () => {
    const firstRoom = new FakeLiveKitRoom();
    const secondRoom = new FakeLiveKitRoom();
    secondRoom.connect.mockRejectedValueOnce(new Error("second room unavailable"));
    const rooms = [firstRoom, secondRoom];
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => rooms.shift() ?? new FakeLiveKitRoom()
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example/a" }, handlers);
    await client.connect({ token: "token-b", url: "wss://livekit.example/b" }, handlers);

    expect(firstRoom.disconnect).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["connecting", "connected", "connecting", "failed"]);
    expect(reasons).toEqual([undefined, undefined, undefined, "second room unavailable"]);
  });

  it("自动发布麦克风失败时会等待 room 断开完成后才回调 failed", async () => {
    const room = new FakeLiveKitRoom();
    const disconnectDeferred = createDeferred();
    room.disconnect.mockReturnValueOnce(disconnectDeferred.promise);
    room.localParticipant.setMicrophoneEnabled.mockRejectedValueOnce(new Error("microphone denied"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    const connectPromise = client.connect(
      { autoPublishMicrophone: true, token: "token-a", url: "wss://livekit.example" },
      handlers
    );
    await Promise.resolve();
    await Promise.resolve();

    await vi.waitFor(() => {
      expect(room.disconnect).toHaveBeenCalledTimes(1);
    });
    expect(statuses).toEqual(["connecting", "connected"]);

    disconnectDeferred.resolve();
    await connectPromise;

    expect(statuses).toEqual(["connecting", "connected", "failed"]);
    expect(reasons).toEqual([undefined, undefined, "microphone denied"]);
  });

  it("自动发布麦克风失败且清理断开失败时保留 room，并继续使用原始发麦失败原因", async () => {
    const room = new FakeLiveKitRoom();
    room.disconnect.mockRejectedValueOnce(new Error("cleanup failed"));
    room.localParticipant.setMicrophoneEnabled.mockRejectedValueOnce(new Error("microphone denied"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect(
      { autoPublishMicrophone: true, token: "token-a", url: "wss://livekit.example" },
      handlers
    );
    await client.disconnect();

    expect(room.disconnect).toHaveBeenCalledTimes(2);
    expect(statuses).toEqual(["connecting", "connected", "failed", "idle"]);
    expect(reasons).toEqual([undefined, undefined, "microphone denied", undefined]);
  });

  it("public disconnect 会等待 room 断开完成后才回调 idle 并返回", async () => {
    const room = new FakeLiveKitRoom();
    const disconnectDeferred = createDeferred();
    room.disconnect.mockReturnValueOnce(disconnectDeferred.promise);
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    const disconnectPromise = client.disconnect();
    let hasReturned = false;
    disconnectPromise.then(() => {
      hasReturned = true;
    });
    await Promise.resolve();

    expect(statuses).toEqual(["connecting", "connected"]);
    expect(hasReturned).toBe(false);

    disconnectDeferred.resolve();
    await disconnectPromise;

    expect(statuses).toEqual(["connecting", "connected", "idle"]);
    expect(hasReturned).toBe(true);
  });

  it("public disconnect 会断开 in-flight room，pending connect 后续不会推进 connected", async () => {
    const room = new FakeLiveKitRoom();
    const connectDeferred = createDeferred();
    room.connect.mockReturnValueOnce(connectDeferred.promise);
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    const connectPromise = client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await vi.waitFor(() => {
      expect(room.connect).toHaveBeenCalledTimes(1);
    });

    await client.disconnect();
    connectDeferred.resolve();
    await connectPromise;

    expect(room.disconnect).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["connecting", "idle"]);
  });

  it("public disconnect 同步触发 SDK Disconnected 时只回调 idle", async () => {
    const room = new FakeLiveKitRoom();
    room.emitDisconnectedOnDisconnect = "sync";
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await client.disconnect();

    expect(statuses).toEqual(["connecting", "connected", "idle"]);
  });

  it("public disconnect 异步触发 SDK Disconnected 时只回调 idle", async () => {
    const room = new FakeLiveKitRoom();
    room.emitDisconnectedOnDisconnect = "async";
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await client.disconnect();

    expect(statuses).toEqual(["connecting", "connected", "idle"]);
  });

  it("public disconnect 断开失败时向上抛出异常且不回调 idle", async () => {
    const room = new FakeLiveKitRoom();
    room.disconnect.mockRejectedValueOnce(new Error("disconnect failed"));
    const { handlers, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await expect(client.disconnect()).rejects.toThrow("disconnect failed");

    expect(statuses).toEqual(["connecting", "connected"]);
  });

  it("连接失败时进入 failed，并优先使用错误 message 作为原因", async () => {
    const room = new FakeLiveKitRoom();
    room.connect.mockRejectedValueOnce(new Error("room unavailable"));
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);

    expect(statuses).toEqual(["connecting", "failed"]);
    expect(reasons).toEqual([undefined, "room unavailable"]);
    expect(statuses).not.toContain("fallback");
  });

  it("连接过程中早到的助手音频轨已播放但 connect 失败时会释放播放资源", async () => {
    const room = new FakeLiveKitRoom();
    const connectDeferred = createDeferred();
    room.connect.mockReturnValueOnce(connectDeferred.promise);
    const audioPlayback = createAssistantAudioPlaybackSink();
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      assistantAudioPlayback: audioPlayback,
      createRoom: () => room
    });

    const connectPromise = client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);
    await vi.waitFor(() => {
      expect(room.connect).toHaveBeenCalledWith("wss://livekit.example", "token-a");
    });

    // connect 尚未完成时播放资源已经被接管；后续失败必须释放，避免隐藏 audio 元素和 LiveKit attach 关系泄漏。
    room.emitTrackSubscribed("assistant-1", { kind: "audio", name: "assistant-audio" });
    connectDeferred.reject(new Error("room unavailable"));
    await connectPromise;

    expect(audioPlayback.playTrack).toHaveBeenCalledTimes(1);
    expect(audioPlayback.release).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["connecting", "failed"]);
    expect(reasons).toEqual([undefined, "room unavailable"]);
  });

  it("连接失败且错误没有 message 时使用默认中文原因", async () => {
    const room = new FakeLiveKitRoom();
    room.connect.mockRejectedValueOnce({});
    const { handlers, reasons, statuses } = createHandlers();
    const client = new LiveKitRoomClient({
      createRoom: () => room
    });

    await client.connect({ token: "token-a", url: "wss://livekit.example" }, handlers);

    expect(statuses).toEqual(["connecting", "failed"]);
    expect(reasons).toEqual([undefined, "LiveKit 房间连接失败。"]);
  });
});
