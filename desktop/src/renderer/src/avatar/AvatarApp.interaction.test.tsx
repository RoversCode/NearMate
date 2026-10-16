// @vitest-environment jsdom
import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AvatarApp,
  avatarAssistantBubbleRenderSpy,
  avatarDragSurfaceRenderSpy,
  avatarPanelButtonRenderSpy,
  avatarResizeFrameRenderSpy,
  avatarStageRenderSpy,
  container,
  getLipSyncControllerMockState,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";
describe("AvatarApp interaction surface", () => {
  it("鼠标移动到真实交互热区时，会主动通知主进程收回鼠标穿透", () => {
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const resizeFrame = container.querySelector(".avatar-resize-frame-stub");
    expect(resizeFrame).toBeTruthy();

    act(() => {
      resizeFrame?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(hoverLeave).not.toHaveBeenCalled();
  });

  it("鼠标只是在透明空白区域移动时，不会错误收回穿透或显示按钮", () => {
    const hoverEnter = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell");
    expect(avatarShell).toBeTruthy();

    act(() => {
      avatarShell?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(hoverEnter).not.toHaveBeenCalled();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("把助手播放音频元素和当前 Live2D 模型绑定到唇形同步控制器", () => {
    let runtimeOptions:
      | {
          onAssistantPlaybackElementChanged?(element: HTMLMediaElement | null): void;
        }
      | undefined;

    useAvatarRuntimeMock.mockImplementation((_bridge, options) => {
      runtimeOptions = options;
      return {
        bottomBubble: null,
        assistantBubble: null,
        settings: {
          show_speech_bubbles: true,
          selected_live2d_model_id: "Haru"
        },
        settingsHydrated: true,
        updateSettings: vi.fn(),
      };
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
          catalog: [
            {
              id: "Haru",
              label: "Haru",
              modelPath: "/live2d/resources/Haru/Haru.model3.json"
            }
          ],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const controller = getLipSyncControllerMockState().instances[0];
    const audioElement = document.createElement("audio");
    const modelBinding = {
      model: {},
      ticker: {}
    };
    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onActiveLive2dModelChanged?(binding: unknown): void;
    };

    act(() => {
      runtimeOptions?.onAssistantPlaybackElementChanged?.(audioElement);
      latestAvatarStageProps.onActiveLive2dModelChanged?.(modelBinding);
      runtimeOptions?.onAssistantPlaybackElementChanged?.(null);
      latestAvatarStageProps.onActiveLive2dModelChanged?.(null);
    });

    expect(controller.setAudioElement).toHaveBeenNthCalledWith(1, audioElement);
    expect(controller.setModelBinding).toHaveBeenNthCalledWith(1, modelBinding);
    expect(controller.setAudioElement).toHaveBeenNthCalledWith(2, null);
    expect(controller.setModelBinding).toHaveBeenNthCalledWith(2, null);

    act(() => {
      root.unmount();
    });

    expect(controller.destroy).toHaveBeenCalledOnce();
  });

  it("鼠标移动到人物小热区内时，会收回穿透并显示按钮和缩放提示", () => {
    const hoverEnter = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(avatarPanelButtonRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        visible: true
      })
    );
    expect(avatarResizeFrameRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        bounds: {
          x: 180,
          y: 120,
          width: 220,
          height: 500
        },
        visible: true
      })
    );
  });

  it("鼠标移动到人物大外接区域但不在小热区时，不会收回穿透", () => {
    const hoverEnter = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 520
        })
      );
    });

    expect(hoverEnter).not.toHaveBeenCalled();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("拖拽层只接收人物小热区边界，完整人物边界仍交给气泡和缩放", () => {
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: {
        kind: "assistant_reply",
        text: "正在看你。",
        visibleText: "正在看你。",
        generationId: "generation-1",
        textFinal: false,
        progressUnitCount: 0,
        captionMsPerUnit: 220,
        ttlMs: null
      },
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    expect(avatarDragSurfaceRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        bounds: {
          x: 250,
          y: 210,
          width: 79,
          height: 170
        }
      })
    );
    expect(avatarAssistantBubbleRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        petBounds: {
          x: 180,
          y: 120,
          width: 220,
          height: 500
        }
      })
    );
    expect(avatarResizeFrameRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        bounds: {
          x: 180,
          y: 120,
          width: 220,
          height: 500
        }
      })
    );
  });

  it("人物可见边界变化时会向主进程上报，清空时也会同步 null", () => {
    const reportPetBounds = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      reportPetBounds,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(
        bounds: { x: number; y: number; width: number; height: number } | null
      ): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 120,
        y: 140,
        width: 160,
        height: 300
      });
      latestAvatarStageProps.onPetBoundsChange?.(null);
    });

    // null 表示当前没有可信人物身体边界，主进程必须同步清除锚点缓存。
    expect(reportPetBounds).toHaveBeenNthCalledWith(1, {
      x: 120,
      y: 140,
      width: 160,
      height: 300
    });
    expect(reportPetBounds).toHaveBeenNthCalledWith(2, null);
  });

  it("鼠标移动到人物外接矩形透明边缘时，不会收回穿透或显示按钮", () => {
    const hoverEnter = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 190,
          clientY: 260
        })
      );
    });

    expect(hoverEnter).not.toHaveBeenCalled();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("鼠标从人物移动到按钮通行走廊时，按钮保持可见且不会提前恢复穿透", () => {
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 405,
          clientY: 150
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(hoverLeave).not.toHaveBeenCalled();
    expect(avatarPanelButtonRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        visible: true
      })
    );
  });

  it("鼠标从核心人物区域移回外接矩形透明边缘时，会释放穿透但按钮进入一点五秒宽限期", () => {
    vi.useFakeTimers();
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 190,
          clientY: 260
        })
      );
    });

    expect(hoverLeave).toHaveBeenCalledOnce();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1499);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("鼠标离开真实交互区回到透明空白区域时，会释放穿透但按钮进入一点五秒宽限期", () => {
    vi.useFakeTimers();
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell");
    const resizeFrame = container.querySelector(".avatar-resize-frame-stub");
    expect(avatarShell).toBeTruthy();
    expect(resizeFrame).toBeTruthy();

    act(() => {
      resizeFrame?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(hoverEnter).toHaveBeenCalledOnce();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      avatarShell?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(hoverLeave).toHaveBeenCalledOnce();
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1500);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("宽限期内重新回到人物热区时，会取消旧隐藏计时并刷新下一轮一点五秒窗口", () => {
    vi.useFakeTimers();
    const hoverEnter = vi.fn();
    const hoverLeave = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter,
      hoverLeave,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 190,
          clientY: 260
        })
      );
      vi.advanceTimersByTime(1000);
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
      vi.advanceTimersByTime(500);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 190,
          clientY: 260
        })
      );
      vi.advanceTimersByTime(1499);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);
  });

  it("人物按钮打开 models，头像轻点打开 Chat", () => {
    vi.useFakeTimers();
    const openPanel = vi.fn();
    const openChat = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel,
      openChat
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };

    act(() => {
      latestAvatarStageProps.onPetBoundsChange?.({
        x: 180,
        y: 120,
        width: 220,
        height: 500
      });
    });

    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 280,
          clientY: 260
        })
      );
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 190,
          clientY: 260
        })
      );
      vi.advanceTimersByTime(1000);
    });

    const panelButton = container.querySelector(".avatar-action-models-stub");
    expect(panelButton).toBeTruthy();

    act(() => {
      panelButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      vi.advanceTimersByTime(500);
    });

    expect(openPanel).toHaveBeenCalledWith("models");
    expect(
      (avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { visible: boolean }).visible
    ).toBe(false);

    const dragSurfaceProps = avatarDragSurfaceRenderSpy.mock.calls.at(-1)?.[0] as {
      onTap?(): void;
    };
    act(() => dragSurfaceProps.onTap?.());
    expect(openChat).toHaveBeenCalledOnce();
  });

  it("缩放热区回调会转发到 bridge 的 startResize、moveResize、endResize", () => {
    const startResize = vi.fn();
    const moveResize = vi.fn();
    const endResize = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru",
      },
      settingsHydrated: true,
      updateSettings: vi.fn(),
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765",
        },
        live2d: {
          defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
          catalog: [
            {
              id: "Haru",
              label: "Haru",
              modelPath: "/live2d/resources/Haru/Haru.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize,
      moveResize,
      endResize,
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const resizeFrameProps = avatarResizeFrameRenderSpy.mock.calls.at(-1)?.[0] as {
      bounds: { x: number; y: number; width: number; height: number } | null;
      visible: boolean;
      onResizeStart?(direction: string, point: { screenX: number; screenY: number }): void;
      onResizeMove?(point: { screenX: number; screenY: number }): void;
      onResizeEnd?(): void;
    };

    expect(resizeFrameProps.bounds).toBeNull();
    expect(resizeFrameProps.visible).toBe(false);
    resizeFrameProps.onResizeStart?.("se", { screenX: 300, screenY: 400 });
    resizeFrameProps.onResizeMove?.({ screenX: 320, screenY: 430 });
    resizeFrameProps.onResizeEnd?.();

    expect(startResize).toHaveBeenCalledWith("se", { screenX: 300, screenY: 400 });
    expect(moveResize).toHaveBeenCalledWith({ screenX: 320, screenY: 430 });
    expect(endResize).toHaveBeenCalledOnce();
  });
});
