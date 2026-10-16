import {
  createControlSettingsSocket,
  type ControlSettingsSocket,
  type ControlSettingsSocketHandlers
} from "./controlSettingsSocket";
import { createReconnectingControlSocket } from "./reconnectingControlSocket";

type DesktopShortcutSettingsSnapshot = {
  enabled: boolean;
  accelerator: string;
};

export type DesktopRuntimeSettingsSnapshot = {
  revision: number;
  followupShortcut: DesktopShortcutSettingsSnapshot;
  avatarVisibilityShortcut: DesktopShortcutSettingsSnapshot;
  selectionCaptureShortcut: DesktopShortcutSettingsSnapshot;
};

export type ControlSettingsSyncService = {
  start(): Promise<void>;
  stop(): void;
  subscribe(listener: (settings: DesktopRuntimeSettingsSnapshot) => void): () => void;
};

export type { ControlSettingsSocketHandlers };

type ControlSettingsSocketFactory = (
  url: string,
  handlers: ControlSettingsSocketHandlers
) => ControlSettingsSocket;

type CreateControlSettingsSyncServiceOptions = {
  httpBaseUrl: string;
  reconnectDelayMs?: number;
  random?: () => number;
  socketFactory?: ControlSettingsSocketFactory;
  logger?: Pick<Console, "info" | "warn">;
};

type BackendSettingsPayload = {
  settings_revision?: unknown;
  followup_toggle_shortcut_enabled?: unknown;
  followup_toggle_shortcut?: unknown;
  avatar_visibility_shortcut_enabled?: unknown;
  avatar_visibility_shortcut?: unknown;
  selection_capture_shortcut_enabled?: unknown;
  selection_capture_shortcut?: unknown;
};

type BackendControlEventPayload = {
  type?: unknown;
  payload?: unknown;
};

/**
 * 创建控制面设置同步服务。
 *
 * @param options 控制面地址、socket 实现和重连节奏。
 * @returns 以控制 WebSocket 事件流为真相源的运行期设置同步服务。
 */
export function createControlSettingsSyncService(
  options: CreateControlSettingsSyncServiceOptions
): ControlSettingsSyncService {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const reconnectDelayMs = options.reconnectDelayMs;
  const random = options.random;
  const socketFactory = options.socketFactory ?? createControlSettingsSocket;
  const logger = options.logger ?? console;
  const listeners = new Set<(settings: DesktopRuntimeSettingsSnapshot) => void>();

  let latestSettings: DesktopRuntimeSettingsSnapshot | null = null;
  function applySettingsPayload(payload: unknown): void {
    try {
      const nextSettings = readDesktopRuntimeSettings(payload);

      // 设置 revision 是同一 Backend 内的单调版本轴。WebSocket 重连或多条
      // 传输路径可能让旧快照晚到；一旦接受它，桌面端会重新注册旧快捷键，
      // Console 也会看到与当前设置不一致的运行状态，所以直接丢弃。
      if (
        latestSettings !== null
        && nextSettings.revision < latestSettings.revision
      ) {
        return;
      }

      const shouldPublish = shouldPublishSettings(nextSettings, latestSettings);
      latestSettings = nextSettings;
      if (!shouldPublish) {
        return;
      }

      logger.info?.("[companion-main] control settings snapshot applied", "settings.changed", nextSettings);
      for (const listener of listeners) {
        listener(nextSettings);
      }
    } catch (error) {
      logger.warn("[companion-main] control settings event ignored", "invalid_settings_payload", error);
    }
  }

  function handleSocketMessage(message: string): void {
    let event: unknown;
    try {
      event = JSON.parse(message);
    } catch {
      logger.warn("[companion-main] control settings event ignored", "invalid_json");
      return;
    }

    if (!isControlEventPayload(event)) {
      return;
    }

    if (event.type !== "settings.changed") {
      return;
    }

    applySettingsPayload(event.payload);
  }

  const controlConnection = createReconnectingControlSocket({
    url: resolveControlWebSocketUrl(normalizedHttpBaseUrl),
    socketFactory,
    onMessage: handleSocketMessage,
    reconnectDelayMs,
    random,
    logger,
    logName: "[companion-main] control settings"
  });

  return {
    async start(): Promise<void> {
      controlConnection.start();
    },

    stop(): void {
      controlConnection.stop();
    },

    subscribe(listener: (settings: DesktopRuntimeSettingsSnapshot) => void): () => void {
      listeners.add(listener);
      if (latestSettings !== null) {
        listener(latestSettings);
      }

      return () => {
        listeners.delete(listener);
      };
    }
  };
}

/**
 * 判断原始 JSON 是否是控制事件外壳。
 */
function isControlEventPayload(payload: unknown): payload is BackendControlEventPayload {
  return payload !== null && typeof payload === "object" && !Array.isArray(payload);
}

/**
 * 从控制面设置快照中提取桌面主进程关心的运行期设置。
 */
function readDesktopRuntimeSettings(payload: unknown): DesktopRuntimeSettingsSnapshot {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("settings snapshot response is invalid");
  }

  const candidate = payload as BackendSettingsPayload;
  if (
    typeof candidate.settings_revision !== "number" ||
    !Number.isSafeInteger(candidate.settings_revision) ||
    candidate.settings_revision < 0 ||
    typeof candidate.followup_toggle_shortcut_enabled !== "boolean" ||
    typeof candidate.followup_toggle_shortcut !== "string" ||
    candidate.followup_toggle_shortcut.trim().length === 0 ||
    typeof candidate.avatar_visibility_shortcut_enabled !== "boolean" ||
    typeof candidate.avatar_visibility_shortcut !== "string" ||
    candidate.avatar_visibility_shortcut.trim().length === 0 ||
    typeof candidate.selection_capture_shortcut_enabled !== "boolean" ||
    typeof candidate.selection_capture_shortcut !== "string" ||
    candidate.selection_capture_shortcut.trim().length === 0
  ) {
    throw new Error("settings snapshot response is missing desktop shortcut fields");
  }

  return {
    revision: candidate.settings_revision,
    followupShortcut: {
      enabled: candidate.followup_toggle_shortcut_enabled,
      accelerator: candidate.followup_toggle_shortcut.trim()
    },
    avatarVisibilityShortcut: {
      enabled: candidate.avatar_visibility_shortcut_enabled,
      accelerator: candidate.avatar_visibility_shortcut.trim()
    },
    selectionCaptureShortcut: {
      enabled: candidate.selection_capture_shortcut_enabled,
      accelerator: candidate.selection_capture_shortcut.trim()
    }
  };
}

/**
 * 判断新快照是否需要通知订阅者。
 */
function shouldPublishSettings(
  nextSettings: DesktopRuntimeSettingsSnapshot,
  previousSettings: DesktopRuntimeSettingsSnapshot | null
): boolean {
  if (previousSettings === null) {
    return true;
  }

  return (
    nextSettings.revision !== previousSettings.revision ||
    nextSettings.followupShortcut.enabled !== previousSettings.followupShortcut.enabled ||
    nextSettings.followupShortcut.accelerator !== previousSettings.followupShortcut.accelerator ||
    nextSettings.avatarVisibilityShortcut.enabled !== previousSettings.avatarVisibilityShortcut.enabled ||
    nextSettings.avatarVisibilityShortcut.accelerator !== previousSettings.avatarVisibilityShortcut.accelerator ||
    nextSettings.selectionCaptureShortcut.enabled !== previousSettings.selectionCaptureShortcut.enabled ||
    nextSettings.selectionCaptureShortcut.accelerator !== previousSettings.selectionCaptureShortcut.accelerator
  );
}

/**
 * 把控制 HTTP 根地址转换为控制 WebSocket 地址。
 */
function resolveControlWebSocketUrl(httpBaseUrl: string): string {
  const url = new URL(httpBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/control";
  url.search = "";
  url.hash = "";
  return url.toString();
}
