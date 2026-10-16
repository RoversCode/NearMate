import { RoomEvent } from "livekit-client";
import { getLiveKitRoomErrorReason } from "./livekitRoomErrors";
import {
  isAssistantAudioTrack,
  isCleanMicrophoneTrack
} from "./livekitRoomTrack";
import type {
  LiveKitRoomHandlers,
  LiveKitRoomLike
} from "./livekitRoomTypes";

export type LiveKitRoomLifecycleState = {
  connectionAttemptId: number;
  pendingRoom: LiveKitRoomLike | null;
  room: LiveKitRoomLike | null;
};

type AttachLiveKitRoomListenersInput = {
  attemptId: number;
  handlers: LiveKitRoomHandlers;
  manuallyDisconnectingRooms: Set<LiveKitRoomLike>;
  room: LiveKitRoomLike;
  state: LiveKitRoomLifecycleState;
  playAssistantAudioTrack(track: unknown, handlers: LiveKitRoomHandlers): void;
  releaseAssistantAudioPlayback(): void;
  startLocalListeningMonitor(track: unknown, handlers: LiveKitRoomHandlers): void;
  releaseLocalListeningMonitor(track?: unknown): void;
};

type DisconnectLiveKitRoomInput = {
  manuallyDisconnectingRooms: Set<LiveKitRoomLike>;
  releaseAssistantAudioPlayback(): void;
  releaseLocalListeningMonitor(): void;
  room: LiveKitRoomLike;
  state: LiveKitRoomLifecycleState;
};

/**
 * attachLiveKitRoomListeners 接线单个 room 的 SDK 事件。
 *
 * Args:
 *   input: 当前连接尝试、room、状态快照和播放资源回调。
 */
export function attachLiveKitRoomListeners(input: AttachLiveKitRoomListenersInput): void {
  input.room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
    if (!isCurrentRoomForAttempt(input.state, input.room, input.attemptId)) {
      return;
    }

    if (isAssistantAudioTrack(track, publication)) {
      input.playAssistantAudioTrack(track, input.handlers);
      input.handlers.onAssistantTrackSubscribed(participant.identity);
      return;
    }

    if (isCleanMicrophoneTrack(track, publication)) {
      input.startLocalListeningMonitor(track, input.handlers);
    }
  });

  input.room.on(RoomEvent.TrackUnsubscribed, (track, publication) => {
    if (!isCurrentRoomForAttempt(input.state, input.room, input.attemptId)) {
      return;
    }

    if (isCleanMicrophoneTrack(track, publication)) {
      input.releaseLocalListeningMonitor(track);
    }
  });

  input.room.on(RoomEvent.Disconnected, () => {
    if (!isCurrentRoomForAttempt(input.state, input.room, input.attemptId)) {
      return;
    }

    input.state.room = null;
    input.releaseAssistantAudioPlayback();
    input.releaseLocalListeningMonitor();
    if (input.manuallyDisconnectingRooms.has(input.room)) {
      return;
    }

    input.handlers.onStatusChange("disconnected", "LiveKit 房间连接已断开。");
  });
}

/**
 * disconnectLiveKitRoomForReconnect 为重连释放旧房间。
 *
 * Args:
 *   input: 待断开的 room、生命周期状态、手动断开集合和播放资源释放回调。
 *
 * Returns:
 *   断开失败原因；成功时返回 null。
 */
export async function disconnectLiveKitRoomForReconnect(
  input: DisconnectLiveKitRoomInput
): Promise<string | null> {
  return disconnectLiveKitRoomSafely(input);
}

/**
 * disconnectLiveKitRoomAfterFailure 连接后置步骤失败时释放当前房间。
 *
 * Args:
 *   input: 待断开的 room、生命周期状态、手动断开集合和播放资源释放回调。
 *
 * Returns:
 *   清理失败原因；成功时返回 null。
 */
export async function disconnectLiveKitRoomAfterFailure(
  input: DisconnectLiveKitRoomInput
): Promise<string | null> {
  return disconnectLiveKitRoomSafely(input);
}

/**
 * isCurrentRoomForAttempt 判断事件来源 room 是否仍属于当前连接尝试。
 *
 * Args:
 *   state: 当前连接生命周期状态。
 *   room: 触发事件的 room。
 *   attemptId: 注册该事件监听器时的连接尝试编号。
 *
 * Returns:
 *   当前连接尝试中的 pending room 或已连接 room 返回 true。
 */
function isCurrentRoomForAttempt(
  state: LiveKitRoomLifecycleState,
  room: LiveKitRoomLike,
  attemptId: number
): boolean {
  // 连接尝试编号先过滤旧闭包，业务意图是让重连/断开后的迟到事件无法接管新会话资源。
  if (state.connectionAttemptId !== attemptId) {
    return false;
  }

  // pendingRoom 覆盖 connect 尚未 resolve 的窗口；后端先发布助手轨道时，LiveKit 可能在这里就下发订阅事件。
  return state.room === room || state.pendingRoom === room;
}

async function disconnectLiveKitRoomSafely(
  input: DisconnectLiveKitRoomInput
): Promise<string | null> {
  input.manuallyDisconnectingRooms.add(input.room);
  try {
    await input.room.disconnect();
    if (input.state.room === input.room) {
      input.state.room = null;
    }
    if (input.state.pendingRoom === input.room) {
      input.state.pendingRoom = null;
    }
    input.releaseAssistantAudioPlayback();
    input.releaseLocalListeningMonitor();
    return null;
  } catch (error) {
    return getLiveKitRoomErrorReason(error, "LiveKit 房间断开失败。");
  } finally {
    input.manuallyDisconnectingRooms.delete(input.room);
  }
}
