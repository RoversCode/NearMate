import type { BrowserWindow, screen as electronScreen } from "electron";
import {
  DEFAULT_AVATAR_PRESENTATION_PROFILE,
  getAvatarPresentationProfile,
  type AvatarPresentationProfile
} from "../../shared/avatarPresentationProfile";
import type {
  AvatarModelLayoutStore,
  AvatarModelWindowBounds
} from "../stores/avatarModelLayoutStore";
import { readAvatarPresentationProfileApplyRequest } from "./avatarWindowInteractionPayloads";

/**
 * AvatarPresentationController 管理人物展示档案、待应用模型和布局记忆。
 */
export type AvatarPresentationController = {
  currentProfile(): AvatarPresentationProfile;
  apply(avatarWindow: BrowserWindow, payload: unknown, input?: { forceDefaultProfile?: boolean }): boolean;
  rememberPending(payload: unknown): boolean;
  applyPending(avatarWindow: BrowserWindow): void;
  resetCurrentLayout(avatarWindow: BrowserWindow): boolean;
  saveCurrentLayout(avatarWindow?: BrowserWindow): void;
  reset(): void;
};

type AvatarPresentationState = {
  currentModelId: string | null;
  pendingModelId: string | null;
  currentProfile: AvatarPresentationProfile;
};

/**
 * createAvatarPresentationController 创建人物展示档案控制器。
 *
 * @param screen Electron screen 模块窄接口。
 * @param layoutStore 按人物保存窗口几何的仓储。
 * @param showIfNeeded 首次应用档案后的显示回调。
 * @param pushVisibleArea 几何变化后的可见区推送回调。
 * @returns 人物展示档案控制器。
 */
export function createAvatarPresentationController(
  screen: Pick<typeof electronScreen, "getDisplayMatching">,
  layoutStore: AvatarModelLayoutStore | undefined,
  showIfNeeded: (avatarWindow: BrowserWindow) => void,
  pushVisibleArea: (avatarWindow: BrowserWindow) => void
): AvatarPresentationController {
  let state: AvatarPresentationState = {
    currentModelId: null,
    pendingModelId: null,
    currentProfile: DEFAULT_AVATAR_PRESENTATION_PROFILE
  };

  function readCurrentWorkArea(avatarWindow: BrowserWindow) {
    return screen.getDisplayMatching(avatarWindow.getBounds()).workArea;
  }

  function createDefaultProfileBounds(
    currentBounds: AvatarModelWindowBounds,
    nextProfile: AvatarPresentationProfile
  ): AvatarModelWindowBounds {
    const currentBottomCenterX = currentBounds.x + currentBounds.width / 2;
    const currentBottomY = currentBounds.y + currentBounds.height;

    return {
      x: Math.round(currentBottomCenterX - nextProfile.windowWidth / 2),
      y: Math.round(currentBottomY - nextProfile.windowHeight),
      width: nextProfile.windowWidth,
      height: nextProfile.windowHeight
    };
  }

  function applyModelId(
    avatarWindow: BrowserWindow,
    modelId: string,
    input?: { forceDefaultProfile?: boolean }
  ): void {
    if (!input?.forceDefaultProfile && state.currentModelId === modelId) {
      return;
    }

    const nextProfile = getAvatarPresentationProfile(modelId);
    const currentBounds = avatarWindow.getBounds();

    state.currentProfile = nextProfile;
    state.currentModelId = modelId;
    state.pendingModelId = null;

    /**
     * 用户手调后的几何优先于人物默认档案；重置入口会显式要求跳过保存记录。
     */
    const savedBounds = input?.forceDefaultProfile
      ? null
      : layoutStore?.read(modelId, readCurrentWorkArea(avatarWindow)) ?? null;
    avatarWindow.setBounds(savedBounds ?? createDefaultProfileBounds(currentBounds, nextProfile));

    showIfNeeded(avatarWindow);
    pushVisibleArea(avatarWindow);
  }

  return {
    currentProfile() {
      return state.currentProfile;
    },
    apply(avatarWindow, payload, input) {
      const profileApplyRequest = readAvatarPresentationProfileApplyRequest(payload);

      if (!profileApplyRequest) {
        return false;
      }

      applyModelId(avatarWindow, profileApplyRequest.modelId, input);
      return true;
    },
    rememberPending(payload) {
      const profileApplyRequest = readAvatarPresentationProfileApplyRequest(payload);

      if (!profileApplyRequest) {
        return false;
      }

      state.pendingModelId = profileApplyRequest.modelId;
      return true;
    },
    applyPending(avatarWindow) {
      if (!state.pendingModelId) {
        return;
      }

      applyModelId(avatarWindow, state.pendingModelId);
    },
    resetCurrentLayout(avatarWindow) {
      if (!state.currentModelId) {
        return false;
      }

      const currentModelId = state.currentModelId;
      layoutStore?.reset(currentModelId);
      applyModelId(avatarWindow, currentModelId, { forceDefaultProfile: true });
      return true;
    },
    saveCurrentLayout(avatarWindow) {
      if (!avatarWindow || !state.currentModelId) {
        return;
      }

      layoutStore?.save(state.currentModelId, avatarWindow.getBounds());
    },
    reset() {
      state = {
        currentModelId: null,
        pendingModelId: null,
        currentProfile: DEFAULT_AVATAR_PRESENTATION_PROFILE
      };
    }
  };
}
