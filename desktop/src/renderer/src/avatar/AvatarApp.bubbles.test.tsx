// @vitest-environment jsdom
import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AvatarApp,
  avatarAssistantBubbleRenderSpy,
  avatarTimedReminderBubbleRenderSpy,
  avatarPanelButtonRenderSpy,
  avatarResizeFrameRenderSpy,
  avatarStageRenderSpy,
  container,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";
describe("AvatarApp bubbles and lip sync", () => {
  it("可以同时渲染底部用户气泡和头部助手气泡，并把人物边界交给助手气泡布局", () => {
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: {
        kind: "speech",
        text: "用户最终说的话",
        ttlMs: 3200
      },
      assistantBubble: {
        kind: "assistant_reply",
        text: "助手正在回复。",
        visibleText: "助手正在回复。",
        generationId: "generation-1",
        textFinal: false,
        progressUnitCount: 0,
        captionMsPerUnit: 220,
        ttlMs: null
      }
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
        x: 120,
        y: 140,
        width: 160,
        height: 300
      });
    });

    expect(container.textContent).toContain("用户最终说的话");
    expect(avatarAssistantBubbleRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        text: "助手正在回复。",
        petBounds: {
          x: 120,
          y: 140,
          width: 160,
          height: 300
        },
        anchor: expect.objectContaining({
          x: expect.any(Number),
          y: expect.any(Number)
        })
      })
    );
    expect(avatarPanelButtonRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        petBounds: {
          x: 120,
          y: 140,
          width: 160,
          height: 300
        }
      })
    );
    expect(avatarResizeFrameRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        bounds: {
          x: 120,
          y: 140,
          width: 160,
          height: 300
        }
      })
    );
  });

  it("Self-Reflection 状态与普通助手气泡意外并存时优先展示 Self-Reflection", () => {
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: {
        kind: "assistant_reply",
        text: "上一条 Entry 回复。",
        visibleText: "上一条 Entry 回复。",
        generationId: "generation-entry",
        textFinal: true,
        progressUnitCount: 10,
        captionMsPerUnit: 220,
        ttlMs: null
      },
      reflectionStatus: {
        kind: "reflection_status",
        mode: "thinking",
        text: "思考中",
        selfReflectionId: "self-reflection-1",
        episodeId: "episode-1",
        revision: 1,
        recoveryRequestId: "reflection-recovery-request",
        recoverySessionId: "session-reflection",
        recoveryGenerationId: "generation-recovery",
        phase: "typing",
        elapsedMs: 0
      },
      ringingReminders: [ringingReminder("123e4567-e89b-42d3-a456-426614174020", "该去面试了")]
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765"
        },
        live2d: {
          defaultModelPath: "",
          catalog: []
        }
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

    expect(avatarAssistantBubbleRenderSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ text: "思考中" })
    );
    expect(avatarTimedReminderBubbleRenderSpy).not.toHaveBeenCalled();
  });

  it("响铃气泡优先于普通助手正文，并按当前运行期和全部可见编号原子关闭", () => {
    const requestDismissReminders = vi.fn();
    const firstReminder = ringingReminder(
      "123e4567-e89b-42d3-a456-426614174020",
      "该去面试了"
    );
    const secondReminder = ringingReminder(
      "123e4567-e89b-42d3-a456-426614174021",
      "结束专注计时"
    );
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: {
        kind: "assistant_reply",
        text: "普通回复",
        visibleText: "普通回复",
        generationId: "generation-ordinary",
        textFinal: false,
        progressUnitCount: 0,
        captionMsPerUnit: 220,
        ttlMs: null
      },
      reflectionStatus: null,
      timedReminderSnapshot: {
        runtime_id: "00000000-0000-4000-8000-0000000000cc",
        revision: 3,
        active: [firstReminder, secondReminder],
        recent: []
      },
      ringingReminders: [firstReminder, secondReminder],
      requestingReminderIds: new Set<string>(),
      reminderDismissError: null,
      requestDismissReminders
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = createAvatarBridge();

    act(() => {
      root.render(<AvatarApp />);
    });

    expect(container.textContent).toContain("该去面试了|结束专注计时");
    expect(avatarAssistantBubbleRenderSpy).not.toHaveBeenCalled();
    act(() => {
      (container.querySelector(".avatar-timed-reminder-bubble-stub button") as HTMLButtonElement)
        .click();
    });
    expect(requestDismissReminders).toHaveBeenCalledWith(
      "00000000-0000-4000-8000-0000000000cc",
      [firstReminder.reminder_id, secondReminder.reminder_id]
    );
  });

  it("聊天折叠时用可点击气泡提示待答请求，展开后只保留聊天卡片", () => {
    const openBackgroundAgentInteraction = vi.fn();
    const requestId = "123e4567-e89b-42d3-a456-426614174000";
    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: {
        kind: "notice",
        text: "普通提醒",
        ttlMs: 3200
      },
      assistantBubble: null,
      chatMode: "collapsed",
      pendingInteraction: {
        requestId,
        kind: "permission"
      }
    });
    (window as Window & { companionAvatar?: unknown }).companionAvatar = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765"
        },
        live2d: {
          defaultModelPath: "",
          catalog: []
        }
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
      openBackgroundAgentInteraction
    };

    act(() => {
      root.render(<AvatarApp />);
    });

    expect(container.textContent).toContain("后台任务需要你的授权，点击处理");
    expect(container.textContent).not.toContain("普通提醒");
    act(() => {
      (container.querySelector(".avatar-speech-bubble-stub") as HTMLButtonElement).click();
    });
    expect(openBackgroundAgentInteraction).toHaveBeenCalledWith(requestId);

    useAvatarRuntimeMock.mockReturnValue({
      bottomBubble: null,
      assistantBubble: null,
      chatMode: "expanded",
      pendingInteraction: {
        requestId,
        kind: "permission"
      }
    });
    act(() => {
      root.render(<AvatarApp />);
    });
    expect(container.textContent).not.toContain("后台任务需要你的授权，点击处理");
  });

});

function ringingReminder(reminderId: string, content: string) {
  return {
    reminder_id: reminderId,
    content,
    state: "ringing" as const,
    trigger_at: "2026-09-04T15:00:00+08:00",
    created_at: "2026-09-04T10:00:00+08:00",
    updated_at: "2026-09-04T15:00:00+08:00",
    finished_at: null,
    ring_count: 1,
    next_ring_at: "2026-09-04T15:01:00+08:00",
    finish_reason: null,
    can_modify: false,
    can_cancel: false,
    can_dismiss: true
  };
}

function createAvatarBridge() {
  return {
    reportDiagnostic: vi.fn(),
    runtime: {
      controlGateway: {
        websocketUrl: "ws://127.0.0.1:8765/ws/control",
        httpBaseUrl: "http://127.0.0.1:8765"
      },
      live2d: { defaultModelPath: "", catalog: [] }
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
}
