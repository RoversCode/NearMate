// @vitest-environment jsdom

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AvatarStage } from "./AvatarStage";
import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";

const applicationInitMock = vi.fn();
const applicationDestroyMock = vi.fn();
const addChildMock = vi.fn();
const removeChildMock = vi.fn();
const assertLive2dModelSupportedMock = vi.fn();
const loadCubism5Live2dEngineMock = vi.fn();
const reportRendererDiagnosticMock = vi.fn();
const live2dModelFromMock = vi.fn();
const mockApplicationInstances: Array<{
  renderer: {
    width: number;
    height: number;
  };
  screen: {
    width: number;
    height: number;
  };
  ticker: unknown;
}> = [];

/**
 * resizeObserverCallbacks 保存当前测试环境里所有注册过的尺寸变化监听。
 * 组件级测试会手动触发这些回调，模拟 Electron 窗口 resize 后 host 尺寸真的发生变化。
 */
const resizeObserverCallbacks: Array<ResizeObserverCallback> = [];

/**
 * MockResizeObserver 为 jsdom 补齐 AvatarStage 尺寸监听测试所需的最小能力。
 * 核心入参是原生风格的 callback；输出是可被组件 effect 注册与清理的观察器实例。
 */
class MockResizeObserver {
  /**
   * callback 保存这次观察器在尺寸变化时需要执行的同步逻辑。
   */
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    resizeObserverCallbacks.push(callback);
  }

  observe(): void {}

  disconnect(): void {
    const index = resizeObserverCallbacks.indexOf(this.callback);

    if (index >= 0) {
      resizeObserverCallbacks.splice(index, 1);
    }
  }
}

vi.mock("pixi.js", () => ({
  Application: class MockApplication {
    renderer = {
      width: 840,
      height: 1040
    };

    screen = {
      width: 420,
      height: 520
    };

    canvas = document.createElement("canvas");

    ticker = {};

    stage = {
      addChild: addChildMock,
      removeChild: removeChildMock
    };

    constructor() {
      mockApplicationInstances.push(this);
    }

    async init(options: unknown) {
      applicationInitMock(options);
    }

    destroy(...args: unknown[]) {
      applicationDestroyMock(...args);
    }
  }
}));

vi.mock("../lib/live2dRuntime", () => ({
  assertLive2dModelSupported: (...args: unknown[]) =>
    assertLive2dModelSupportedMock(...args),
  loadCubism5Live2dEngine: (...args: unknown[]) =>
    loadCubism5Live2dEngineMock(...args)
}));

vi.mock("../lib/rendererDiagnostics", async importOriginal => {
  const actual = await importOriginal<typeof import("../lib/rendererDiagnostics")>();

  return {
    ...actual,
    reportRendererDiagnostic: (...args: unknown[]) =>
      reportRendererDiagnosticMock(...args)
  };
});

describe("AvatarStage", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    resizeObserverCallbacks.length = 0;
    applicationInitMock.mockReset();
    applicationDestroyMock.mockReset();
    addChildMock.mockReset();
    removeChildMock.mockReset();
    assertLive2dModelSupportedMock.mockReset();
    loadCubism5Live2dEngineMock.mockReset();
    reportRendererDiagnosticMock.mockReset();
    live2dModelFromMock.mockReset();
    mockApplicationInstances.length = 0;
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
    vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        x: 0,
        y: 0,
        top: 0,
        left: 0,
        right: 420,
        bottom: 520,
        width: 420,
        height: 520,
        toJSON: () => ({
          x: 0,
          y: 0,
          top: 0,
          left: 0,
          right: 420,
          bottom: 520,
          width: 420,
          height: 520
        })
      } as DOMRect);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("首个真实模型 ready 后，会补挂 resize 重布局监听，并在后续尺寸变化时继续使用 Hiyori 档案", async () => {
    const onPetBoundsChange = vi.fn();
    const onModelReady = vi.fn();
    const anchorSet = vi.fn();
    const scaleSet = vi.fn();
    const positionSet = vi.fn();
    const destroyMock = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: anchorSet
      },
      scale: {
        set: scaleSet
      },
      position: {
        set: positionSet
      },
      textures: [],
      destroy: destroyMock
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock.mockResolvedValue(model)
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          presentationProfile: getAvatarPresentationProfile("Hiyori"),
          onPetBoundsChange,
          onModelReady
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onModelReady).toHaveBeenCalledWith("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(scaleSet).toHaveBeenCalledTimes(2);
    expect(positionSet).toHaveBeenCalledTimes(2);
    expect(onPetBoundsChange).toHaveBeenCalledTimes(2);
    expect(onPetBoundsChange).toHaveBeenNthCalledWith(1, {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
    expect(onPetBoundsChange).toHaveBeenNthCalledWith(2, {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
    expect(mockApplicationInstances).toHaveLength(1);
    expect(resizeObserverCallbacks).toHaveLength(1);

    /**
     * 模拟 Electron 窗口已经完成一次真实 resize：
     * 先把 Pixi screen 切到更大的尺寸，再手动触发 ResizeObserver 回调。
     * 如果监听没有在首个模型 ready 后补挂，这里不会出现第二次 scale / position / petBounds 更新。
     */
    mockApplicationInstances[0]!.screen.width = 560;
    mockApplicationInstances[0]!.screen.height = 760;

    act(() => {
      resizeObserverCallbacks[0]?.([], {} as ResizeObserver);
    });

    expect(scaleSet).toHaveBeenCalledTimes(3);
    expect(positionSet).toHaveBeenCalledTimes(3);
    expect(scaleSet).toHaveBeenLastCalledWith(
      expect.closeTo(1.2413333333333332, 12)
    );
    expect(positionSet).toHaveBeenLastCalledWith(
      expect.closeTo(324.8, 12),
      752.4
    );
    expect(onPetBoundsChange).toHaveBeenCalledTimes(3);
    expect(onPetBoundsChange).toHaveBeenNthCalledWith(3, {
      x: 176,
      y: 52,
      width: 238,
      height: 700
    });
  });

  it("真实模型提交成功后会上报当前 Live2D 模型与 Pixi ticker", async () => {
    const onActiveLive2dModelChanged = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: vi.fn()
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock.mockResolvedValue(model);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Haru/Haru.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onActiveLive2dModelChanged
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onActiveLive2dModelChanged).toHaveBeenCalledWith({
      model,
      ticker: mockApplicationInstances[0]?.ticker,
      modelPath: "/live2d/resources/Haru/Haru.model3.json"
    });
  });

  it("模型切换和组件卸载时会先清空活跃 Live2D 模型绑定", async () => {
    const onActiveLive2dModelChanged = vi.fn();
    const firstModel = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: vi.fn()
    };
    const secondModel = {
      internalModel: {
        width: 320,
        height: 640,
        originalWidth: 320,
        originalHeight: 640
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: vi.fn()
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock
      .mockResolvedValueOnce(firstModel)
      .mockResolvedValueOnce(secondModel);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Haru/Haru.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onActiveLive2dModelChanged
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Natori/Natori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onActiveLive2dModelChanged
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.unmount();
    });

    expect(onActiveLive2dModelChanged.mock.calls).toEqual([
      [
        {
          model: firstModel,
          ticker: mockApplicationInstances[0]?.ticker,
          modelPath: "/live2d/resources/Haru/Haru.model3.json"
        }
      ],
      [null],
      [
        {
          model: secondModel,
          ticker: mockApplicationInstances[0]?.ticker,
          modelPath: "/live2d/resources/Natori/Natori.model3.json"
        }
      ],
      [null]
    ]);
  });

  it("当父层拒绝提交新模型时，会继续保留旧模型并且不会把新模型挂到舞台", async () => {
    const committedModelDestroyMock = vi.fn();
    const pendingModelDestroyMock = vi.fn();
    const committedModel = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: committedModelDestroyMock
    };
    const pendingModel = {
      internalModel: {
        width: 320,
        height: 640,
        originalWidth: 320,
        originalHeight: 640
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: pendingModelDestroyMock
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock
      .mockResolvedValueOnce(committedModel)
      .mockResolvedValueOnce(pendingModel);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Haru/Haru.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(addChildMock).toHaveBeenNthCalledWith(1, committedModel);

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Natori/Natori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockResolvedValue(false)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(removeChildMock).not.toHaveBeenCalled();
    expect(committedModelDestroyMock).not.toHaveBeenCalled();
    expect(pendingModelDestroyMock).toHaveBeenCalledTimes(1);
  });

  it("当 onModelReady 直接 reject 时，会销毁临时新模型并保留旧模型继续展示", async () => {
    const committedModelDestroyMock = vi.fn();
    const rejectedModelDestroyMock = vi.fn();
    const committedModel = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: committedModelDestroyMock
    };
    const rejectedModel = {
      internalModel: {
        width: 320,
        height: 640,
        originalWidth: 320,
        originalHeight: 640
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: rejectedModelDestroyMock
    };
    const onModelLoadFailed = vi.fn();

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock
      .mockResolvedValueOnce(committedModel)
      .mockResolvedValueOnce(rejectedModel);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Haru/Haru.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(addChildMock).toHaveBeenNthCalledWith(1, committedModel);

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Natori/Natori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockRejectedValue(new Error("persist failed")),
          onModelLoadFailed
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    /**
     * 这里锁住 reject 分支的资源回收。
     * 如果异常直接落入 catch 但没有销毁临时模型，就会把未提交的新实例遗留在内存里。
     */
    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(removeChildMock).not.toHaveBeenCalled();
    expect(committedModelDestroyMock).not.toHaveBeenCalled();
    expect(rejectedModelDestroyMock).toHaveBeenCalledTimes(1);
    expect(onModelLoadFailed).toHaveBeenCalledWith(
      "/live2d/resources/Natori/Natori.model3.json",
      expect.any(Error)
    );
  });

  it("当切换中的 effect 已 cleanup 后才收到 onModelReady reject，也会销毁临时新模型", async () => {
    const committedModelDestroyMock = vi.fn();
    const lateRejectedModelDestroyMock = vi.fn();
    const committedPath = "/live2d/resources/Haru/Haru.model3.json";
    const pendingPath = "/live2d/resources/Natori/Natori.model3.json";
    let rejectPendingReady: ((reason?: unknown) => void) | null = null;
    const pendingReadyPromise = new Promise<boolean>((_, reject) => {
      rejectPendingReady = reject;
    });
    const committedModel = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: committedModelDestroyMock
    };
    const lateRejectedModel = {
      internalModel: {
        width: 320,
        height: 640,
        originalWidth: 320,
        originalHeight: 640
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: lateRejectedModelDestroyMock
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock
      .mockResolvedValueOnce(committedModel)
      .mockResolvedValueOnce(lateRejectedModel);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: committedPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: pendingPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn(() => pendingReadyPromise)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: committedPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      rejectPendingReady?.(new Error("late reject"));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    /**
     * 这里专门锁住“cleanup 先发生、reject 后到达”的竞态。
     * 如果 cleanup 没有提前销毁临时模型，这个分支就会把 lateRejectedModel 泄漏在内存里。
     */
    expect(live2dModelFromMock).toHaveBeenCalledTimes(2);
    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(removeChildMock).not.toHaveBeenCalled();
    expect(committedModelDestroyMock).not.toHaveBeenCalled();
    expect(lateRejectedModelDestroyMock).toHaveBeenCalledTimes(1);
  });

  it("当切换失败后重新传回已显示旧模型的同一路径时，不会重新加载旧模型文件", async () => {
    const committedModelDestroyMock = vi.fn();
    const vetoedModelDestroyMock = vi.fn();
    const committedModel = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: committedModelDestroyMock
    };
    const vetoedModel = {
      internalModel: {
        width: 320,
        height: 640,
        originalWidth: 320,
        originalHeight: 640
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: vi.fn()
      },
      position: {
        set: vi.fn()
      },
      textures: [],
      destroy: vetoedModelDestroyMock
    };
    const acceptedReadyMock = vi.fn().mockResolvedValue(true);
    const vetoedReadyMock = vi.fn().mockResolvedValue(false);
    const committedPath = "/live2d/resources/Haru/Haru.model3.json";
    const vetoedPath = "/live2d/resources/Natori/Natori.model3.json";

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock
      .mockResolvedValueOnce(committedModel)
      .mockResolvedValueOnce(vetoedModel);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: committedPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: acceptedReadyMock
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: vetoedPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: vetoedReadyMock
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: committedPath,
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          onModelReady: acceptedReadyMock
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    /**
     * 这里锁住“回到已显示旧路径只做重布局，不重复走文件加载”这一规格。
     * 如果组件重新把 A 当成新模型加载，from 调用次数会变成 3，且旧实例可能被错误替换。
     */
    expect(live2dModelFromMock).toHaveBeenCalledTimes(2);
    expect(live2dModelFromMock).toHaveBeenNthCalledWith(
      1,
      committedPath,
      expect.objectContaining({
        ticker: mockApplicationInstances[0]?.ticker
      })
    );
    expect(live2dModelFromMock).toHaveBeenNthCalledWith(
      2,
      vetoedPath,
      expect.objectContaining({
        ticker: mockApplicationInstances[0]?.ticker
      })
    );
    expect(addChildMock).toHaveBeenCalledTimes(1);
    expect(addChildMock).toHaveBeenCalledWith(committedModel);
    expect(removeChildMock).not.toHaveBeenCalled();
    expect(committedModelDestroyMock).not.toHaveBeenCalled();
    expect(vetoedModelDestroyMock).toHaveBeenCalledTimes(1);
  });

  it("仅切换展示档案而模型路径不变时，不会重新加载模型文件，但会走现有重布局同步", async () => {
    const scaleSet = vi.fn();
    const positionSet = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600,
        originalWidth: 300,
        originalHeight: 600
      },
      anchor: {
        set: vi.fn()
      },
      scale: {
        set: scaleSet
      },
      position: {
        set: positionSet
      },
      textures: [],
      destroy: vi.fn()
    };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    live2dModelFromMock.mockResolvedValue(model);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: live2dModelFromMock
      }
    });

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          presentationProfile: getAvatarPresentationProfile("Haru"),
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    const scaleCallsAfterInitialReady = scaleSet.mock.calls.length;
    const positionCallsAfterInitialReady = positionSet.mock.calls.length;

    act(() => {
      root.render(
        createElement(AvatarStage, {
          modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          assistantStateLabel: "待机中",
          controlStatusLabel: "可交互",
          mediaStatusLabel: "可缩放",
          onAvatarTap: () => undefined,
          mode: "pet",
          presentationProfile: getAvatarPresentationProfile("Hiyori"),
          onModelReady: vi.fn().mockResolvedValue(true)
        })
      );
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(live2dModelFromMock).toHaveBeenCalledTimes(1);
    expect(scaleSet.mock.calls.length).toBeGreaterThan(scaleCallsAfterInitialReady);
    expect(positionSet.mock.calls.length).toBeGreaterThan(positionCallsAfterInitialReady);
  });
});
