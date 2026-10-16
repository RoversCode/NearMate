// @vitest-environment jsdom
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TimedReminderCompact } from "../types/backendEvents";
import {
  AvatarApp, avatarDragSurfaceRenderSpy, avatarStageRenderSpy,
  avatarTimedReminderBubbleRenderSpy, container, root, useAvatarRuntimeMock,
  getLipSyncControllerMockState
} from "./AvatarApp.testHarness";

afterEach(() => vi.unstubAllGlobals());

/** 提供可以观察本地音频和窗口调用的 Avatar 环境。 */
function setup() {
  vi.useFakeTimers();
  const audioElements: HTMLAudioElement[] = [];
  vi.stubGlobal("Audio", vi.fn(function () {
    const audio = document.createElement("audio");
    audio.play = vi.fn(async () => undefined);
    audio.pause = vi.fn();
    audioElements.push(audio);
    return audio;
  }));
  const bridge = {
    reportDiagnostic: vi.fn(), runtime: {
      controlGateway: { httpBaseUrl: "http://127.0.0.1:8765", websocketUrl: "ws://127.0.0.1:8765/ws/control" },
      live2d: { defaultModelPath: "", catalog: [] }
    },
    hoverEnter: vi.fn(), hoverLeave: vi.fn(), startDrag: vi.fn(), moveDrag: vi.fn(),
    endDrag: vi.fn(), startResize: vi.fn(), moveResize: vi.fn(), endResize: vi.fn(),
    applyPresentationProfile: vi.fn(), openPanel: vi.fn(),
    reportModelSwitchState: vi.fn(), onModelSwitchRequested: vi.fn(() => () => undefined)
  };
  (window as Window & { companionAvatar?: unknown }).companionAvatar = bridge;
  const reminder: TimedReminderCompact = {
    reminder_id: "123e4567-e89b-42d3-a456-426614174020", content: "会议时间到了",
    state: "ringing", ring_count: 0,
    trigger_at: "2026-09-05T10:00:00+08:00", created_at: "2026-09-05T09:00:00+08:00",
    updated_at: "2026-09-05T10:00:00+08:00", finished_at: null, next_ring_at: null,
    finish_reason: null, can_modify: false, can_cancel: false, can_dismiss: true
  };
  const requestDismissReminders = vi.fn();
  const runtime = {
    timedReminderSnapshot: {
      runtime_id: "123e4567-e89b-42d3-a456-426614174021", revision: 0,
      active: [] as TimedReminderCompact[], recent: []
    },
    ringingReminders: [] as TimedReminderCompact[],
    requestingReminderIds: new Set<string>(), requestDismissReminders
  };
  useAvatarRuntimeMock.mockImplementation(() => runtime);
  act(() => root.render(<AvatarApp />));
  const stage = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
    onPetBoundsChange(bounds: { x: number; y: number; width: number; height: number }): void;
  };
  act(() => stage.onPetBoundsChange({ x: 120, y: 140, width: 160, height: 300 }));

  /** 发布一份新快照，保留组件实例以验证真实的提醒变化接线。 */
  function ring(): void {
    runtime.timedReminderSnapshot = { ...runtime.timedReminderSnapshot, revision: 1, active: [reminder] };
    runtime.ringingReminders = [reminder];
    act(() => root.render(<AvatarApp />));
  }
  return { audioElements, bridge, runtime, reminder, ring };
}

describe("Avatar 提醒反馈接线", () => {
  it("短反馈传给头像和气泡，500毫秒回位且不改变拖拽坐标或唇形音源", () => {
    const { ring, audioElements, bridge } = setup();
    const bounds = avatarDragSurfaceRenderSpy.mock.calls.at(-1)?.[0].bounds;
    ring();
    act(() => vi.advanceTimersByTime(1000));
    expect(container.querySelector(".avatar-shell--reminder-feedback")).not.toBeNull();
    expect(avatarTimedReminderBubbleRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ feedbackActive: true })
    );
    expect(audioElements).toHaveLength(1);
    act(() => vi.advanceTimersByTime(500));
    expect(container.querySelector(".avatar-shell--reminder-feedback")).toBeNull();
    expect(avatarDragSurfaceRenderSpy.mock.calls.at(-1)?.[0].bounds).toEqual(bounds);
    expect(bridge.startDrag).not.toHaveBeenCalled();
    expect(bridge.moveDrag).not.toHaveBeenCalled();
    expect(getLipSyncControllerMockState().instances[0].setAudioElement).not.toHaveBeenCalled();
    act(() => audioElements[0].dispatchEvent(new Event("ended")));
    expect(audioElements).toHaveLength(2);
  });

  it("点击知道了立即停止声音与动画，仍提交原有关闭命令", () => {
    const { ring, audioElements, runtime, reminder } = setup();
    ring();
    act(() => vi.advanceTimersByTime(1000));
    act(() => (container.querySelector(".avatar-timed-reminder-bubble-stub button") as HTMLButtonElement).click());
    expect(audioElements[0].pause).toHaveBeenCalledOnce();
    expect(container.querySelector(".avatar-shell--reminder-feedback")).toBeNull();
    expect(runtime.requestDismissReminders).toHaveBeenCalledWith(
      runtime.timedReminderSnapshot.runtime_id, [reminder.reminder_id]
    );
    act(() => audioElements[0].dispatchEvent(new Event("ended")));
    expect(audioElements).toHaveLength(1);
  });

  it("关闭请求中的快照保持静音，卸载取消未开始的反馈", () => {
    const { ring, audioElements, runtime, reminder } = setup();
    ring();
    runtime.requestingReminderIds = new Set([reminder.reminder_id]);
    act(() => root.render(<AvatarApp />));
    act(() => vi.advanceTimersByTime(1000));
    expect(audioElements).toHaveLength(0);
    runtime.requestingReminderIds = new Set();
    runtime.timedReminderSnapshot = {
      ...runtime.timedReminderSnapshot, revision: 2, active: [{ ...reminder, ring_count: 2 }]
    };
    act(() => root.render(<AvatarApp />));
    act(() => root.render(null));
    act(() => vi.advanceTimersByTime(1000));
    expect(audioElements).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("播放期间卸载会暂停声音，旧结束事件不再启动震动音", () => {
    const { ring, audioElements } = setup();
    ring();
    act(() => vi.advanceTimersByTime(1000));
    act(() => root.render(null));
    expect(audioElements[0].pause).toHaveBeenCalledOnce();
    audioElements[0].dispatchEvent(new Event("ended"));
    expect(audioElements).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("关闭一条提醒的请求尚未返回时，另一条新提醒仍然发声", () => {
    const { ring, audioElements, runtime, reminder } = setup();
    ring();
    runtime.requestingReminderIds = new Set([reminder.reminder_id]);
    act(() => root.render(<AvatarApp />));
    const secondReminder = { ...reminder, reminder_id: "123e4567-e89b-42d3-a456-426614174025" };
    runtime.timedReminderSnapshot = {
      ...runtime.timedReminderSnapshot, revision: 2, active: [reminder, secondReminder]
    };
    runtime.ringingReminders = [reminder, secondReminder];
    act(() => root.render(<AvatarApp />));
    act(() => vi.advanceTimersByTime(1000));
    expect(audioElements).toHaveLength(1);
    expect(audioElements[0].play).toHaveBeenCalledOnce();
    runtime.timedReminderSnapshot = { ...runtime.timedReminderSnapshot, revision: 3 };
    act(() => root.render(<AvatarApp />));
    expect(audioElements[0].pause).not.toHaveBeenCalled();
  });
});
