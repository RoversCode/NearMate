import { Room } from "livekit-client";
import type { LiveKitRoomLike } from "./livekitRoomTypes";

/**
 * 真实 LiveKit Room 的默认配置。
 *
 * 功能：集中声明房间媒体策略和麦克风采集约束。
 * 核心入参：无，作为 Room 构造参数使用。
 * 预期输出：启用自适应流、dynacast，并显式开启 WebRTC 回声消除、降噪和自动增益。
 * 边界异常：浏览器若不支持某项采集约束，会由底层 WebRTC/LiveKit 按平台能力处理。
 */
export const DEFAULT_LIVEKIT_ROOM_OPTIONS = {
  adaptiveStream: true,
  audioCaptureDefaults: {
    autoGainControl: true,
    echoCancellation: true,
    noiseSuppression: true
  },
  dynacast: true
} as const;

/**
 * createDefaultLiveKitRoom 创建生产环境真实 LiveKit room。
 *
 * Returns:
 *   使用桌面端默认媒体策略创建的 LiveKit 房间。
 */
export function createDefaultLiveKitRoom(): LiveKitRoomLike {
  return new Room(DEFAULT_LIVEKIT_ROOM_OPTIONS);
}
