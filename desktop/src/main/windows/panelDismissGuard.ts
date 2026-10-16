type Bounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type Point = {
  x: number;
  y: number;
};

type PanelWindowPort = {
  on(eventName: "blur" | "focus", listener: () => void): unknown;
  off?(eventName: "blur" | "focus", listener: () => void): unknown;
  removeListener?(eventName: "blur" | "focus", listener: () => void): unknown;
  isFocused(): boolean;
  getBounds(): Bounds;
};

type ScreenPort = {
  getCursorScreenPoint(): Point;
};

type PanelDismissGuardInput = {
  panelWindow: PanelWindowPort;
  screen: ScreenPort;
  delayMs: number;
  closePanelFromExternalBlur(): void;
};

/**
 * createPanelDismissGuard 把侧边板 blur 降级为外部点击候选信号。
 *
 * @param input 侧边板窗口、屏幕鼠标读取能力、确认延迟与外部关闭回调。
 * @returns 清理函数，用于销毁运行时时取消定时器和解绑窗口事件。
 */
export function createPanelDismissGuard(input: PanelDismissGuardInput): () => void {
  let blurConfirmTimer: ReturnType<typeof setTimeout> | null = null;

  const cancelPendingClose = () => {
    if (!blurConfirmTimer) {
      return;
    }

    clearTimeout(blurConfirmTimer);
    blurConfirmTimer = null;
  };

  const confirmExternalBlurClose = () => {
    blurConfirmTimer = null;

    // 面板可能在 Electron 焦点抖动后重新聚焦，此时不能把内部点击误判为外部关闭。
    if (input.panelWindow.isFocused()) {
      return;
    }

    // 失焦但鼠标仍停留在面板内，通常来自面板内部控件或窗口层级切换，不应关闭。
    if (isPointInsideBounds(input.screen.getCursorScreenPoint(), input.panelWindow.getBounds())) {
      return;
    }

    input.closePanelFromExternalBlur();
  };

  const scheduleExternalBlurClose = () => {
    cancelPendingClose();
    blurConfirmTimer = setTimeout(confirmExternalBlurClose, input.delayMs);
  };

  input.panelWindow.on("blur", scheduleExternalBlurClose);
  input.panelWindow.on("focus", cancelPendingClose);

  return () => {
    cancelPendingClose();
    removeWindowListener(input.panelWindow, "blur", scheduleExternalBlurClose);
    removeWindowListener(input.panelWindow, "focus", cancelPendingClose);
  };
}

/**
 * isPointInsideBounds 判断屏幕坐标是否仍落在面板矩形内。
 *
 * @param point 当前鼠标屏幕坐标。
 * @param bounds 当前面板窗口边界。
 * @returns 坐标落在矩形内时返回 true。
 */
function isPointInsideBounds(point: Point, bounds: Bounds): boolean {
  return (
    point.x >= bounds.x &&
    point.x <= bounds.x + bounds.width &&
    point.y >= bounds.y &&
    point.y <= bounds.y + bounds.height
  );
}

/**
 * removeWindowListener 兼容 Electron EventEmitter 的 off 与 removeListener 两种解绑接口。
 *
 * @param panelWindow 需要解绑监听器的面板窗口端口。
 * @param eventName 窗口事件名。
 * @param listener 之前注册的监听函数。
 */
function removeWindowListener(
  panelWindow: PanelWindowPort,
  eventName: "blur" | "focus",
  listener: () => void
): void {
  if (panelWindow.off) {
    panelWindow.off(eventName, listener);
    return;
  }

  panelWindow.removeListener?.(eventName, listener);
}
