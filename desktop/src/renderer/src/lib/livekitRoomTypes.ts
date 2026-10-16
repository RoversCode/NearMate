import { RoomEvent } from "livekit-client";
import type { ConnectionStatus } from "../types/events";
import type { AssistantAudioPlaybackSink } from "./assistantAudioPlayback";

/**
 * LiveKit 连接参数故意保持极简。
 * 当前桌面壳只需要知道“有没有地址、有没有 token、要不要自动发麦克风”。
 */
export type LiveKitRoomConnectConfig = {
  url?: string;
  token?: string;
  autoPublishMicrophone?: boolean;
};

/**
 * LiveKit 房间的窄接口只保留 renderer 媒体客户端真正会调用的能力。
 * 核心输出是让测试可以注入 fake room，同时避免客户端依赖完整 LiveKit Room 实例。
 */
export type LiveKitRoomLike = {
  connect(url: string, token: string): Promise<void>;
  disconnect(): Promise<void>;
  on(
    event: typeof RoomEvent.TrackSubscribed,
    listener: (track: unknown, publication: unknown, participant: { identity: string }) => void
  ): unknown;
  on(
    event: typeof RoomEvent.TrackUnsubscribed,
    listener: (track: unknown, publication: unknown, participant: { identity: string }) => void
  ): unknown;
  on(event: typeof RoomEvent.Disconnected, listener: () => void): unknown;
  localParticipant: {
    setMicrophoneEnabled(enabled: boolean): Promise<unknown>;
  };
};

/**
 * LiveKitRoomClient 构造参数用于注入房间工厂。
 * 核心入参是可选 createRoom；缺省时会创建启用自适应流和 dynacast 的真实 LiveKit Room。
 */
export type LiveKitRoomClientConfig = {
  createRoom?: () => LiveKitRoomLike;
  assistantAudioPlayback?: AssistantAudioPlaybackSink;
  localListeningMonitor?: import("./livekitLocalListeningMonitor").LocalListeningMonitor;
};

/**
 * renderer 只关心媒体层连接状态和是否收到助手音轨订阅事件。
 */
export type LiveKitRoomHandlers = {
  onStatusChange(status: ConnectionStatus, reason?: string): void;
  onAssistantTrackSubscribed(identity: string): void;
  onLocalListeningHintChanged?(active: boolean): void;
  onAssistantPlaybackReady?(): void;
  onAssistantPlaybackUnavailable?(code: string, message: string): void;
};
