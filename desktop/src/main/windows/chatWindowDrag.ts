import type { BrowserWindow, Rectangle } from "electron";

type ChatDragPoint = {
  screenX: number;
  screenY: number;
};

export type ChatWindowDragController = {
  startDrag(chatWindow: BrowserWindow, payload: unknown): boolean;
  moveDrag(chatWindow: BrowserWindow, payload: unknown): boolean;
  endDrag(chatWindow?: BrowserWindow): void;
  reset(): void;
};

type ChatDragState = {
  startPoint: ChatDragPoint;
  startBounds: Rectangle;
};

/**
 * createChatWindowDragController 创建聊天浮层拖拽状态控制器。
 *
 * Args:
 *   rememberBounds: 拖拽产生新窗口位置后，用于同步位置记忆的回调。
 *
 * Returns:
 *   可供 IPC 处理器调用的拖拽控制器。
 */
export function createChatWindowDragController(
  rememberBounds: (bounds: Rectangle) => void
): ChatWindowDragController {
  let dragState: ChatDragState | null = null;

  return {
    startDrag(chatWindow, payload) {
      const point = readChatDragPoint(payload);
      if (!point) {
        return false;
      }

      dragState = {
        startPoint: point,
        startBounds: chatWindow.getBounds()
      };
      return true;
    },
    moveDrag(chatWindow, payload) {
      const point = readChatDragPoint(payload);
      if (!dragState || !point) {
        return false;
      }

      const nextBounds = {
        x: dragState.startBounds.x + point.screenX - dragState.startPoint.screenX,
        y: dragState.startBounds.y + point.screenY - dragState.startPoint.screenY,
        width: dragState.startBounds.width,
        height: dragState.startBounds.height
      };

      chatWindow.setBounds(nextBounds);
      rememberBounds(nextBounds);
      return true;
    },
    endDrag(chatWindow) {
      if (chatWindow) {
        rememberBounds(chatWindow.getBounds());
      }

      dragState = null;
    },
    reset() {
      dragState = null;
    }
  };
}

function readChatDragPoint(payload: unknown): ChatDragPoint | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const point = payload as Record<string, unknown>;
  if (typeof point.screenX !== "number" || typeof point.screenY !== "number") {
    return null;
  }

  if (!Number.isFinite(point.screenX) || !Number.isFinite(point.screenY)) {
    return null;
  }

  return {
    screenX: point.screenX,
    screenY: point.screenY
  };
}
