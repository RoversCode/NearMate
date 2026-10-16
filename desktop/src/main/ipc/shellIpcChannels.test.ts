import { describe, expect, it } from "vitest";
import {
  SHELL_IPC_HANDLER_CHANNELS,
  SHELL_IPC_LISTENER_CHANNELS
} from "./shellIpcChannels";

describe("shell IPC channel registry", () => {
  it("统一列出需要清理的 listener 和 handler channel", () => {
    expect(SHELL_IPC_LISTENER_CHANNELS).toEqual([
      "renderer:diagnostic",
      "avatar:hover-enter",
      "avatar:hover-leave",
      "avatar:open-panel",
      "avatar:material-drop-enter",
      "avatar:material-drop-leave",
      "avatar:drag-start",
      "avatar:drag-move",
      "avatar:drag-end",
      "avatar:resize-start",
      "avatar:resize-move",
      "avatar:resize-end",
      "avatar:apply-presentation-profile",
      "avatar:report-pet-bounds",
      "avatar:model-switch-state-changed",
      "avatar:open-background-agent-interaction",
      "avatar:open-chat",
      "panel:close",
      "panel:reset-avatar-layout",
      "panel:request-model-switch",
      "chat:set-mode",
      "chat:drag-start",
      "chat:drag-move",
      "chat:drag-end",
      "chat:resize-start",
      "chat:resize-move",
      "chat:resize-end",
      "desktop:open-console"
    ]);
    expect(SHELL_IPC_HANDLER_CHANNELS).toEqual([
      "livekit:create-session",
      "livekit:cancel-session",
      "avatar:request-visible-area",
      "avatar:get-chat-mode",
      "chat:get-mode",
      "turn-submission:reserve",
      "turn-submission:edit",
      "turn-submission:delete",
      "turn-submission:resume",
      "turn-submission:retry",
      "environment:prepare-text",
      "chat:open-external-url",
      "chat:show-item-in-folder",
      "materials:add-staged",
      "materials:remove-staged",
      "materials:forget-recent",
      "materials:confirm-remove-applied",
      "materials:get-failure-report",
      "materials:get-thumbnail",
      "materials:capture-clipboard-image-material",
      "materials:create-composer-text-material"
    ]);
  });
});
