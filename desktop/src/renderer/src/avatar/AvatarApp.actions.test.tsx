// @vitest-environment jsdom

import { act, StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { AvatarStageProps } from "../components/avatarStageTypes";
import type { Live2dLipSyncModelBinding } from "../lib/live2dLipSync";
import type { CompanionDisplayState } from "../companion/companionStatus";
import {
  AvatarApp,
  avatarDragSurfaceRenderSpy,
  avatarPanelButtonRenderSpy,
  avatarStageRenderSpy,
  container,
  getAvatarActionControllerMockState,
  getLipSyncControllerMockState,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";

const whalePath = "./live2d/resources/DeepSeekChibi/c_0120.model3.json";
const haruPath = "./live2d/resources/Haru/Haru.model3.json";

function prepareAvatar(strictMode = false) {
  const openChat = vi.fn();
  const hoverLeave = vi.fn();
  const runtime = {
    bottomBubble: null,
    assistantBubble: null,
    settings: { selected_live2d_model_id: "DeepSeekChibi", show_speech_bubbles: true },
    settingsHydrated: true,
    updateSettings: vi.fn(),
    chatMode: "collapsed",
    companionStatus: undefined as undefined | {
      state: CompanionDisplayState;
      label: string;
      description: string;
      wakeMode: "unknown";
      wakeLabel: string;
      manualToggleRevision: null;
    }
  };
  useAvatarRuntimeMock.mockImplementation(() => runtime);
  (window as Window & { companionAvatar?: unknown }).companionAvatar = {
    runtime: {
      controlGateway: { httpBaseUrl: "http://127.0.0.1:8765", websocketUrl: "ws://127.0.0.1:8765/ws/control" },
      live2d: {
        defaultModelPath: whalePath,
        catalog: [
          { id: "DeepSeekChibi", label: "大肥鱼（Q版）", modelPath: whalePath },
          { id: "Haru", label: "Haru", modelPath: haruPath }
        ]
      }
    },
    reportDiagnostic: vi.fn(),
    applyPresentationProfile: vi.fn(),
    onModelSwitchRequested: vi.fn(() => () => undefined),
    reportModelSwitchState: vi.fn(),
    hoverEnter: vi.fn(),
    hoverLeave,
    startDrag: vi.fn(),
    moveDrag: vi.fn(),
    endDrag: vi.fn(),
    startResize: vi.fn(),
    moveResize: vi.fn(),
    endResize: vi.fn(),
    openPanel: vi.fn(),
    openChat
  };
  act(() => root.render(strictMode ? <StrictMode><AvatarApp /></StrictMode> : <AvatarApp />));
  return { openChat, hoverLeave, runtime, controller: getAvatarActionControllerMockState().instances.at(-1)! };
}

function latestStageProps(): AvatarStageProps {
  return avatarStageRenderSpy.mock.calls.at(-1)?.[0] as AvatarStageProps;
}

function latestRailProps(): { onPlayReaction?: () => void } {
  return avatarPanelButtonRenderSpy.mock.calls.at(-1)?.[0] as { onPlayReaction?: () => void };
}

function modelBinding(modelPath: string): Live2dLipSyncModelBinding {
  return { model: {}, ticker: { add: vi.fn(), remove: vi.fn() }, modelPath };
}

describe("AvatarApp model actions", () => {
  it("开发模式重放挂载副作用后，真实模型仍能绑定口型与动作", () => {
    const { controller } = prepareAvatar(true);
    const whale = modelBinding(whalePath);
    controller.setModelBinding.mockReturnValueOnce(true);
    act(() => latestStageProps().onActiveLive2dModelChanged?.(whale));

    expect(controller.setModelBinding).toHaveBeenLastCalledWith(whale);
    expect(getLipSyncControllerMockState().instances.at(-1)!.setModelBinding).toHaveBeenLastCalledWith(whale);
    expect(latestRailProps().onPlayReaction).toBeTypeOf("function");
  });

  it("动作能力取自已提交的模型绑定，目标设置为 Q 模型也不会提前显示按钮", () => {
    const { controller } = prepareAvatar();
    expect(latestRailProps().onPlayReaction).toBeUndefined();

    const whale = modelBinding(whalePath);
    controller.setModelBinding.mockReturnValueOnce(true);
    act(() => latestStageProps().onActiveLive2dModelChanged?.(whale));
    expect(controller.setModelBinding).toHaveBeenLastCalledWith(whale);
    expect(getLipSyncControllerMockState().instances[0].setModelBinding).toHaveBeenLastCalledWith(whale);
    expect(latestRailProps().onPlayReaction).toBeTypeOf("function");
    act(() => latestRailProps().onPlayReaction?.());
    expect(controller.playNextReaction).toHaveBeenCalledOnce();

    act(() => latestStageProps().onActiveLive2dModelChanged?.(null));
    expect(controller.setModelBinding).toHaveBeenLastCalledWith(null);
    expect(latestRailProps().onPlayReaction).toBeUndefined();

    const haru = modelBinding(haruPath);
    act(() => latestStageProps().onActiveLive2dModelChanged?.(haru));
    expect(controller.setModelBinding).toHaveBeenLastCalledWith(haru);
    expect(latestRailProps().onPlayReaction).toBeUndefined();
  });

  it("转发权威角色状态，聊天展开和气泡隐藏不会停止状态动作", () => {
    const { runtime, controller } = prepareAvatar();
    expect(controller.setState).toHaveBeenLastCalledWith("syncing");
    for (const state of ["listening", "thinking", "responding", "working", "waiting_wake"] as const) {
      runtime.companionStatus = {
        state, label: "当前状态", description: "", wakeMode: "unknown", wakeLabel: "", manualToggleRevision: null
      };
      runtime.chatMode = "expanded";
      runtime.settings.show_speech_bubbles = false;
      act(() => root.render(<AvatarApp />));
      expect(controller.setState).toHaveBeenLastCalledWith(state);
    }
  });

  it("动作能力同步扩展鼠标命中区，解绑后第四按钮位置恢复穿透", () => {
    const { controller, hoverLeave } = prepareAvatar();
    controller.setModelBinding.mockReturnValueOnce(true);
    act(() => {
      latestStageProps().onActiveLive2dModelChanged?.(modelBinding(whalePath));
      latestStageProps().onPetBoundsChange?.({ x: 180, y: 120, width: 220, height: 500 });
    });
    const shell = container.querySelector(".avatar-shell")!;
    const movePointer = (x: number, y: number) => {
      act(() => shell.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: x, clientY: y })));
    };
    movePointer(280, 260);
    // 此点在第四按钮内，已越过三按钮框及其通行走廊。
    movePointer(440, 318);
    expect(hoverLeave).not.toHaveBeenCalled();

    act(() => latestStageProps().onActiveLive2dModelChanged?.(null));
    movePointer(440, 318);
    expect(hoverLeave).toHaveBeenCalledOnce();
  });

  it("轻点角色同时触发反应并保留聊天入口，卸载时释放动作控制器", () => {
    const { controller, openChat } = prepareAvatar();
    const dragSurface = avatarDragSurfaceRenderSpy.mock.calls.at(-1)?.[0] as { onTap?(): void };
    act(() => dragSurface.onTap?.());
    expect(controller.reactToTap).toHaveBeenCalledOnce();
    expect(openChat).toHaveBeenCalledOnce();

    act(() => root.unmount());
    expect(controller.destroy).toHaveBeenCalledOnce();
    expect(getLipSyncControllerMockState().instances[0].destroy).toHaveBeenCalledOnce();
  });
});
