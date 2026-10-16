import { describe, expect, it, vi } from "vitest";
import { ShortcutSpeechGate } from "./shortcutSpeechGate";
import type { BackendEvent } from "../types/backendEvents";

function ready(requestId: string): BackendEvent {
  return { type: "assistant.playout_delivery_ready", payload: {
    request_id: requestId, generation_id: `generation-${requestId}`, delivery_id: `delivery-${requestId}`
  } };
}

describe("快捷键语音门禁", () => {
  it.each([true, false])("等待 HTTP 期间提交的新轮在失败后仍可恢复（交付先于失败：%s）", readyBeforeFailure => {
    const mute = vi.fn();
    const deliver = vi.fn();
    const gate = new ShortcutSpeechGate(mute, deliver);
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
    gate.handleBackendEvent({ type: "user.final_transcript", payload: {
      session_id: "desktop-text", turn_id: null, submission_id: "new", request_id: "new-user-turn",
      text: "继续说", history_disposition: "ordinary_turn"
    } });
    if (readyBeforeFailure) gate.handleBackendEvent(ready("new-user-turn"));
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: null });
    if (!readyBeforeFailure) gate.handleBackendEvent(ready("new-user-turn"));
    expect(mute).toHaveBeenLastCalledWith(false);
    expect(deliver).toHaveBeenCalledOnce();
    expect(deliver.mock.calls[0][0].request_id).toBe("new-user-turn");
  });
  it.each([true, false])("仅取消点之后的新唤醒可以恢复短答（唤醒先于回执：%s）", wakeBeforeAck => {
    const mute = vi.fn();
    const gate = new ShortcutSpeechGate(mute, vi.fn());
    const wake = (at: number): BackendEvent => ({ type: "wake.detected", payload: {
      input_runtime_id: "input-runtime-test", input_runtime_epoch: 1, session_id: "voice", detected_at: at
    } });
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
    gate.handleBackendEvent(wake(99));
    if (wakeBeforeAck) gate.handleBackendEvent(wake(101));
    expect(mute).not.toHaveBeenCalledWith(false);
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1,
      interruptedRequestIds: [], speechInterruptedAt: 100 });
    if (!wakeBeforeAck) {
      expect(mute).not.toHaveBeenCalledWith(false);
      gate.handleBackendEvent(wake(101));
    }
    expect(mute).toHaveBeenLastCalledWith(false);
  });
  it("按键同步静音，回执与迟到旧交付都不能重新放声，新请求可以恢复", () => {
    const mute = vi.fn();
    const deliver = vi.fn();
    const gate = new ShortcutSpeechGate(mute, deliver);
    gate.handleBackendEvent(ready("old"));
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
    expect(mute).toHaveBeenLastCalledWith(true);
    expect(gate.handleBackendEvent(ready("old"))).toBe(false);
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: ["old", "queued"] });
    expect(gate.handleBackendEvent(ready("queued"))).toBe(false);
    expect(mute).toHaveBeenCalledTimes(1);
    gate.handleBackendEvent(ready("new"));
    expect(mute).toHaveBeenLastCalledWith(false);
    expect(deliver.mock.calls.map(([delivery]) => delivery.request_id)).toEqual(["old", "new"]);
  });

  it("快速连按的旧回执不能解除新按键静音，等待中的新交付只回执一次", () => {
    const mute = vi.fn();
    const deliver = vi.fn();
    const gate = new ShortcutSpeechGate(mute, deliver);
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 2 });
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: ["old"] });
    gate.handleBackendEvent(ready("new"));
    expect(mute).not.toHaveBeenCalledWith(false);
    expect(deliver).not.toHaveBeenCalled();
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 2, interruptedRequestIds: ["old"] });
    expect(mute).toHaveBeenLastCalledWith(false);
    expect(deliver).toHaveBeenCalledOnce();
  });

  it("网络失败不会恢复旧声，之后新的真实交付仍可以正常说话", () => {
    const mute = vi.fn();
    const gate = new ShortcutSpeechGate(mute, vi.fn());
    gate.handleFeedback({ phase: "pressed", shortcutId: "followup_toggle", sequence: 1 });
    gate.handleBackendEvent(ready("late-old"));
    gate.handleFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: null });
    expect(gate.handleBackendEvent(ready("late-old"))).toBe(false);
    expect(mute).not.toHaveBeenCalledWith(false);
    expect(gate.handleBackendEvent(ready("previously-unseen-old"))).toBe(false);
    gate.handleBackendEvent({ type: "user.final_transcript", payload: {
      session_id: "desktop-text", turn_id: null, submission_id: "next", request_id: "next-turn",
      text: "继续", history_disposition: "ordinary_turn"
    } });
    gate.handleBackendEvent(ready("next-turn"));
    expect(mute).toHaveBeenLastCalledWith(false);
  });

  it("形象和选区快捷键不会中断角色语音", () => {
    const mute = vi.fn();
    const gate = new ShortcutSpeechGate(mute, vi.fn());
    gate.handleFeedback({ phase: "pressed", shortcutId: "avatar_visibility", sequence: 1 });
    gate.handleFeedback({ phase: "pressed", shortcutId: "selection_capture", sequence: 2 });
    expect(mute).not.toHaveBeenCalled();
  });
});
