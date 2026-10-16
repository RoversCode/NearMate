// @vitest-environment jsdom
import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AvatarApp,
  avatarStageRenderSpy,
  container,
  getLipSyncControllerMockState,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";
describe("AvatarApp material drop", () => {
  it("全部不支持时显示警示气泡且不播放上传音效", async () => {
    vi.useFakeTimers();
    const play = vi.fn(async () => undefined);
    const audioConstructor = vi.fn(function (this: { currentTime: number; pause: () => void; play: () => Promise<void>; src: string }, src: string) {
      this.src = src;
      this.currentTime = 0;
      this.pause = vi.fn();
      this.play = play;
    });
    let materialDeliveryListener: ((payload: unknown) => void) | null = null;

    vi.stubGlobal("Audio", audioConstructor);
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startMaterialDrop: vi.fn(),
      endMaterialDrop: vi.fn(),
      onMaterialDeliveryFeedback: vi.fn((listener: (payload: unknown) => void) => {
        materialDeliveryListener = listener;
        return vi.fn();
      }),
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

    await act(async () => {
      materialDeliveryListener?.({
        operation: "add",
        kind: "warning",
        addedCount: 0,
        submittedCount: 2,
        text: "所选 2 项均为不支持的格式，已过滤"
      });
      await Promise.resolve();
    });

    expect(audioConstructor).not.toHaveBeenCalled();
    expect(play).not.toHaveBeenCalled();
    expect(container.textContent).toContain("所选 2 项均为不支持的格式，已过滤");

    act(() => {
      vi.advanceTimersByTime(2000);
    });

    expect(container.textContent).not.toContain("所选 2 项均为不支持的格式，已过滤");
  });

  it("拖拽文件给虚拟形象时提交材料并等待统一接收反馈，不触发唇形同步", async () => {
    vi.useFakeTimers();
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<unknown>>().mockResolvedValue({ accepted: true });
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    let materialDeliveryListener: ((payload: unknown) => void) | null = null;

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startMaterialDrop: vi.fn(),
      endMaterialDrop: vi.fn(),
      onMaterialDeliveryFeedback: vi.fn((listener: (payload: unknown) => void) => {
        materialDeliveryListener = listener;
        return vi.fn();
      }),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn(),
      addStagedMaterials,
      getPathForFile
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const controller = getLipSyncControllerMockState().instances[0];
    const avatarShell = container.querySelector(".avatar-shell") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        files: [file]
      }
    });

    await act(async () => {
      avatarShell.dispatchEvent(dropEvent);
      await Promise.resolve();
    });

    expect(addStagedMaterials).toHaveBeenCalledWith([
      {
        kind: "dragged_file",
        source: "drag_drop",
        display_name: "report.md",
        path: "G:\\docs\\report.md",
      }
    ]);
    expect(container.textContent).not.toContain("已添加 1 项");
    act(() => {
      materialDeliveryListener?.({
        operation: "add",
        kind: "success",
        addedCount: 1,
        submittedCount: 1,
        text: "已添加 1 项",
        sound: "upload"
      });
    });
    expect(container.textContent).toContain("已添加 1 项");
    expect(controller.setAudioElement).not.toHaveBeenCalled();
    expect(controller.setModelBinding).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(2000);
    });

    expect(container.textContent).not.toContain("已添加 1 项");
  });

  it("材料投递反馈跟随人物可见边界定位，而不是固定在窗口底部", async () => {
    vi.useFakeTimers();
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<unknown>>().mockResolvedValue({ accepted: true });
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    let materialDeliveryListener: ((payload: unknown) => void) | null = null;

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startMaterialDrop: vi.fn(),
      endMaterialDrop: vi.fn(),
      onMaterialDeliveryFeedback: vi.fn((listener: (payload: unknown) => void) => {
        materialDeliveryListener = listener;
        return vi.fn();
      }),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn(),
      addStagedMaterials,
      getPathForFile
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
        y: 40,
        width: 220,
        height: 620
      });
    });

    const avatarShell = container.querySelector(".avatar-shell") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        files: [file]
      }
    });

    await act(async () => {
      avatarShell.dispatchEvent(dropEvent);
      await Promise.resolve();
    });

    act(() => {
      materialDeliveryListener?.({
        operation: "add",
        kind: "success",
        addedCount: 1,
        submittedCount: 1,
        text: "已添加 1 项",
        sound: "upload"
      });
    });

    const toast = container.querySelector(".avatar-material-feedback-toast") as HTMLElement;
    expect(toast).toBeTruthy();
    expect(toast.classList.contains("avatar-material-feedback-toast-positioned")).toBe(true);
    expect(toast.style.top).toMatch(/px$/);
    expect(toast.style.left).toMatch(/px$/);
    expect(toast.style.bottom).toBe("auto");
  });

  it("外部材料拖入头像窗口时会先请求主进程收回穿透，投递后再释放", async () => {
    vi.useFakeTimers();
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<unknown>>().mockResolvedValue({ accepted: true });
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    const startMaterialDrop = vi.fn();
    const endMaterialDrop = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startMaterialDrop,
      endMaterialDrop,
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn(),
      addStagedMaterials,
      getPathForFile
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dragEnterEvent = new Event("dragenter", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dragEnterEvent, "dataTransfer", {
      value: {
        types: ["Files"],
        files: [file]
      }
    });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        types: ["Files"],
        files: [file]
      }
    });

    await act(async () => {
      avatarShell.dispatchEvent(dragEnterEvent);
      avatarShell.dispatchEvent(dropEvent);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(startMaterialDrop).toHaveBeenCalledOnce();
    expect(endMaterialDrop).toHaveBeenCalledOnce();
    expect(addStagedMaterials).toHaveBeenCalledOnce();
    expect(container.textContent).not.toContain("已添加 1 项");
  });

  it("拖拽进入头像窗口时不渲染大面积材料投递白框", () => {
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
      openPanel: vi.fn(),
      addStagedMaterials: vi.fn(),
      getPathForFile: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell") as HTMLElement;

    act(() => {
      avatarShell.dispatchEvent(new Event("dragover", { bubbles: true, cancelable: true }));
    });

    expect(container.querySelector(".avatar-material-feedback-toast")).toBeNull();
  });

  it("材料已提交后不依赖当前材料上下文刷新也显示上传成功反馈", async () => {
    vi.useFakeTimers();
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<unknown>>().mockResolvedValue({ accepted: true });
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    let materialDeliveryListener: ((payload: unknown) => void) | null = null;

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
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
          defaultModelPath: "",
          catalog: [],
        },
      },
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      onMaterialDeliveryFeedback: vi.fn((listener: (payload: unknown) => void) => {
        materialDeliveryListener = listener;
        return vi.fn();
      }),
      onModelSwitchRequested: vi.fn(() => () => undefined),
      reportModelSwitchState: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile: vi.fn(),
      openPanel: vi.fn(),
      addStagedMaterials,
      getPathForFile
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const avatarShell = container.querySelector(".avatar-shell") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        files: [file]
      }
    });

    await act(async () => {
      avatarShell.dispatchEvent(dropEvent);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(addStagedMaterials).toHaveBeenCalledOnce();
    expect(container.textContent).not.toContain("已添加 1 项");
    act(() => {
      materialDeliveryListener?.({
        operation: "add",
        kind: "success",
        addedCount: 1,
        submittedCount: 1,
        text: "已添加 1 项",
        sound: "upload"
      });
    });
    expect(container.textContent).toContain("已添加 1 项");
    expect(container.textContent).not.toContain("材料添加失败");
  });

});
