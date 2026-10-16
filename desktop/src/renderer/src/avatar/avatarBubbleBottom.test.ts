import { describe, expect, it } from "vitest";
import type { AvatarBubbleState } from "./avatarBubbleTypes";
import { applyBottomAvatarBubbleEvent, clearTimedBottomBubble } from "./avatarBubbleBottom";

const baseState: AvatarBubbleState = {
  showSpeechBubbles: true,
  activeCharacterCaptionTiming: null,
  bottomBubble: null,
  assistantBubble: null
};

describe("avatarBubbleBottom", () => {
  it("普通底部气泡超时会清除", () => {
    const speechState = applyBottomAvatarBubbleEvent(baseState, {
      type: "shell.avatar_bubble_changed",
      payload: {
        kind: "speech",
        text: "普通语音会自动消失。",
        ttl_ms: 1000
      }
    });
    expect(clearTimedBottomBubble(speechState).bottomBubble).toBeNull();
  });
});
