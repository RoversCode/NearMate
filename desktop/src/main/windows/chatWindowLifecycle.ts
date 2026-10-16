import type { Rectangle } from "electron";
import type { ChatWindowMode, ChatWindowMotionController } from "./chatWindowMotion";

/**
 * ChatWindowLifecycleOwner 是聊天窗唯一的主进程生命周期拥有者。
 * 它统一维护 mode、窗口几何、显示/隐藏、聚焦以及 Avatar 同步，避免入口之间各自拼装半套流程。
 */
export type ChatWindowLifecycleOwner = {
  currentMode(): ChatWindowMode;
  setRendererMode(mode: ChatWindowMode): void;
  openChat(options?: { focus?: boolean; requestId?: string }): void;
  hideChat(): void;
  initializeHidden(): void;
};

type CreateChatWindowLifecycleOwnerInput = {
  chatWindow: {
    setBounds(bounds: Rectangle): void;
    show(): void;
    showInactive(): void;
    hide(): void;
    focus(): void;
    isDestroyed(): boolean;
    webContents: { send(channel: string, payload: unknown): void };
  };
  chatWindowMotion: ChatWindowMotionController;
  getWorkArea(): Rectangle;
  sendAvatarMode(mode: ChatWindowMode): void;
  sendChatModeRequest(mode: ChatWindowMode): void;
  getAvatarSummonMode?(): string;
};

/**
 * 创建主进程聊天生命周期控制器。
 *
 * Args:
 *   input: 聊天窗口、几何控制器、工作区读取函数和跨窗口同步回调。
 *
 * Returns:
 *   可供所有入口复用的 Chat 生命周期拥有者。
 */
export function createChatWindowLifecycleOwner(
  input: CreateChatWindowLifecycleOwnerInput
): ChatWindowLifecycleOwner {
  let mode: ChatWindowMode = "collapsed";
  let visible = false;

  function isAlive(): boolean {
    return !input.chatWindow.isDestroyed();
  }

  function publish(nextMode: ChatWindowMode): void {
    mode = nextMode;
    input.sendAvatarMode(nextMode);
  }

  function hideChat(): void {
    if (!isAlive()) {
      return;
    }

    // 隐藏不销毁 renderer，草稿、控制连接和运行期消息因此得以保留。
    input.chatWindow.hide();
    visible = false;
    publish("collapsed");
    input.sendChatModeRequest("collapsed");
  }

  return {
    currentMode(): ChatWindowMode {
      return mode;
    },
    setRendererMode(nextMode): void {
      if (nextMode === "collapsed") {
        hideChat();
        return;
      }

      // Renderer 只报告用户意图；真正打开、定位和聚焦仍由主进程统一完成。
      if (mode === "expanded" && visible) {
        return;
      }
      this.openChat({ focus: true });
    },
    openChat(options = {}): void {
      if (!isAlive()) {
        return;
      }

      // 桌宠被系统收起时，不允许打开 Chat 抢回桌面焦点。
      if (input.getAvatarSummonMode?.() === "hidden") {
        input.chatWindow.hide();
        visible = false;
        publish("collapsed");
        return;
      }

      const bounds = input.chatWindowMotion.resolveBounds("expanded", input.getWorkArea());
      input.chatWindow.setBounds(bounds);
      publish("expanded");
      input.sendChatModeRequest("expanded");
      if (options.focus !== false) {
        input.chatWindow.show();
        input.chatWindow.focus();
      } else {
        input.chatWindow.showInactive();
      }
      visible = true;
      if (options.requestId) {
        input.chatWindow.webContents.send("chat:focus-background-interaction", {
          requestId: options.requestId
        });
      }
    },
    hideChat,
    initializeHidden(): void {
      // 首帧以 collapsed 水合，但窗口保持隐藏，避免启动时出现桌面浮球。
      input.chatWindowMotion.resolveBounds("collapsed", input.getWorkArea());
      hideChat();
    }
  };
}
