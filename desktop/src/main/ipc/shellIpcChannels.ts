import { RENDERER_DIAGNOSTIC_CHANNEL } from "../../shared/rendererDiagnostics";

/**
 * SHELL_IPC_LISTENER_CHANNELS 是 registerShellIpcHandlers 注册的 send/on 通道清单。
 * 清理逻辑必须使用同一份清单，避免新增 IPC 后忘记 removeAllListeners。
 */
export const SHELL_IPC_LISTENER_CHANNELS = [
  RENDERER_DIAGNOSTIC_CHANNEL,
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
] as const;

/**
 * SHELL_IPC_HANDLER_CHANNELS 是 registerShellIpcHandlers 注册的 invoke/handle 通道清单。
 */
export const SHELL_IPC_HANDLER_CHANNELS = [
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
] as const;
