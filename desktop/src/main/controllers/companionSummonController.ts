import type { AvatarSummonMode } from "../shell/shellTypes";
import type { ChatWindowLifecycleOwner } from "../windows/chatWindowLifecycle";

type AvatarVisibilityController = {
  toggleAvatarSummonedVisibility(): AvatarSummonMode;
};

type ChatWindowEntryPort = {
  hide(): void;
  webContents: {
    send(channel: string, payload: unknown): void;
  };
};

type CreateCompanionSummonControllerInput = {
  avatarVisibilityController: AvatarVisibilityController;
  chatWindow: ChatWindowEntryPort;
  chatLifecycle?: ChatWindowLifecycleOwner;
};

export type CompanionSummonController = {
  toggleCompanionEntryVisibility(): AvatarSummonMode;
};

/**
 * createCompanionSummonController 编排桌宠入口召回动作。
 *
 * Args:
 *   input: 桌宠显隐控制器、聊天窗端口、聊天窗定位器与主屏工作区读取函数。
 *
 * Returns:
 *   可供快捷键和托盘复用的伴侣入口控制器。
 */
export function createCompanionSummonController(
  input: CreateCompanionSummonControllerInput
): CompanionSummonController {
  return {
    toggleCompanionEntryVisibility(): AvatarSummonMode {
      const summonMode = input.avatarVisibilityController.toggleAvatarSummonedVisibility();

      if (summonMode !== "summoned_topmost") {
        /**
         * 入口组收起时，聊天 renderer 也要回到隐藏态。
         * 下次打开时不会残留上一次展开输入框的视觉状态。
         */
        if (input.chatLifecycle) {
          input.chatLifecycle.hideChat();
        } else {
          input.chatWindow.webContents.send("chat:mode-requested", { mode: "collapsed" });
          input.chatWindow.hide();
        }
        return summonMode;
      }

      /**
       * 用户显式召回桌宠时，保持 Chat 隐藏，不抢焦点。
       */
      if (input.chatLifecycle) {
        input.chatLifecycle.hideChat();
      } else {
        input.chatWindow.webContents.send("chat:mode-requested", { mode: "collapsed" });
        // collapsed 是完全隐藏态，避免恢复已经移除的独立聊天浮球。
        input.chatWindow.hide();
      }
      return summonMode;
    }
  };
}
