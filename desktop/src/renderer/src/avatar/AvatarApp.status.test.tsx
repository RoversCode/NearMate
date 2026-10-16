// @vitest-environment jsdom

import { act } from "react";
import { describe, expect, it } from "vitest";
import {
  AvatarApp,
  avatarStageRenderSpy,
  container,
  root,
  useAvatarRuntimeMock
} from "./AvatarApp.testHarness";

describe("AvatarApp status ornament", () => {
  it("Chat 展开时保留无文字状态图案，并继续抑制所有对话气泡", () => {
    useAvatarRuntimeMock.mockReturnValue({
      chatMode: "expanded",
      bottomBubble: { kind: "speech", text: "用户表达", ttlMs: 3000 },
      assistantBubble: { visibleText: "助手回复" },
      companionStatus: {
        state: "thinking",
        label: "思考中",
        description: "正在整理回应",
        wakeMode: "followup",
        wakeLabel: "跟进模式已开启",
        manualToggleRevision: 0
      }
    });
    act(() => root.render(<AvatarApp />));
    const stageProps = avatarStageRenderSpy.mock.calls.at(-1)?.[0] as {
      onPetBoundsChange?(bounds: { x: number; y: number; width: number; height: number }): void;
    };
    act(() => stageProps.onPetBoundsChange?.({ x: 120, y: 140, width: 160, height: 300 }));

    const indicator = container.querySelector("[data-avatar-status='thinking']");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toBe("");
    expect(container.querySelector(".avatar-speech-bubble-stub, .avatar-assistant-bubble-stub")).toBeNull();
  });
});
