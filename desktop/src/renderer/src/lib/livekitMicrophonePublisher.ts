import type { LiveKitRoomLike } from "./livekitRoomTypes";

/**
 * publishLiveKitMicrophone 在 room 已连接时发布本地麦克风。
 *
 * Args:
 *   room: 当前已连接的 LiveKit room，未连接时传入 null。
 */
export async function publishLiveKitMicrophone(room: LiveKitRoomLike | null): Promise<void> {
  if (!room) {
    return;
  }

  await room.localParticipant.setMicrophoneEnabled(true);
}
