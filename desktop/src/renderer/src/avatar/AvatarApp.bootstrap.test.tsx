// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShortcutSound } from "../lib/shortcutSound";
import type { ShortcutFeedback } from "../../../shared/shortcutFeedback";

const useBootstrapStatusMock = vi.fn();
const useAvatarRuntimeMock = vi.fn();
const avatarStageRenderSpy = vi.fn();
const avatarResizeFrameRenderSpy = vi.fn();

vi.mock("../bootstrap/useBootstrapStatus", () => ({
  useBootstrapStatus: (...args: unknown[]) => useBootstrapStatusMock(...args)
}));

vi.mock("./useAvatarRuntime", () => ({
  useAvatarRuntime: (...args: unknown[]) => useAvatarRuntimeMock(...args)
}));

vi.mock("../components/AvatarStage", () => ({
  AvatarStage: (props: unknown) => {
    avatarStageRenderSpy(props);
    return <div className="avatar-stage-stub" />;
  }
}));

vi.mock("../components/AvatarDragSurface", () => ({
  AvatarDragSurface: () => <div className="avatar-drag-surface-stub" />
}));

vi.mock("../components/AvatarResizeFrame", () => ({
  AvatarResizeFrame: (props: unknown) => {
    avatarResizeFrameRenderSpy(props);
    return <div className="avatar-resize-frame-stub" />;
  }
}));

vi.mock("../components/AvatarActionRail", () => ({
  AvatarActionRail: () => <button type="button">open</button>
}));

vi.mock("../components/AvatarSpeechBubble", () => ({
  AvatarSpeechBubble: () => <div className="avatar-speech-bubble-stub" />
}));

import { AvatarApp } from "./AvatarApp";

describe("AvatarApp bootstrap gate", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  it("初始化阻断期三个已注册快捷键仍有短音，回执不重播且不启动业务运行时", async () => {
    useBootstrapStatusMock.mockReturnValue({ status: "blocked", reason: "uninitialized", retry: vi.fn() });
    const cue = vi.spyOn(ShortcutSound.prototype, "play").mockResolvedValue(undefined);
    let receive!: (feedback: ShortcutFeedback) => void;
    const unsubscribe = vi.fn();
    window.companionAvatar!.onShortcutFeedback = listener => { receive = listener; return unsubscribe; };
    act(() => root.render(<AvatarApp />));
    expect(cue).not.toHaveBeenCalled();
    act(() => {
      receive({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
      receive({ phase: "pressed", shortcutId: "avatar_visibility", sequence: 2 });
      receive({ phase: "pressed", shortcutId: "selection_capture", sequence: 3 });
      receive({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: null });
    });
    expect(cue).toHaveBeenCalledTimes(3);
    expect(useAvatarRuntimeMock).not.toHaveBeenCalled();
    act(() => root.unmount());
    expect(unsubscribe).toHaveBeenCalledOnce();
    cue.mockRestore();
  });

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    useBootstrapStatusMock.mockReset();
    useAvatarRuntimeMock.mockReset();
    avatarStageRenderSpy.mockReset();
    avatarResizeFrameRenderSpy.mockReset();
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      reportPetBounds: vi.fn(),
      openPanel: vi.fn(),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        },
        live2d: {
          defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
          catalog: [
            {
              id: "Haru",
              label: "Haru",
              modelPath: "/live2d/resources/Haru/Haru.model3.json"
            }
          ]
        }
      }
    };
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    delete (window as Window & { companionAvatar?: unknown }).companionAvatar;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("bootstrap 后端不可用时仍展示桌宠资源和重试气泡，并且不会启动 avatar runtime", () => {
    const retry = vi.fn();
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "backend_unavailable",
      retry
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const retryButton = Array.from(container.querySelectorAll("button"))
      .find(button => button.textContent === "重试");
    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
    };

    expect(container.querySelector('[data-window="avatar"]')).toBeTruthy();
    expect(container.querySelector(".avatar-stage-stub")).toBeTruthy();
    expect(latestAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");
    expect(container.textContent).toContain("本地服务当前不可用");
    expect(container.textContent).not.toContain("打开 console");
    expect(container.textContent).not.toContain("Bootstrap blocked");
    expect(retryButton).toBeTruthy();
    act(() => {
      retryButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(retry).toHaveBeenCalledOnce();
    expect(useAvatarRuntimeMock).not.toHaveBeenCalled();
  });

  it("bootstrap 未初始化时展示桌宠资源和持久 console 气泡，并且挂载后不立刻收回穿透", () => {
    const openConsole = vi.fn();
    const hoverEnter = vi.fn();
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void } })
      .companionAvatar!.openConsole = openConsole;
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const openConsoleButton = Array.from(container.querySelectorAll("button"))
      .find(button => button.textContent === "打开 console");
    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
    };

    expect(container.querySelector('[data-window="avatar"]')).toBeTruthy();
    expect(container.querySelector(".avatar-stage-stub")).toBeTruthy();
    expect(latestAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");
    expect(container.textContent).toContain("请先打开 console 配置必需的 LLM 职责，并完成当前角色语音的试听确认");
    expect(container.textContent).not.toContain("Bootstrap blocked");
    expect(openConsoleButton).toBeTruthy();
    expect(hoverEnter).not.toHaveBeenCalled();

    act(() => {
      openConsoleButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(openConsole).toHaveBeenCalledTimes(1);
    expect(useAvatarRuntimeMock).not.toHaveBeenCalled();
  });

  it("bootstrap 鼠标移动到引导气泡按钮时会收回穿透，避免按钮点不到", () => {
    const openConsole = vi.fn();
    const hoverEnter = vi.fn();
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void } })
      .companionAvatar!.openConsole = openConsole;
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    hoverEnter.mockClear();

    const openConsoleButton = Array.from(container.querySelectorAll("button"))
      .find(button => button.textContent === "打开 console");

    act(() => {
      openConsoleButton?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
  });

  it("初始化说明变高或窗口变窄后仍把完整气泡和按钮放进可见窗口", () => {
    vi.stubGlobal("innerWidth", 430);
    vi.stubGlobal("innerHeight", 520);
    let bubbleHeight = 190;
    let notifyResize: () => void = () => undefined;
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) {
        notifyResize = () => callback([], this as unknown as ResizeObserver);
      }
      observe() {}
      disconnect() {}
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return { height: this.classList.contains("avatar-bootstrap-bubble") ? bubbleHeight : 0 } as DOMRect;
    });
    (window as Window & { companionAvatar?: { openConsole(): void } }).companionAvatar!.openConsole = vi.fn();
    useBootstrapStatusMock.mockReturnValue({ status: "blocked", reason: "uninitialized", retry: vi.fn() });
    act(() => { root.render(<AvatarApp />); });
    const stage = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange(bounds: { x: number; y: number; width: number; height: number }): void;
    };
    act(() => { stage.onPetBoundsChange({ x: 90, y: 32, width: 270, height: 488 }); });
    const bubble = container.querySelector<HTMLElement>(".avatar-bootstrap-bubble")!;
    expect(Number.parseFloat(bubble.style.top) + bubbleHeight).toBeLessThanOrEqual(502);
    expect(bubble.querySelector("button")?.textContent).toBe("打开 console");
    const firstTop = Number.parseFloat(bubble.style.top);
    bubbleHeight = 230;
    act(() => { notifyResize(); });
    expect(Number.parseFloat(bubble.style.top)).toBe(firstTop - 40);
    expect(Number.parseFloat(bubble.style.top) + bubbleHeight).toBeLessThanOrEqual(502);
  });

  it("bootstrap 鼠标只在透明空白区域移动时，不会收回穿透挡住桌面", () => {
    const hoverEnter = vi.fn();
    (window as Window & { companionAvatar?: { hoverEnter(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 12,
          clientY: 18
        })
      );
    });

    expect(hoverEnter).not.toHaveBeenCalled();
  });

  it("bootstrap 鼠标从交互区移回透明空白区域时，会恢复穿透", () => {
    const openConsole = vi.fn();
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void; hoverLeave(): void } })
      .companionAvatar!.openConsole = openConsole;
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void; hoverLeave(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    (window as Window & { companionAvatar?: { openConsole(): void; hoverEnter(): void; hoverLeave(): void } })
      .companionAvatar!.hoverLeave = hoverLeave;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const openConsoleButton = Array.from(container.querySelectorAll("button"))
      .find(button => button.textContent === "打开 console");
    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      openConsoleButton?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 8,
          clientY: 10
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(hoverLeave).toHaveBeenCalledOnce();
  });

  it("bootstrap 鼠标移动到人物小热区内时会收回穿透，保证桌宠可以拖动", () => {
    const hoverEnter = vi.fn();
    (window as Window & { companionAvatar?: { hoverEnter(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 100,
        y: 120,
        width: 200,
        height: 400
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 180,
          clientY: 220
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
  });

  it("bootstrap 鼠标移动到缩放框外沿时会收回穿透，保证窗口可以缩放", () => {
    const hoverEnter = vi.fn();
    (window as Window & { companionAvatar?: { hoverEnter(): void } })
      .companionAvatar!.hoverEnter = hoverEnter;
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 100,
        y: 120,
        width: 200,
        height: 400
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 90,
          clientY: 112
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
  });

  it("bootstrap 仍在加载时展示桌宠资源和检查中气泡，并且不会启动 avatar runtime", () => {
    useBootstrapStatusMock.mockReturnValue({
      status: "loading",
      reason: null,
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    expect(container.querySelector('[data-window="avatar"]')).toBeTruthy();
    expect(container.querySelector(".avatar-stage-stub")).toBeTruthy();
    expect(container.textContent).toContain("正在检查初始化状态");
    expect(container.textContent).not.toContain("Bootstrap blocked");
    expect(useAvatarRuntimeMock).not.toHaveBeenCalled();
  });

  it("bootstrap 阶段缩放提示框只围绕人物边界显示", () => {
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 100,
        y: 120,
        width: 200,
        height: 400
      });
    });

    expect(avatarResizeFrameRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        bounds: {
          x: 100,
          y: 120,
          width: 200,
          height: 400
        },
        visible: true
      })
    );
  });
});
