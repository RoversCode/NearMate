import type { AssistantAudioPlaybackSink } from "./assistantAudioPlayback";
import {
  createDefaultLiveKitRoom,
  DEFAULT_LIVEKIT_ROOM_OPTIONS
} from "./livekitRoomConfig";
import { getLiveKitRoomErrorReason } from "./livekitRoomErrors";
import {
  attachLiveKitRoomListeners,
  disconnectLiveKitRoomAfterFailure,
  disconnectLiveKitRoomForReconnect,
  type LiveKitRoomLifecycleState
} from "./livekitRoomLifecycle";
import { publishLiveKitMicrophone } from "./livekitMicrophonePublisher";
import { LiveKitAssistantTrackPlayback } from "./livekitAssistantTrackPlayback";
import {
  LiveKitLocalListeningMonitor,
  type LocalListeningMonitor
} from "./livekitLocalListeningMonitor";
import type {
  LiveKitRoomConnectConfig,
  LiveKitRoomHandlers,
  LiveKitRoomLike,
  LiveKitRoomClientConfig
} from "./livekitRoomTypes";

export { DEFAULT_LIVEKIT_ROOM_OPTIONS };
export type {
  LiveKitRoomConnectConfig,
  LiveKitRoomHandlers,
  LiveKitRoomLike
} from "./livekitRoomTypes";

/**
 * LiveKitRoomClient 是桌面端严格媒体客户端。
 * 它只负责真实 LiveKit 房间连接、远端音轨订阅通知和本地麦克风发布，不再制造本地媒体 fallback。
 */
export class LiveKitRoomClient {
  private readonly createRoom: () => LiveKitRoomLike;
  private readonly assistantTrackPlayback: LiveKitAssistantTrackPlayback;
  private readonly localListeningMonitor: LocalListeningMonitor;
  private readonly manuallyDisconnectingRooms = new Set<LiveKitRoomLike>();
  private readonly lifecycleState: LiveKitRoomLifecycleState = {
    connectionAttemptId: 0,
    pendingRoom: null,
    room: null
  };
  private handlers: LiveKitRoomHandlers | null = null;

  /**
   * 初始化媒体客户端。
   * 核心入参是可选房间工厂；预期输出是一个可复用的连接客户端，边界上测试可用 fake room 替代真实服务。
   */
  constructor(config: LiveKitRoomClientConfig = {}) {
    this.createRoom = config.createRoom ?? createDefaultLiveKitRoom;
    this.assistantTrackPlayback = new LiveKitAssistantTrackPlayback(
      config.assistantAudioPlayback ?? null
    );
    this.localListeningMonitor = config.localListeningMonitor ?? new LiveKitLocalListeningMonitor();
  }

  /**
   * 连接真实 LiveKit 房间。
   * 核心入参是房间地址、token、自动发麦克风开关与状态回调；预期输出是严格的连接状态。
   * 边界异常：缺少 url/token 或 connect 抛错都会进入 failed，并且不会回退到本地占位媒体。
   */
  async connect(config: LiveKitRoomConnectConfig, handlers: LiveKitRoomHandlers): Promise<void> {
    this.handlers = handlers;
    const attemptId = this.lifecycleState.connectionAttemptId + 1;
    this.lifecycleState.connectionAttemptId = attemptId;
    const previousRoom = this.lifecycleState.room ?? this.lifecycleState.pendingRoom;

    /**
     * 所有连接尝试都先进入 connecting。
     * 即使后续旧房间清理失败，也能给 UI 一个明确的“开始重连但失败”生命周期。
     */
    handlers.onStatusChange("connecting");

    /**
     * 新连接开始前先释放旧房间。
     * 如果旧房间断开失败，不能清空引用或继续创建新 room，避免真实媒体连接重叠。
     */
    if (previousRoom) {
      const cleanupReason = await disconnectLiveKitRoomForReconnect({
        manuallyDisconnectingRooms: this.manuallyDisconnectingRooms,
        releaseAssistantAudioPlayback: () => this.assistantTrackPlayback.release(),
        releaseLocalListeningMonitor: () => this.localListeningMonitor.stop(),
        room: previousRoom,
        state: this.lifecycleState
      });
      if (cleanupReason) {
        handlers.onStatusChange("failed", cleanupReason);
        return;
      }
    }

    if (this.lifecycleState.connectionAttemptId !== attemptId) {
      return;
    }

    /**
     * 真实媒体链路必须同时具备房间地址和 token。
     * 缺任一项时立即失败，避免 UI 误把无凭据状态展示成可用 fallback。
     */
    if (!config.url || !config.token) {
      handlers.onStatusChange("failed", "未提供 LiveKit 房间地址或 token。");
      return;
    }

    /** 当前连接尝试创建出的 room，用于失败清理和事件闭包识别。 */
    let room: LiveKitRoomLike | null = null;

    try {
      const createdRoom = this.createRoom();
      room = createdRoom;
      this.lifecycleState.pendingRoom = createdRoom;

      attachLiveKitRoomListeners({
        attemptId,
        handlers,
        manuallyDisconnectingRooms: this.manuallyDisconnectingRooms,
        room: createdRoom,
        state: this.lifecycleState,
        playAssistantAudioTrack: (track, currentHandlers) => {
          void this.assistantTrackPlayback.playTrack(track, currentHandlers);
        },
        releaseAssistantAudioPlayback: () => this.assistantTrackPlayback.release(),
        startLocalListeningMonitor: (track, currentHandlers) => {
          this.localListeningMonitor.start(track, active => {
            currentHandlers.onLocalListeningHintChanged?.(active);
          });
        },
        releaseLocalListeningMonitor: track => this.localListeningMonitor.stop(track)
      });

      /**
       * 只有 connect 成功后才把 room 设为当前连接。
       * 这样失败路径不会暴露半初始化 room，也不会让 publishMicrophone 误认为已连接。
       */
      await createdRoom.connect(config.url, config.token);
      if (
        this.lifecycleState.connectionAttemptId !== attemptId ||
        this.lifecycleState.pendingRoom !== createdRoom
      ) {
        return;
      }

      this.lifecycleState.pendingRoom = null;
      this.lifecycleState.room = createdRoom;
      handlers.onStatusChange("connected");

      /**
       * 自动发麦克风是后续实时语音接入的预留口。
       * 当前默认关闭，避免在 stub 阶段无意抢占系统录音权限。
       */
      if (config.autoPublishMicrophone) {
        await this.publishMicrophone();
      }
    } catch (error) {
      if (this.lifecycleState.connectionAttemptId !== attemptId) {
        return;
      }

      /**
       * 连接失败是严格媒体客户端的终态错误。
       * 优先透传 SDK 错误信息，缺少 message 时使用稳定中文兜底，方便 UI 和日志统一展示。
       */
      const reason = getLiveKitRoomErrorReason(error, "LiveKit 房间连接失败。");

      /**
       * 如果失败发生在 connect 成功后的后置步骤，例如自动发布麦克风，
       * 必须先静默断开已经建立的 room，避免底层媒体连接泄漏。
       */
      if (room && this.lifecycleState.room === room) {
        const cleanupReason = await disconnectLiveKitRoomAfterFailure({
          manuallyDisconnectingRooms: this.manuallyDisconnectingRooms,
          releaseAssistantAudioPlayback: () => this.assistantTrackPlayback.release(),
          releaseLocalListeningMonitor: () => this.localListeningMonitor.stop(),
          room,
          state: this.lifecycleState
        });
        if (cleanupReason) {
          handlers.onStatusChange("failed", reason);
          return;
        }
      }

      handlers.onStatusChange("failed", reason);
      if (room && this.lifecycleState.pendingRoom === room) {
        this.lifecycleState.pendingRoom = null;
      }
      this.assistantTrackPlayback.release();
      this.localListeningMonitor.stop();
      this.lifecycleState.room = null;
    }
  }

  /**
   * 发布本地麦克风。
   * 核心输出是连接后启用本地麦克风；边界上未连接时幂等返回，避免调用方额外判断连接状态。
   */
  async publishMicrophone(): Promise<void> {
    await publishLiveKitMicrophone(this.lifecycleState.room);
  }

  /**
   * 断开当前 LiveKit 房间。
   * 核心输出是释放 room 并回调 idle；边界上未连接时幂等返回，不重复通知 UI。
   */
  async disconnect(): Promise<void> {
    const room = this.lifecycleState.room ?? this.lifecycleState.pendingRoom;
    if (!room) {
      this.localListeningMonitor.stop();
      return;
    }

    this.lifecycleState.connectionAttemptId += 1;

    /**
     * 主动断开必须等待 SDK 完成底层清理后再回调 idle。
     * 如果 SDK 断开失败，异常向上抛出，调用方可以决定如何展示或重试；这里不输出 token 等敏感上下文。
     */
    this.manuallyDisconnectingRooms.add(room);
    try {
      await room.disconnect();
      if (this.lifecycleState.room === room) {
        this.lifecycleState.room = null;
      }
      if (this.lifecycleState.pendingRoom === room) {
        this.lifecycleState.pendingRoom = null;
      }
      this.assistantTrackPlayback.release();
      this.localListeningMonitor.stop();
    } finally {
      this.manuallyDisconnectingRooms.delete(room);
    }
    this.handlers?.onStatusChange("idle");
  }
}

export type { AssistantAudioPlaybackSink };
