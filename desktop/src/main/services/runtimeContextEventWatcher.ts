import {
  createControlSettingsSocket,
  type ControlSettingsSocket,
  type ControlSettingsSocketHandlers
} from "./controlSettingsSocket";
import type { EnvironmentSnapshotService } from "./environmentSnapshotService";
import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";
import type {
  MaterialDeliveryFeedbackService,
  MaterialPreflightFailure
} from "./materialDeliveryFeedbackService";
import { createReconnectingControlSocket } from "./reconnectingControlSocket";

export type RuntimeContextEventWatcher = {
  start(): Promise<void>;
  stop(): void;
};

type ControlSocketFactory = (
  url: string,
  handlers: ControlSettingsSocketHandlers
) => ControlSettingsSocket;

type CreateRuntimeContextEventWatcherOptions = {
  httpBaseUrl: string;
  environmentSnapshot: Pick<EnvironmentSnapshotService, "captureVoiceSnapshot">;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "release">;
  materialFeedback: Pick<MaterialDeliveryFeedbackService, "notifyMaterialPreflightFailures">;
  reconnectDelayMs?: number;
  random?: () => number;
  socketFactory?: ControlSocketFactory;
  logger?: Pick<Console, "warn">;
};

/**
 * 创建环境刷新与受管材料资产释放的运行期事件监听器。
 *
 * @param options 控制面地址、两个运行期服务与可注入 socket。
 * @returns 可跟随桌面主进程生命周期启动和停止的监听器。
 */
export function createRuntimeContextEventWatcher(
  options: CreateRuntimeContextEventWatcherOptions
): RuntimeContextEventWatcher {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const reconnectDelayMs = options.reconnectDelayMs;
  const random = options.random;
  const socketFactory = options.socketFactory ?? createControlSettingsSocket;
  const logger = options.logger ?? console;

  function handleSocketMessage(message: string): void {
    let event: unknown;
    try {
      event = JSON.parse(message);
    } catch {
      logger.warn("[companion-main] runtime context event ignored", "invalid_json");
      return;
    }

    if (!isControlEvent(event)) {
      return;
    }
    if (event.type === "environment.snapshot_requested") {
      const metadata = readEnvironmentSnapshotMetadata(event.payload);
      if (metadata !== null) {
        void options.environmentSnapshot.captureVoiceSnapshot(metadata);
      }
      return;
    }
    if (event.type === "materials.preflight_failed") {
      const failures = readPreflightFailures(event.payload);
      if (failures.length > 0) {
        options.materialFeedback.notifyMaterialPreflightFailures(failures);
      }
      return;
    }
    if (event.type !== "materials.assets_released") {
      return;
    }

    const managedAssetIds = readManagedAssetIds(event.payload);
    void Promise.all(managedAssetIds.map(id => options.managedAssetStore.release(id))).catch(error => {
      logger.warn("[companion-main] managed material asset release failed", error);
    });
  }

  const controlConnection = createReconnectingControlSocket({
    url: resolveControlWebSocketUrl(normalizedHttpBaseUrl),
    socketFactory,
    onMessage: handleSocketMessage,
    reconnectDelayMs,
    random,
    logger,
    logName: "[companion-main] runtime context"
  });

  return {
    async start(): Promise<void> {
      controlConnection.start();
    },

    stop(): void {
      controlConnection.stop();
    }
  };
}

function readEnvironmentSnapshotMetadata(payload: unknown): {
  request_id: string;
  turn_id: string;
  reason: "voice_turn_started";
} | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const candidate = payload as Record<string, unknown>;
  if (typeof candidate.request_id !== "string" || !candidate.request_id.trim()
    || typeof candidate.turn_id !== "string" || !candidate.turn_id.trim()
    || candidate.reason !== "voice_turn_started") {
    return null;
  }
  return {
    request_id: candidate.request_id.trim(),
    turn_id: candidate.turn_id.trim(),
    reason: "voice_turn_started"
  };
}

function readPreflightFailures(payload: unknown): MaterialPreflightFailure[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return [];
  }
  const items = (payload as { items?: unknown }).items;
  if (!Array.isArray(items)) {
    return [];
  }
  return items.flatMap(item => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return [];
    }
    const candidate = item as Record<string, unknown>;
    if (typeof candidate.material_id !== "string" || !candidate.material_id.trim()
      || typeof candidate.display_name !== "string" || !candidate.display_name.trim()
      || typeof candidate.reason !== "string" || !candidate.reason.trim()) {
      return [];
    }
    return [{
      materialId: candidate.material_id.trim(),
      displayName: candidate.display_name.trim(),
      reason: candidate.reason.trim()
    }];
  });
}

function isControlEvent(payload: unknown): payload is { type: string; payload?: unknown } {
  return Boolean(payload) && typeof payload === "object" && !Array.isArray(payload)
    && typeof (payload as { type?: unknown }).type === "string";
}

function readManagedAssetIds(payload: unknown): string[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return [];
  }
  const ids = (payload as { managed_asset_ids?: unknown }).managed_asset_ids;
  return Array.isArray(ids)
    ? ids.filter((id): id is string => typeof id === "string" && Boolean(id.trim())).map(id => id.trim())
    : [];
}

function resolveControlWebSocketUrl(httpBaseUrl: string): string {
  const url = new URL(httpBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/control";
  url.search = "";
  url.hash = "";
  return url.toString();
}
