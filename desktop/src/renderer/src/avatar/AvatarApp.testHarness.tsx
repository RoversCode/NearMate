import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, vi } from "vitest";

export const useAvatarRuntimeMock = vi.fn();
export const avatarStageRenderSpy = vi.fn();
export const avatarResizeFrameRenderSpy = vi.fn();
export const avatarDragSurfaceRenderSpy = vi.fn();
export const avatarPanelButtonRenderSpy = vi.fn();
export const avatarAssistantBubbleRenderSpy = vi.fn();
export const avatarTimedReminderBubbleRenderSpy = vi.fn();
const useBootstrapStatusMock = vi.fn();
const lipSyncControllerMockState = vi.hoisted(() => ({
  instances: [] as Array<{
    destroy: ReturnType<typeof vi.fn<() => void>>;
    setAudioElement: ReturnType<typeof vi.fn<(element: HTMLMediaElement | null) => void>>;
    setModelBinding: ReturnType<typeof vi.fn<(binding: unknown) => void>>;
  }>
}));
const avatarActionControllerMockState = vi.hoisted(() => ({
  instances: [] as Array<{
    destroy: ReturnType<typeof vi.fn<() => void>>;
    setModelBinding: ReturnType<typeof vi.fn<(binding: unknown) => boolean>>;
    setState: ReturnType<typeof vi.fn<(state: string) => void>>;
    reactToTap: ReturnType<typeof vi.fn<() => void>>;
    playNextReaction: ReturnType<typeof vi.fn<() => void>>;
  }>
}));

/**
 * getLipSyncControllerMockState 暴露被 mock 的唇形同步控制器实例。
 *
 * @returns 当前测试文件内收集到的控制器实例列表。
 */
export function getLipSyncControllerMockState(): typeof lipSyncControllerMockState {
  return lipSyncControllerMockState;
}

/**
 * 提供动作控制器替身，测试 Avatar 接线而不启动真实 Cubism。
 *
 * @returns 当前测试创建的动作控制器实例。
 */
export function getAvatarActionControllerMockState(): typeof avatarActionControllerMockState {
  return avatarActionControllerMockState;
}

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
  AvatarDragSurface: (props: unknown) => {
    avatarDragSurfaceRenderSpy(props);
    return <div className="avatar-drag-surface-stub" data-avatar-interactive="true" />;
  }
}));

vi.mock("../components/AvatarResizeFrame", () => ({
  AvatarResizeFrame: (props: unknown) => {
    avatarResizeFrameRenderSpy(props);
    return <div className="avatar-resize-frame-stub" data-avatar-interactive="true" />;
  }
}));

vi.mock("../components/AvatarActionRail", () => ({
  AvatarActionRail: (props: {
    visible: boolean;
    petBounds: { x: number; y: number; width: number; height: number } | null;
    onOpenModels(): void;
    onOpenConsole(): void;
    onPlayReaction?(): void;
  }) => {
    avatarPanelButtonRenderSpy(props);

    if (!props.visible) {
      return null;
    }

    return (
      <div className="avatar-action-rail-stub" data-avatar-interactive="true">
        <button className="avatar-action-models-stub" type="button" onClick={props.onOpenModels} />
        <button className="avatar-action-console-stub" type="button" onClick={props.onOpenConsole} />
        {props.onPlayReaction ? (
          <button className="avatar-action-reaction-stub" type="button" onClick={props.onPlayReaction} />
        ) : null}
      </div>
    );
  }
}));

vi.mock("../components/AvatarSpeechBubble", () => ({
  AvatarSpeechBubble: ({ text, onClick }: { text: string; onClick?(): void }) => onClick ? (
    <button type="button" className="avatar-speech-bubble-stub" onClick={onClick}>{text}</button>
  ) : (
    <div className="avatar-speech-bubble-stub">{text}</div>
  )
}));

vi.mock("../components/AvatarAssistantBubble", () => ({
  AvatarAssistantBubble: (props: unknown) => {
    avatarAssistantBubbleRenderSpy(props);
    return <div className="avatar-assistant-bubble-stub" />;
  }
}));

vi.mock("../components/AvatarTimedReminderBubble", () => ({
  AvatarTimedReminderBubble: (props: {
    reminders: Array<{ content: string }>;
    onDismiss(): void;
  }) => {
    avatarTimedReminderBubbleRenderSpy(props);
    return (
      <section
        className="avatar-timed-reminder-bubble-stub"
        data-avatar-interactive="true"
      >
        {props.reminders.map(reminder => reminder.content).join("|")}
        <button type="button" onClick={props.onDismiss}>知道了</button>
      </section>
    );
  }
}));

vi.mock("../lib/live2dLipSync", () => ({
  Live2dLipSyncController: class Live2dLipSyncController {
    readonly destroy = vi.fn();
    readonly setAudioElement = vi.fn();
    readonly setModelBinding = vi.fn();

    constructor() {
      lipSyncControllerMockState.instances.push(this);
    }
  }
}));

vi.mock("../lib/deepSeekAvatarController", () => ({
  DeepSeekAvatarController: class DeepSeekAvatarController {
    readonly destroy = vi.fn();
    readonly setModelBinding = vi.fn((_binding: unknown) => false);
    readonly setState = vi.fn();
    readonly reactToTap = vi.fn();
    readonly playNextReaction = vi.fn();

    constructor() {
      avatarActionControllerMockState.instances.push(this);
    }
  }
}));

export { AvatarApp } from "./AvatarApp";

export let container: HTMLDivElement;
export let root: ReturnType<typeof createRoot>;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useAvatarRuntimeMock.mockReset();
  avatarStageRenderSpy.mockReset();
  avatarResizeFrameRenderSpy.mockReset();
  avatarDragSurfaceRenderSpy.mockReset();
  avatarPanelButtonRenderSpy.mockReset();
  avatarAssistantBubbleRenderSpy.mockReset();
  avatarTimedReminderBubbleRenderSpy.mockReset();
  lipSyncControllerMockState.instances.length = 0;
  avatarActionControllerMockState.instances.length = 0;
  useBootstrapStatusMock.mockReset();
  // 既有 AvatarApp 测试关注 ready 后的头像交互；默认放行 bootstrap，阻断态由专门的 bootstrap 测试覆盖。
  useBootstrapStatusMock.mockReturnValue({
    status: "ready",
    reason: null,
    retry: vi.fn()
  });
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  delete (window as Window & { companionAvatar?: unknown }).companionAvatar;
  vi.useRealTimers();
});
