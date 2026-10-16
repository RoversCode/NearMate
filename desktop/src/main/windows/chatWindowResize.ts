import type { BrowserWindow, Rectangle } from "electron";
import { CHAT_WINDOW_MIN_SIZE, clampChatWindowBounds } from "./chatWindowMotion";

type ChatResizeEdge = "n" | "e" | "s" | "w" | "ne" | "nw" | "se" | "sw";

type ChatResizePoint = {
  screenX: number;
  screenY: number;
};

type ChatResizeState = {
  edge: ChatResizeEdge;
  startPoint: ChatResizePoint;
  startBounds: Rectangle;
  workArea: Rectangle;
};

export type ChatWindowResizeController = {
  startResize(chatWindow: BrowserWindow, payload: unknown): boolean;
  moveResize(chatWindow: BrowserWindow, payload: unknown): boolean;
  endResize(chatWindow?: BrowserWindow): void;
  reset(): void;
};

/**
 * 创建透明聊天窗口的自定义缩放控制器。
 *
 * Args:
 *   rememberBounds: 缩放后记录展开窗口边界的回调。
 *   resolveWorkArea: 根据起始边界选择目标显示器工作区的函数。
 *
 * Returns:
 *   只在主进程计算并应用窗口几何的缩放控制器。
 */
export function createChatWindowResizeController(
  rememberBounds: (bounds: Rectangle) => void,
  resolveWorkArea: (bounds: Rectangle) => Rectangle
): ChatWindowResizeController {
  let resizeState: ChatResizeState | null = null;

  return {
    startResize(chatWindow, payload) {
      const start = readResizeStart(payload);
      if (!start) {
        return false;
      }
      const startBounds = chatWindow.getBounds();
      resizeState = {
        edge: start.edge,
        startPoint: start,
        startBounds,
        workArea: resolveWorkArea(startBounds)
      };
      return true;
    },
    moveResize(chatWindow, payload) {
      const point = readResizePoint(payload);
      if (!resizeState || !point) {
        return false;
      }
      const bounds = resizeChatWindowBounds(resizeState, point);
      chatWindow.setBounds(bounds);
      rememberBounds(bounds);
      return true;
    },
    endResize(chatWindow) {
      if (chatWindow) {
        rememberBounds(chatWindow.getBounds());
      }
      resizeState = null;
    },
    reset() {
      resizeState = null;
    }
  };
}

/**
 * 根据缩放边和屏幕坐标计算下一帧窗口边界。
 *
 * Args:
 *   state: 缩放开始时冻结的边界和工作区。
 *   point: 当前指针屏幕坐标。
 *
 * Returns:
 *   满足最小尺寸且不越出工作区的窗口边界。
 */
function resizeChatWindowBounds(state: ChatResizeState, point: ChatResizePoint): Rectangle {
  const deltaX = point.screenX - state.startPoint.screenX;
  const deltaY = point.screenY - state.startPoint.screenY;
  const right = state.startBounds.x + state.startBounds.width;
  const bottom = state.startBounds.y + state.startBounds.height;
  let x = state.startBounds.x;
  let y = state.startBounds.y;
  let width = state.startBounds.width;
  let height = state.startBounds.height;

  if (state.edge.includes("w")) {
    x = Math.max(
      state.workArea.x,
      Math.min(state.startBounds.x + deltaX, right - CHAT_WINDOW_MIN_SIZE.width)
    );
    width = right - x;
  } else if (state.edge.includes("e")) {
    width = Math.min(
      state.workArea.x + state.workArea.width - x,
      Math.max(CHAT_WINDOW_MIN_SIZE.width, state.startBounds.width + deltaX)
    );
  }

  if (state.edge.includes("n")) {
    y = Math.max(
      state.workArea.y,
      Math.min(state.startBounds.y + deltaY, bottom - CHAT_WINDOW_MIN_SIZE.height)
    );
    height = bottom - y;
  } else if (state.edge.includes("s")) {
    height = Math.min(
      state.workArea.y + state.workArea.height - y,
      Math.max(CHAT_WINDOW_MIN_SIZE.height, state.startBounds.height + deltaY)
    );
  }

  return clampChatWindowBounds({ x, y, width, height }, state.workArea);
}

function readResizeStart(payload: unknown): (ChatResizePoint & { edge: ChatResizeEdge }) | null {
  const point = readResizePoint(payload);
  if (!point || !payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const edge = (payload as { edge?: unknown }).edge;
  if (!isResizeEdge(edge)) {
    return null;
  }
  return { ...point, edge };
}

function readResizePoint(payload: unknown): ChatResizePoint | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const point = payload as Record<string, unknown>;
  if (typeof point.screenX !== "number" || typeof point.screenY !== "number" ||
      !Number.isFinite(point.screenX) || !Number.isFinite(point.screenY)) {
    return null;
  }
  return { screenX: point.screenX, screenY: point.screenY };
}

function isResizeEdge(value: unknown): value is ChatResizeEdge {
  return value === "n" || value === "e" || value === "s" || value === "w" ||
    value === "ne" || value === "nw" || value === "se" || value === "sw";
}
