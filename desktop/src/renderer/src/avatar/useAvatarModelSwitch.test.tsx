// @vitest-environment jsdom

import { act, createElement, useRef } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CompanionAvatarBridge } from "./avatarBridge";
import type { AvatarRuntime } from "./useAvatarRuntime";
import type { Live2dModelSwitchRequest } from "../../../shared/live2dModelTypes";
import { useAvatarModelSwitch } from "./useAvatarModelSwitch";
import { createInitialCompanionStatusState, projectCompanionStatus } from "../companion/companionStatus";

const modelCatalog = [
  {
    id: "Haru",
    label: "Haru",
    modelPath: "/live2d/resources/Haru/Haru.model3.json"
  },
  {
    id: "Hiyori",
    label: "Hiyori",
    modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
  }
];

const settingsFixture = {
  selected_live2d_model_id: "Haru",
  show_speech_bubbles: true,
};

type ModelSwitchHookResult = ReturnType<typeof useAvatarModelSwitch>;

function createBridge() {
  const listeners: Array<(request: Live2dModelSwitchRequest) => void> = [];
  const bridge = {
    applyPresentationProfile: vi.fn(),
    endDrag: vi.fn(),
    endResize: vi.fn(),
    hoverEnter: vi.fn(),
    hoverLeave: vi.fn(),
    moveDrag: vi.fn(),
    moveResize: vi.fn(),
    onModelSwitchRequested: vi.fn((listener: (request: Live2dModelSwitchRequest) => void) => {
      listeners.push(listener);
      return () => undefined;
    }),
    openPanel: vi.fn(),
    reportDiagnostic: vi.fn(),
    reportModelSwitchState: vi.fn(),
    runtime: {
      controlGateway: {
        httpBaseUrl: "http://127.0.0.1:8765",
        websocketUrl: "ws://127.0.0.1:8765/ws/control"
      },
      live2d: {
        defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
        catalog: modelCatalog
      }
    },
    startDrag: vi.fn(),
    startResize: vi.fn()
  } satisfies CompanionAvatarBridge;

  return {
    bridge,
    requestSwitch(modelId: string, requestId?: string) {
      listeners.at(-1)?.({ modelId, ...(requestId === undefined ? {} : { requestId }) });
    }
  };
}

function createRuntime(overrides: Partial<AvatarRuntime> = {}): AvatarRuntime {
  return {
    companionStatus: projectCompanionStatus(createInitialCompanionStatusState(), []),
    assistantBubble: null,
    bottomBubble: null,
    activitySnapshot: { revision: 0, runtime_id: "", active: [], recent: [] },
    activeActivityCount: 0,
    timedReminderSnapshot: { revision: 0, runtime_id: "", active: [], recent: [] },
    ringingReminders: [],
    requestingReminderIds: new Set(),
    reminderDismissError: null,
    localListeningHintActive: false,
    chatMode: null,
    pendingInteraction: null,
    settings: settingsFixture,
    settingsHydrated: true,
    requestDismissReminders: vi.fn().mockResolvedValue(undefined),
    updateSettings: vi.fn().mockResolvedValue(undefined),
    ...overrides,
    reflectionStatus: overrides.reflectionStatus ?? null
  };
}

type TestHarnessProps = {
  avatarBridge: CompanionAvatarBridge;
  avatarRuntime: AvatarRuntime;
  committedPresentationModelId: string | null;
  committedSelectedModelId: string | undefined;
  onHookResult(result: ModelSwitchHookResult): void;
  pendingModelId: string | null;
  rememberedModelUnavailable?: boolean;
  resolvedModelPath: string;
  setPendingModelId(modelId: string | null): void;
  targetModelId: string | undefined | null;
};

function TestHarness({
  avatarBridge,
  avatarRuntime,
  committedPresentationModelId,
  committedSelectedModelId,
  onHookResult,
  pendingModelId,
  rememberedModelUnavailable = false,
  resolvedModelPath,
  setPendingModelId,
  targetModelId
}: TestHarnessProps) {
  const appliedPresentationProfileModelIdRef = useRef<string | null>(null);
  const hookResult = useAvatarModelSwitch({
    appliedPresentationProfileModelIdRef,
    avatarBridge,
    avatarRuntime,
    committedPresentationModelId,
    committedSelectedModelId,
    modelCatalog,
    pendingModelId,
    rememberedModelUnavailable,
    resolvedModelPath,
    setPendingModelId,
    targetModelId
  });

  onHookResult(hookResult);
  return null;
}

describe("useAvatarModelSwitch", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("收到有效切换请求时进入 switching，并用同步 pending 阻止同拍第二条请求", () => {
    const { bridge, requestSwitch } = createBridge();
    const setPendingModelId = vi.fn();
    let hookResult: ModelSwitchHookResult | null = null;

    act(() => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          avatarRuntime: createRuntime(),
          committedPresentationModelId: "Haru",
          committedSelectedModelId: "Haru",
          onHookResult: result => {
            hookResult = result;
          },
          pendingModelId: null,
          resolvedModelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          setPendingModelId,
          targetModelId: "Hiyori"
        })
      );
    });

    act(() => {
      requestSwitch("Hiyori");
      requestSwitch("Haru");
    });

    expect(hookResult).not.toBeNull();
    expect(setPendingModelId).toHaveBeenCalledOnce();
    expect(setPendingModelId).toHaveBeenCalledWith("Hiyori");
    expect(bridge.reportModelSwitchState).toHaveBeenNthCalledWith(1, {
      status: "switching",
      modelId: "Hiyori"
    });
    expect(bridge.reportModelSwitchState).toHaveBeenNthCalledWith(2, {
      status: "failed", modelId: "Haru", reason: "另一个人物正在切换，请完成后重试。"
    });
  });

  it("设置尚未水合时明确拒绝切换，人物面板能够解锁重试", () => {
    const { bridge, requestSwitch } = createBridge();
    const setPendingModelId = vi.fn();
    act(() => root.render(createElement(TestHarness, {
      avatarBridge: bridge,
      avatarRuntime: createRuntime({ settingsHydrated: false }),
      committedPresentationModelId: "Haru", committedSelectedModelId: "Haru",
      onHookResult: () => undefined, pendingModelId: null,
      resolvedModelPath: "/live2d/resources/Haru/Haru.model3.json",
      setPendingModelId, targetModelId: "Haru"
    })));
    act(() => requestSwitch("Hiyori"));
    expect(setPendingModelId).not.toHaveBeenCalled();
    expect(bridge.reportModelSwitchState).toHaveBeenCalledWith({
      status: "failed", modelId: "Hiyori", reason: "人物设置正在加载，请稍后重试。"
    });
  });

  it("新请求回执保留各自编号，同拍拒绝不会覆盖已接纳的持久化请求", async () => {
    const { bridge, requestSwitch } = createBridge();
    const runtime = createRuntime();
    let result: ModelSwitchHookResult | null = null;
    act(() => root.render(createElement(TestHarness, {
      avatarBridge: bridge, avatarRuntime: runtime, committedPresentationModelId: "Haru", committedSelectedModelId: "Haru",
      onHookResult: value => { result = value; }, pendingModelId: null, setPendingModelId: vi.fn(),
      resolvedModelPath: "/live2d/resources/Hiyori/Hiyori.model3.json", targetModelId: "Hiyori"
    })));
    act(() => {
      requestSwitch("Hiyori", "accepted-request");
      requestSwitch("Hiyori", "rejected-request");
    });
    expect(bridge.reportModelSwitchState).toHaveBeenNthCalledWith(1, {
      status: "switching", modelId: "Hiyori", requestId: "accepted-request"
    });
    expect(bridge.reportModelSwitchState).toHaveBeenNthCalledWith(2, {
      status: "failed", modelId: "Hiyori", requestId: "rejected-request", reason: "另一个人物正在切换，请完成后重试。"
    });
    await act(async () => { await result!.handleModelReady("/live2d/resources/Hiyori/Hiyori.model3.json"); });
    expect(runtime.updateSettings).toHaveBeenCalledTimes(1);
    expect(bridge.reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded", modelId: "Hiyori", requestId: "accepted-request"
    });
  });

  it("切换目标不在可用目录中时说明原因，不让面板停在切换中", () => {
    const { bridge, requestSwitch } = createBridge();
    act(() => root.render(createElement(TestHarness, {
      avatarBridge: bridge, avatarRuntime: createRuntime(),
      committedPresentationModelId: "Haru", committedSelectedModelId: "Haru",
      onHookResult: () => undefined, pendingModelId: null,
      resolvedModelPath: "/live2d/resources/Haru/Haru.model3.json",
      setPendingModelId: vi.fn(), targetModelId: "Haru"
    })));
    act(() => requestSwitch("Ren"));
    expect(bridge.reportModelSwitchState).toHaveBeenCalledWith({
      status: "failed", modelId: "Ren", reason: "该人物资源当前不可用，请选择其他人物。"
    });
  });

  it("pending 模型 ready 后持久化设置、应用展示档案并上报成功", async () => {
    const { bridge } = createBridge();
    const updateSettings = vi.fn().mockResolvedValue(undefined);
    const setPendingModelId = vi.fn();
    let hookResult: ModelSwitchHookResult | null = null;

    await act(async () => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          avatarRuntime: createRuntime({
            updateSettings
          }),
          committedPresentationModelId: "Haru",
          committedSelectedModelId: "Haru",
          onHookResult: result => {
            hookResult = result;
          },
          pendingModelId: "Hiyori",
          resolvedModelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          setPendingModelId,
          targetModelId: "Hiyori"
        })
      );
    });

    let commitDecision: boolean | void = undefined;

    await act(async () => {
      commitDecision = await hookResult?.handleModelReady(
        "/live2d/resources/Hiyori/Hiyori.model3.json"
      );
    });

    expect(commitDecision).toBe(true);
    expect(updateSettings).toHaveBeenCalledWith({
      selected_live2d_model_id: "Hiyori"
    });
    expect(bridge.applyPresentationProfile).toHaveBeenLastCalledWith("Hiyori");
    expect(bridge.reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded",
      modelId: "Hiyori"
    });
    expect(setPendingModelId).not.toHaveBeenCalledWith(null);
  });

  it("保存失败时拒绝提交新模型并清理 pending", async () => {
    const { bridge } = createBridge();
    const updateSettings = vi.fn().mockRejectedValue(new Error("save failed"));
    const setPendingModelId = vi.fn();
    let hookResult: ModelSwitchHookResult | null = null;

    await act(async () => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          avatarRuntime: createRuntime({
            updateSettings
          }),
          committedPresentationModelId: "Haru",
          committedSelectedModelId: "Haru",
          onHookResult: result => {
            hookResult = result;
          },
          pendingModelId: "Hiyori",
          resolvedModelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          setPendingModelId,
          targetModelId: "Hiyori"
        })
      );
    });

    let commitDecision: boolean | void = undefined;

    await act(async () => {
      commitDecision = await hookResult?.handleModelReady(
        "/live2d/resources/Hiyori/Hiyori.model3.json"
      );
    });

    expect(commitDecision).toBe(false);
    expect(bridge.reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "failed",
      modelId: "Hiyori",
      reason: "人物保存失败，请稍后重试。"
    });
    expect(setPendingModelId).toHaveBeenCalledWith(null);
  });

  it("committed 设置追上 pending 后才清空 pending", () => {
    const { bridge } = createBridge();
    const setPendingModelId = vi.fn();

    act(() => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          avatarRuntime: createRuntime(),
          committedPresentationModelId: "Hiyori",
          committedSelectedModelId: "Hiyori",
          onHookResult: () => undefined,
          pendingModelId: "Hiyori",
          resolvedModelPath: "/live2d/resources/Hiyori/Hiyori.model3.json",
          setPendingModelId,
          targetModelId: "Hiyori"
        })
      );
    });

    expect(setPendingModelId).toHaveBeenCalledWith(null);
  });
});
