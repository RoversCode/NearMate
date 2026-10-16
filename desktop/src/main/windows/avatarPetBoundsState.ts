import type { BrowserWindow } from "electron";
import type { AvatarPetBounds } from "../../shared/live2dModelTypes";
import { readAvatarPetBounds } from "./avatarWindowInteractionPayloads";

/**
 * AvatarPetBoundsCache 保存最近一次 renderer 上报的人物可见边界。
 */
export type AvatarPetBoundsCache = {
  remember(payload: unknown): boolean;
  readInScreen(avatarWindow: BrowserWindow): AvatarPetBounds | null;
  reset(): void;
};

/**
 * createAvatarPetBoundsCache 创建人物可见边界缓存。
 *
 * @returns 可写入、读取和重置的边界缓存。
 */
export function createAvatarPetBoundsCache(): AvatarPetBoundsCache {
  let state: AvatarPetBounds | null = null;

  return {
    remember(payload) {
      if (payload === null) {
        state = null;
        return true;
      }

      const nextPetBounds = readAvatarPetBounds(payload);

      if (!nextPetBounds) {
        return false;
      }

      state = nextPetBounds;
      return true;
    },
    readInScreen(avatarWindow) {
      if (!state) {
        return null;
      }

      const windowBounds = avatarWindow.getBounds();

      return {
        x: windowBounds.x + state.x,
        y: windowBounds.y + state.y,
        width: state.width,
        height: state.height
      };
    },
    reset() {
      state = null;
    }
  };
}
