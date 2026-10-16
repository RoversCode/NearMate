// @vitest-environment jsdom
import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import {
  AvatarApp,
  avatarStageRenderSpy,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";
describe("AvatarApp model switching", () => {
  it("收到模型切换请求后，只有新模型 ready 才会持久化并上报 succeeded", async () => {
    const reportModelSwitchState = vi.fn();
    const updateSettings = vi.fn().mockResolvedValue(undefined);
    const applyPresentationProfile = vi.fn();
    const onModelSwitchRequestedHandlers: Array<(request: { modelId: string }) => void> = [];

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
      },
      settingsHydrated: true,
      updateSettings,
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
            },
            {
              id: "Natori",
              label: "Natori",
              modelPath: "/live2d/resources/Natori/Natori.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested(listener: (request: { modelId: string }) => void) {
        onModelSwitchRequestedHandlers.push(listener);
        return () => undefined;
      },
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Natori" });
    });

    expect(reportModelSwitchState).toHaveBeenCalledWith({
      status: "switching",
      modelId: "Natori"
    });
    expect(updateSettings).not.toHaveBeenCalled();

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      onModelReady?(modelPath: string): Promise<void>;
    };

    expect(latestAvatarStageProps.modelPath).toBe("/live2d/resources/Natori/Natori.model3.json");

    await act(async () => {
      await latestAvatarStageProps.onModelReady?.("/live2d/resources/Natori/Natori.model3.json");
    });

    expect(updateSettings).toHaveBeenCalledWith({
      selected_live2d_model_id: "Natori"
    });
    expect(applyPresentationProfile).toHaveBeenLastCalledWith("Natori");
    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded",
      modelId: "Natori"
    });
  });

  it("pending 切换期间新模型路径可以先切换，但舞台展示档案仍保持旧人物，直到设置写回成功后才切到新人物档案", async () => {
    const reportModelSwitchState = vi.fn();
    const updateSettings = vi.fn().mockResolvedValue(undefined);
    const onModelSwitchRequestedHandlers: Array<(request: { modelId: string }) => void> = [];
    const runtimeState = {
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
      },
      settingsHydrated: true,
      updateSettings: vi.fn(async (nextSettings: Partial<typeof runtimeState.settings>) => {
        await updateSettings(nextSettings);
        runtimeState.settings = { ...runtimeState.settings, ...nextSettings };
      }),
    };

    useAvatarRuntimeMock.mockImplementation(() => runtimeState);
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
            },
            {
              id: "Hiyori",
              label: "Hiyori",
              modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested(listener: (request: { modelId: string }) => void) {
        onModelSwitchRequestedHandlers.push(listener);
        return () => undefined;
      },
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
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

    const initialAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
    };

    expect(initialAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");
    expect(initialAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Haru")
    );

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Hiyori" });
    });

    const pendingAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
      onModelReady?(modelPath: string): Promise<void>;
    };

    expect(pendingAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(pendingAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Haru")
    );

    await act(async () => {
      await pendingAvatarStageProps.onModelReady?.("/live2d/resources/Hiyori/Hiyori.model3.json");
      root.render(<AvatarApp />);
    });

    const committedAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
    };

    expect(committedAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(committedAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Hiyori")
    );
    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded",
      modelId: "Hiyori"
    });
  });

  it("同一拍连续收到两个不同人物切换请求时，只接受第一条 pending 切换并只持久化第一条", async () => {
    const reportModelSwitchState = vi.fn();
    const updateSettings = vi.fn().mockResolvedValue(undefined);
    const applyPresentationProfile = vi.fn();
    const onModelSwitchRequestedHandlers: Array<(request: { modelId: string }) => void> = [];

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
      },
      settingsHydrated: true,
      updateSettings,
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
            },
            {
              id: "Hiyori",
              label: "Hiyori",
              modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
            },
            {
              id: "Natori",
              label: "Natori",
              modelPath: "/live2d/resources/Natori/Natori.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested(listener: (request: { modelId: string }) => void) {
        onModelSwitchRequestedHandlers.push(listener);
        return () => undefined;
      },
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Hiyori" });
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Natori" });
    });

    /**
     * 同一拍内第二条请求必须看到“第一条已经占住 pending”的最新真相源，
     * 否则它会把第一条待提交人物覆盖掉，导致真正 ready 与最终持久化错位。
     */
    expect(reportModelSwitchState).toHaveBeenCalledTimes(2);
    expect(reportModelSwitchState).toHaveBeenNthCalledWith(1, {
      status: "switching",
      modelId: "Hiyori"
    });
    expect(reportModelSwitchState).toHaveBeenNthCalledWith(2, {
      status: "failed", modelId: "Natori", reason: "另一个人物正在切换，请完成后重试。"
    });

    const pendingAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      onModelReady?(modelPath: string): Promise<boolean | void>;
    };

    expect(pendingAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");

    await act(async () => {
      await pendingAvatarStageProps.onModelReady?.("/live2d/resources/Hiyori/Hiyori.model3.json");
    });

    expect(updateSettings).toHaveBeenCalledWith({
      selected_live2d_model_id: "Hiyori"
    });
    expect(applyPresentationProfile).toHaveBeenLastCalledWith("Hiyori");
    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded",
      modelId: "Hiyori"
    });
  });

  it("切换成功后在 committed settings 真正追上前，不会因为 pending 过早清空而回跳到旧模型路径", async () => {
    const reportModelSwitchState = vi.fn();
    const applyPresentationProfile = vi.fn();
    const onModelSwitchRequestedHandlers: Array<(request: { modelId: string }) => void> = [];
    const runtimeState = {
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru"
      },
      settingsHydrated: true,
      updateSettings: vi.fn().mockResolvedValue(undefined),
    };

    useAvatarRuntimeMock.mockImplementation(() => runtimeState);
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
            },
            {
              id: "Hiyori",
              label: "Hiyori",
              modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested(listener: (request: { modelId: string }) => void) {
        onModelSwitchRequestedHandlers.push(listener);
        return () => undefined;
      },
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Hiyori" });
    });

    const pendingAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
      onModelReady?(modelPath: string): Promise<boolean | void>;
    };

    expect(pendingAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(pendingAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Haru")
    );

    await act(async () => {
      await pendingAvatarStageProps.onModelReady?.("/live2d/resources/Hiyori/Hiyori.model3.json");
      /**
       * 这里显式模拟“设置写回请求已经成功，但 runtime.settings 还没追上新人物”。
       * 如果成功路径过早清空 pending，这次重新渲染会短暂回跳到旧 Haru 路径。
       */
      root.render(<AvatarApp />);
    });

    const stillPendingAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
    };

    expect(stillPendingAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(stillPendingAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Haru")
    );

    runtimeState.settings = {
      ...runtimeState.settings,
      selected_live2d_model_id: "Hiyori",
    };

    await act(async () => {
      root.render(<AvatarApp />);
      await Promise.resolve();
    });

    const committedAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
    };

    expect(committedAvatarStageProps.modelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(committedAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Hiyori")
    );
    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "succeeded",
      modelId: "Hiyori"
    });
    expect(applyPresentationProfile).toHaveBeenLastCalledWith("Hiyori");
  });

  it("模型保存失败时，会显式拒绝提交新模型并上报 failed", async () => {
    const reportModelSwitchState = vi.fn();
    const updateSettings = vi.fn().mockRejectedValue(new Error("save failed"));
    const applyPresentationProfile = vi.fn();
    const onModelSwitchRequestedHandlers: Array<(request: { modelId: string }) => void> = [];

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru",
      },
      settingsHydrated: true,
      updateSettings,
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
            },
            {
              id: "Natori",
              label: "Natori",
              modelPath: "/live2d/resources/Natori/Natori.model3.json"
            }
          ],
        },
      },
      onModelSwitchRequested(listener: (request: { modelId: string }) => void) {
        onModelSwitchRequestedHandlers.push(listener);
        return () => undefined;
      },
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const initialAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
    };
    expect(initialAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Natori" });
    });

    const pendingAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      onModelReady?(modelPath: string): Promise<boolean | void>;
      onModelLoadFailed?(modelPath: string, error: unknown): void;
    };

    expect(pendingAvatarStageProps.modelPath).toBe("/live2d/resources/Natori/Natori.model3.json");

    /**
     * 这里先给出显式初值，避免 TypeScript 在异步 act 赋值前把它判断成未初始化变量。
     * 测试真正关心的是 ready 回调返回后它是否被覆盖成 false。
     */
    let commitDecision: boolean | void = undefined;

    await act(async () => {
      commitDecision = await pendingAvatarStageProps.onModelReady?.(
        "/live2d/resources/Natori/Natori.model3.json"
      );
    });

    expect(commitDecision).toBe(false);
    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "failed",
      modelId: "Natori",
      reason: "人物保存失败，请稍后重试。"
    });
    expect(applyPresentationProfile).not.toHaveBeenCalledWith("Natori");

    const revertedAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
    };
    expect(revertedAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");

    act(() => {
      onModelSwitchRequestedHandlers[0]?.({ modelId: "Natori" });
    });

    const loadFailureAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onModelLoadFailed?(modelPath: string, error: unknown): void;
    };

    act(() => {
      loadFailureAvatarStageProps.onModelLoadFailed?.(
        "/live2d/resources/Natori/Natori.model3.json",
        new Error("load failed")
      );
    });

    expect(reportModelSwitchState).toHaveBeenLastCalledWith({
      status: "failed",
      modelId: "Natori",
      reason: "人物资源加载失败。"
    });
    expect(applyPresentationProfile).not.toHaveBeenCalledWith("Natori");
  });

  it("上次记住的人物如果已不在当前可用目录中，会回退默认人物并上报明确失败原因", () => {
    const reportModelSwitchState = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Ren",
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
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
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
      modelPath: string | null;
      onModelLoadFailed?(modelPath: string, error: unknown): void;
    };

    expect(latestAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");

    act(() => {
      latestAvatarStageProps.onModelLoadFailed?.(
        "/live2d/resources/Haru/Haru.model3.json",
        new Error("unsupported model")
      );
    });

    expect(reportModelSwitchState).toHaveBeenCalledWith({
      status: "failed",
      modelId: "Ren",
      reason: "上次选择的人物当前不可用，已回退到默认人物。"
    });
  });

  it("失效人物回退到默认人物时，会使用真正展示人物自己的档案与 apply，但失败上报仍针对原失效 id", () => {
    const reportModelSwitchState = vi.fn();
    const applyPresentationProfile = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Hiyori",
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
      reportModelSwitchState,
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      startDrag: vi.fn(),
      moveDrag: vi.fn(),
      endDrag: vi.fn(),
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      modelPath: string | null;
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
    };

    expect(latestAvatarStageProps.modelPath).toBe("/live2d/resources/Haru/Haru.model3.json");
    expect(latestAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Haru")
    );
    expect(applyPresentationProfile).toHaveBeenCalledWith("Haru");
    expect(reportModelSwitchState).toHaveBeenCalledWith({
      status: "failed",
      modelId: "Hiyori",
      reason: "上次选择的人物当前不可用，已回退到默认人物。"
    });
  });

  it("启动时会在当前 committed 人物已知后尽早申请展示档案，后续重复 ready 不会重复 apply", async () => {
    const applyPresentationProfile = vi.fn();

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      settings: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Hiyori",
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
            },
            {
              id: "Hiyori",
              label: "Hiyori",
              modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
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
      startResize: vi.fn(),
      moveResize: vi.fn(),
      endResize: vi.fn(),
      applyPresentationProfile,
      openPanel: vi.fn()
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    const latestAvatarStageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      presentationProfile: ReturnType<typeof getAvatarPresentationProfile>;
      onModelReady?(modelPath: string): Promise<void>;
    };

    expect(latestAvatarStageProps.presentationProfile).toBe(
      getAvatarPresentationProfile("Hiyori")
    );
    expect(applyPresentationProfile).toHaveBeenCalledTimes(1);
    expect(applyPresentationProfile).toHaveBeenCalledWith("Hiyori");

    await act(async () => {
      await latestAvatarStageProps.onModelReady?.("/live2d/resources/Hiyori/Hiyori.model3.json");
      await latestAvatarStageProps.onModelReady?.("/live2d/resources/Hiyori/Hiyori.model3.json");
    });

    expect(applyPresentationProfile).toHaveBeenCalledTimes(1);
    expect(applyPresentationProfile).toHaveBeenCalledWith("Hiyori");
  });

});
