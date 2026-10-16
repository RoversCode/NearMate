import type { BrowserWindow } from "electron";

const AVATAR_FIRST_SHOW_FALLBACK_DELAY_MS = 150;

/**
 * AvatarFirstShowGate 让安装版成功反馈先完成，再统一开放角色窗首显。
 */
export type AvatarFirstShowGate = {
  /** 阻挡计时兜底与展示档案首显；返回值为空。 */
  hold(): void;
  /** 放行仍存活的待显示窗口，重复调用不会重复首显；返回值为空。 */
  release(): void;
  schedule(avatarWindow: BrowserWindow): void;
  showIfNeeded(avatarWindow: BrowserWindow): void;
  reset(): void;
};

type AvatarFirstShowState = {
  isHeld: boolean;
  pendingWindow: BrowserWindow | null;
  hasShown: boolean;
  fallbackTimer: ReturnType<typeof setTimeout> | null;
};

/**
 * createAvatarFirstShowGate 创建角色窗首次显示门闩。
 *
 * @returns 可阻挡、放行、调度和重置的首显控制器。
 */
export function createAvatarFirstShowGate(): AvatarFirstShowGate {
  let state: AvatarFirstShowState = {
    isHeld: false,
    pendingWindow: null,
    hasShown: false,
    fallbackTimer: null
  };

  function cancelFallback(): void {
    if (!state.fallbackTimer) {
      return;
    }

    clearTimeout(state.fallbackTimer);
    state.fallbackTimer = null;
  }

  return {
    hold() {
      state.isHeld = true;
      cancelFallback();
    },
    release() {
      state.isHeld = false;
      const pendingWindow = state.pendingWindow;
      state.pendingWindow = null;
      if (pendingWindow) this.showIfNeeded(pendingWindow);
    },
    schedule(avatarWindow) {
      // 安装版先在隐藏窗口中加载，150ms 兜底也不能跨过成功反馈的交接屏障。
      if (state.isHeld) {
        state.pendingWindow = avatarWindow;
        return;
      }
      if (state.hasShown || state.fallbackTimer) {
        return;
      }

      state.fallbackTimer = setTimeout(() => {
        state.fallbackTimer = null;
        this.showIfNeeded(avatarWindow);
      }, AVATAR_FIRST_SHOW_FALLBACK_DELAY_MS);
    },
    showIfNeeded(avatarWindow) {
      if (state.isHeld) {
        state.pendingWindow = avatarWindow;
        return;
      }
      if (avatarWindow.isDestroyed()) return;
      if (state.hasShown) {
        return;
      }

      cancelFallback();
      state.hasShown = true;
      avatarWindow.show();
    },
    reset() {
      cancelFallback();
      state = {
        // 失败或退出中的阻挡不能被窗口销毁顺手解除；普通重建继续沿用未阻挡状态。
        isHeld: state.isHeld,
        pendingWindow: null,
        hasShown: false,
        fallbackTimer: null
      };
    }
  };
}
