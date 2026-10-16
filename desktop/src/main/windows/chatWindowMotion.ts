import type { Rectangle } from "electron";

export type ChatWindowMode = "collapsed" | "expanded";

export const CHAT_WINDOW_COLLAPSED_SIZE = {
  width: 96,
  height: 96
} as const;

export const CHAT_WINDOW_EXPANDED_SIZE = {
  width: 900,
  height: 640
} as const;

export const CHAT_WINDOW_MIN_SIZE = {
  width: 760,
  height: 520
} as const;

const CHAT_WINDOW_COLLAPSED_LEFT_MARGIN = 24;
const CHAT_WINDOW_COLLAPSED_BOTTOM_MARGIN = 36;
const CHAT_WINDOW_EXPANDED_MARGIN = 16;

type ResolveChatWindowBoundsInput = {
  mode: ChatWindowMode;
  workArea: Rectangle;
  anchorBounds?: Rectangle | null;
};

export type ChatWindowMotionController = {
  currentMode(): ChatWindowMode | null;
  resolveBounds(mode: ChatWindowMode, workArea: Rectangle): Rectangle;
  rememberBounds(bounds: Rectangle): void;
  reset(): void;
};

/**
 * 计算聊天窗口在当前工作区内的默认边界。
 *
 * Args:
 *   input: 目标模式、工作区和可选的上一模式锚点。
 *
 * Returns:
 *   可直接传给 BrowserWindow.setBounds 的窗口矩形。
 */
export function resolveChatWindowBounds(input: ResolveChatWindowBoundsInput): Rectangle {
  const size = resolveChatWindowSize(input.mode, input.workArea);

  if (input.anchorBounds) {
    return clampChatWindowBounds({
      x: input.anchorBounds.x,
      y: input.anchorBounds.y + input.anchorBounds.height - size.height,
      width: size.width,
      height: size.height
    }, input.workArea);
  }

  if (input.mode === "collapsed") {
    return {
      x: input.workArea.x + CHAT_WINDOW_COLLAPSED_LEFT_MARGIN,
      y: input.workArea.y + input.workArea.height - CHAT_WINDOW_COLLAPSED_SIZE.height - CHAT_WINDOW_COLLAPSED_BOTTOM_MARGIN,
      width: CHAT_WINDOW_COLLAPSED_SIZE.width,
      height: CHAT_WINDOW_COLLAPSED_SIZE.height
    };
  }

  return {
    x: input.workArea.x + CHAT_WINDOW_EXPANDED_MARGIN,
    y: input.workArea.y + input.workArea.height - size.height - CHAT_WINDOW_EXPANDED_MARGIN,
    width: size.width,
    height: size.height
  };
}

/**
 * 创建聊天窗口位置与尺寸的运行期状态控制器。
 *
 * Returns:
 *   分别记忆折叠锚点和展开边界的控制器。
 */
export function createChatWindowMotion(): ChatWindowMotionController {
  let currentMode: ChatWindowMode | null = null;
  let collapsedBounds: Rectangle | null = null;
  let expandedBounds: Rectangle | null = null;

  return {
    currentMode() {
      return currentMode;
    },
    resolveBounds(mode, workArea) {
      const storedBounds = mode === "collapsed" ? collapsedBounds : expandedBounds;
      const oppositeBounds = currentMode === "collapsed" ? collapsedBounds : expandedBounds;
      const size = mode === "expanded" && expandedBounds
        ? { width: expandedBounds.width, height: expandedBounds.height }
        : resolveChatWindowSize(mode, workArea);
      const candidate = storedBounds && currentMode === mode
        ? storedBounds
        : oppositeBounds
          ? {
              x: oppositeBounds.x,
              y: oppositeBounds.y + oppositeBounds.height - size.height,
              ...size
            }
          : storedBounds ?? resolveChatWindowBounds({ mode, workArea });
      const bounds = clampChatWindowBounds({ ...candidate, ...size }, workArea);

      currentMode = mode;
      if (mode === "collapsed") {
        collapsedBounds = bounds;
      } else {
        expandedBounds = bounds;
      }
      return bounds;
    },
    rememberBounds(bounds) {
      if (currentMode === "collapsed") {
        collapsedBounds = { ...bounds };
      } else if (currentMode === "expanded") {
        expandedBounds = { ...bounds };
      }
    },
    reset() {
      currentMode = null;
      collapsedBounds = null;
      expandedBounds = null;
    }
  };
}

/**
 * 把聊天窗口边界限制在工作区内。
 *
 * Args:
 *   bounds: 待限制的窗口矩形。
 *   workArea: 当前显示器可用工作区。
 *
 * Returns:
 *   尺寸和位置均不越出工作区的矩形。
 */
export function clampChatWindowBounds(bounds: Rectangle, workArea: Rectangle): Rectangle {
  const width = Math.min(bounds.width, workArea.width);
  const height = Math.min(bounds.height, workArea.height);
  const maxX = workArea.x + Math.max(0, workArea.width - width);
  const maxY = workArea.y + Math.max(0, workArea.height - height);

  return {
    x: clamp(bounds.x, workArea.x, maxX),
    y: clamp(bounds.y, workArea.y, maxY),
    width,
    height
  };
}

function resolveChatWindowSize(mode: ChatWindowMode, workArea: Rectangle): Pick<Rectangle, "width" | "height"> {
  if (mode === "collapsed") {
    return CHAT_WINDOW_COLLAPSED_SIZE;
  }

  return {
    width: Math.min(CHAT_WINDOW_EXPANDED_SIZE.width, Math.max(0, workArea.width - CHAT_WINDOW_EXPANDED_MARGIN * 2)),
    height: Math.min(CHAT_WINDOW_EXPANDED_SIZE.height, Math.max(0, workArea.height - CHAT_WINDOW_EXPANDED_MARGIN * 2))
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
