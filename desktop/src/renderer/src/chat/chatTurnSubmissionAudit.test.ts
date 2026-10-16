import { describe, expect, it } from "vitest";
import type { ReminderSettings } from "../types/events";
import { createInitialChatState, reduceChatState } from "./chatReducer";

const settingsFixture: ReminderSettings = {
  show_speech_bubbles: true,
  selected_live2d_model_id: "Haru"
};

describe("文本提交跨 HTTP 与 WebSocket 丢事件审计", () => {
  it("正式用户事件丢失时，空快照不能删除已被 Backend 接纳的用户气泡", () => {
    const submissionId = "00000000-0000-4000-8000-000000000201";
    let state = createInitialChatState(settingsFixture);
    state = reduceChatState(state, {
      type: "turn_submission_optimistically_added",
      submissionId,
      messageText: "不能消失的用户消息"
    });
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 1,
        items: [{
          submissionId,
          messageText: "不能消失的用户消息",
          state: "queued",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });

    // 模拟 Backend 已完成派发，但 WebSocket 恰好漏掉 user.final_transcript，
    // Desktop 只从 HTTP 响应或重连首帧收到提交已经离队的空快照。
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: { revision: 2, items: [] }
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      submissionId,
      text: "不能消失的用户消息",
      submissionState: "awaiting_final"
    });
  });

  it("派发失败事件把等待收口的气泡收口为普通用户消息", () => {
    const submissionId = "00000000-0000-4000-8000-000000000202";
    let state = createInitialChatState(settingsFixture);
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 1,
        items: [{
          submissionId,
          messageText: "需要失败收口",
          state: "queued",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: { revision: 2, items: [] }
    });

    state = reduceChatState(state, {
      type: "backend_event_received",
      event: {
        type: "assistant.request_failed",
        payload: {
          request_id: `text-input-${submissionId}`,
          code: "request_start_failed",
          message: "消息已经发送，但我现在没能开始回复，请稍后再试。"
        }
      }
    });

    expect(state.messages[0]).toMatchObject({
      requestId: `text-input-${submissionId}`,
      text: "需要失败收口"
    });
    expect(state.messages[0].submissionId).toBeUndefined();
    expect(state.messages[0].submissionState).toBeUndefined();
    expect(state.finalizedSubmissionIds.has(submissionId)).toBe(true);
  });

  it("正式用户事件到达后原位移除等待收口状态", () => {
    const submissionId = "00000000-0000-4000-8000-000000000203";
    let state = createInitialChatState(settingsFixture);
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 1,
        items: [{
          submissionId,
          messageText: "等待正式事件",
          state: "queued",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: { revision: 2, items: [] }
    });
    state = reduceChatState(state, {
      type: "backend_event_received",
      event: {
        type: "user.final_transcript",
        payload: {
          session_id: "desktop-text",
          turn_id: null,
          request_id: `text-input-${submissionId}`,
          submission_id: submissionId,
          history_disposition: "ordinary_turn",
          text: "等待正式事件"
        }
      }
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      submissionId,
      requestId: `text-input-${submissionId}`,
      text: "等待正式事件"
    });
    expect(state.messages[0].submissionState).toBeUndefined();
  });
});
