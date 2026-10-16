import { describe, expect, it, vi } from "vitest";
import { createPanelDismissGuard } from "./panelDismissGuard";

type WindowEventName = "blur" | "focus";

/**
 * FakePanelWindow 用最小事件能力模拟侧边板窗口。
 * 它只暴露守卫真正依赖的焦点、边界和事件订阅行为，避免测试耦合真实 BrowserWindow。
 */
class FakePanelWindow {
  private readonly listeners = new Map<WindowEventName, Array<() => void>>();
  private focused = true;

  constructor(
    private readonly bounds = { x: 100, y: 120, width: 360, height: 520 }
  ) {}

  on(eventName: WindowEventName, listener: () => void): this {
    const listeners = this.listeners.get(eventName) ?? [];
    listeners.push(listener);
    this.listeners.set(eventName, listeners);
    return this;
  }

  off(eventName: WindowEventName, listener: () => void): this {
    this.listeners.set(
      eventName,
      (this.listeners.get(eventName) ?? []).filter(candidate => candidate !== listener)
    );
    return this;
  }

  isFocused(): boolean {
    return this.focused;
  }

  getBounds() {
    return { ...this.bounds };
  }

  requestBlur(): void {
    this.focused = false;
    this.emit("blur");
  }

  requestFocus(): void {
    this.focused = true;
    this.emit("focus");
  }

  countListeners(eventName: WindowEventName): number {
    return this.listeners.get(eventName)?.length ?? 0;
  }

  private emit(eventName: WindowEventName): void {
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener();
    }
  }
}

/**
 * createHarness 统一搭建守卫测试环境。
 * 入参可覆盖鼠标位置，输出包含窗口、关闭回调和可显式销毁的守卫。
 */
function createHarness(input?: {
  cursorPoint?: { x: number; y: number };
  delayMs?: number;
}) {
  const panelWindow = new FakePanelWindow();
  const closePanelFromExternalBlur = vi.fn();
  const cursorPoint = input?.cursorPoint ?? { x: 0, y: 0 };
  const stop = createPanelDismissGuard({
    panelWindow,
    screen: {
      getCursorScreenPoint: vi.fn(() => cursorPoint)
    },
    delayMs: input?.delayMs ?? 80,
    closePanelFromExternalBlur
  });

  return {
    panelWindow,
    closePanelFromExternalBlur,
    stop
  };
}

describe("createPanelDismissGuard", () => {
  it("blur 后马上重新 focus 时不会关闭侧边板", async () => {
    vi.useFakeTimers();

    try {
      const {
        panelWindow,
        closePanelFromExternalBlur
      } = createHarness({ cursorPoint: { x: 20, y: 20 } });

      panelWindow.requestBlur();
      panelWindow.requestFocus();
      await vi.advanceTimersByTimeAsync(80);

      expect(closePanelFromExternalBlur).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("blur 后鼠标仍在侧边板范围内时不会关闭侧边板", async () => {
    vi.useFakeTimers();

    try {
      const {
        panelWindow,
        closePanelFromExternalBlur
      } = createHarness({ cursorPoint: { x: 220, y: 260 } });

      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(80);

      expect(closePanelFromExternalBlur).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("blur 后鼠标在侧边板外且持续失焦时才关闭侧边板", async () => {
    vi.useFakeTimers();

    try {
      const {
        panelWindow,
        closePanelFromExternalBlur
      } = createHarness({ cursorPoint: { x: 20, y: 20 } });

      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(80);

      expect(closePanelFromExternalBlur).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("连续 blur 只执行最后一次延迟确认", async () => {
    vi.useFakeTimers();

    try {
      const {
        panelWindow,
        closePanelFromExternalBlur
      } = createHarness({ cursorPoint: { x: 20, y: 20 } });

      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(40);
      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(79);

      expect(closePanelFromExternalBlur).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(1);

      expect(closePanelFromExternalBlur).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("销毁守卫时会取消定时器并移除事件监听", async () => {
    vi.useFakeTimers();

    try {
      const {
        panelWindow,
        closePanelFromExternalBlur,
        stop
      } = createHarness({ cursorPoint: { x: 20, y: 20 } });

      panelWindow.requestBlur();
      stop();
      await vi.advanceTimersByTimeAsync(80);

      expect(closePanelFromExternalBlur).not.toHaveBeenCalled();
      expect(panelWindow.countListeners("blur")).toBe(0);
      expect(panelWindow.countListeners("focus")).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
