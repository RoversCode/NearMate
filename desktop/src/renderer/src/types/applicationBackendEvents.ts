import type { MaterialContextSnapshot } from "../materials/materialTypes";
import { readMaterialContextSnapshot } from "../../../shared/materialContext";
import { isStandardUuid } from "../../../shared/uuid";
import {
  readTurnSubmissionsSnapshot,
  type TurnSubmissionsSnapshot
} from "../../../shared/turnSubmissions";
import type { AudioDeviceSelectionPayload } from "./audioPayloads";
import type {
  ControlEventDomain,
  CoreToUiEventNameByDomain
} from "./eventCatalog";
import type { SettingsSnapshot } from "./settingsSnapshot";
import { isSettingsSnapshot } from "./settingsSnapshot";
import {
  hasExactKeys,
  isNonEmptyString,
  isRecord,
  isStringList,
  preservePayloadWhen,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";

/**
 * 描述清理旧资源、准备模型和装配运行时的真实进度。
 *
 * Args:
 *   不适用；字段由 Backend 的公开快照提供。
 * Returns:
 *   带进程、操作和版本身份的进度；只有 ready 才表示必要组件已装配完成。
 */
export type RuntimeInitializationPayload = {
  runtime_id: string;
  operation_id: string | null;
  revision: number;
  state: "idle" | "initializing" | "ready" | "failed";
  phase: "idle" | "cleanup" | "input_models" | "assistant" | "media" | "ready" | "failed";
  message: string;
  retryable: boolean;
};

export type BootstrapStatusChangedPayload = {
  is_initialized: boolean;
  llm: { is_ready: boolean; missing: string[]; errors: string[] };
  voice: {
    provider: string;
    is_ready: boolean;
    phase: string;
    model_id: string | null;
    role_name: string | null;
    has_confirmed_voice: boolean;
  };
  has_qweather_api_key: boolean;
};

type RuntimeInitializationChangedEvent = {
  type: "runtime.initialization_changed";
  payload: RuntimeInitializationPayload;
};

type BootstrapStatusChangedEvent = {
  type: "bootstrap.status_changed";
  payload: BootstrapStatusChangedPayload;
};

/** Backend 通过控制 WebSocket 发布的完整设置快照。 */
export type SettingsChangedPayload = SettingsSnapshot;

type MaterialContextsChangedEvent = {
  type: "materials.contexts_changed";
  payload: MaterialContextSnapshot;
};

type MaterialAssetsReleasedEvent = {
  type: "materials.assets_released";
  payload: { managed_asset_ids: string[] };
};

type MaterialPreflightFailedEvent = {
  type: "materials.preflight_failed";
  payload: { items: Array<{ material_id: string; display_name: string; reason: string }> };
};

type TurnSubmissionsSnapshotChangedEvent = {
  type: "turn_submissions.snapshot_changed";
  payload: TurnSubmissionsSnapshot;
};

type SettingsChangedEvent = {
  type: "settings.changed";
  payload: SettingsChangedPayload;
};

type SettingsShortcutStatusChangedEvent = {
  type: "settings.shortcut_status_changed";
  payload: {
    items: Array<{
      shortcut_id: "followup_toggle" | "avatar_visibility" | "selection_capture";
      accelerator: string;
      enabled: boolean;
      status: "registered" | "disabled" | "internal_conflict" | "unavailable" | "unknown";
      settings_revision: number;
      checked_at: string;
      message: string;
    }>;
  };
};

export type AudioDevicesChangeRequestedEvent = {
  type: "audio.devices_change_requested";
  payload: {
    request_id: string;
    selection: AudioDeviceSelectionPayload;
    requested_at: number;
  };
};

type AudioDevicesApplyResultEvent = {
  type: "audio.devices_apply_result";
  payload: {
    request_id: string;
    status: "applied" | "failed";
    mode: "system_default" | "manual";
    reason: string | null;
    applied_at: number;
  };
};

type ShellAvatarBubbleChangedEvent = {
  type: "shell.avatar_bubble_changed";
  payload:
    | { kind: "hidden" }
    | { kind: "speech" | "notice"; text: string; ttl_ms: number };
};

export type ApplicationBackendEvent =
  | RuntimeInitializationChangedEvent
  | BootstrapStatusChangedEvent
  | MaterialContextsChangedEvent
  | MaterialAssetsReleasedEvent
  | MaterialPreflightFailedEvent
  | TurnSubmissionsSnapshotChangedEvent
  | SettingsChangedEvent
  | SettingsShortcutStatusChangedEvent
  | AudioDevicesChangeRequestedEvent
  | AudioDevicesApplyResultEvent
  | ShellAvatarBubbleChangedEvent;

type ApplicationEventDomain = Extract<
  ControlEventDomain,
  "application" | "materials" | "turn_admission" | "settings" | "audio_devices" | "shell"
>;
type ApplicationEventName = CoreToUiEventNameByDomain<ApplicationEventDomain>;

export const APPLICATION_EVENT_PARSERS = {
  "runtime.initialization_changed": payload => preservePayloadWhen(payload, isRuntimeInitializationPayload),
  "bootstrap.status_changed": payload => preservePayloadWhen(payload, isBootstrapStatusChangedPayload),
  "materials.contexts_changed": parseMaterialContextSnapshot,
  "materials.assets_released": payload => preservePayloadWhen(
    payload,
    isMaterialAssetsReleasedPayload
  ),
  "materials.preflight_failed": payload => preservePayloadWhen(
    payload,
    isMaterialPreflightFailedPayload
  ),
  "turn_submissions.snapshot_changed": parseTurnSubmissionsSnapshot,
  "settings.changed": payload => preservePayloadWhen(payload, isSettingsSnapshot),
  "settings.shortcut_status_changed": payload => preservePayloadWhen(
    payload,
    isShortcutStatusPayload
  ),
  "audio.devices_change_requested": payload => preservePayloadWhen(
    payload,
    isAudioDevicesChangeRequestedPayload
  ),
  "audio.devices_apply_result": payload => preservePayloadWhen(
    payload,
    isAudioDevicesApplyResultPayload
  ),
  "shell.avatar_bubble_changed": payload => preservePayloadWhen(
    payload,
    isShellAvatarBubbleChangedPayload
  )
} satisfies Record<ApplicationEventName, BackendPayloadParser>;

/** 校验恢复进度的公开字段，避免把内部故障或非法版本带入门禁。 */
function isRuntimeInitializationPayload(payload: unknown): payload is RuntimeInitializationPayload {
  if (!isRecord(payload)
    || !hasExactKeys(payload, ["runtime_id", "operation_id", "revision", "state", "phase", "message", "retryable"])
    || typeof payload.runtime_id !== "string" || !isStandardUuid(payload.runtime_id)
    || !(payload.operation_id === null || (typeof payload.operation_id === "string" && isStandardUuid(payload.operation_id)))
    || !Number.isSafeInteger(payload.revision) || (payload.revision as number) < 0
    || !isNonEmptyString(payload.message)
    || typeof payload.retryable !== "boolean") return false;
  const phasesByState: Record<RuntimeInitializationPayload["state"], readonly string[]> = {
    idle: ["idle"], initializing: ["cleanup", "input_models", "assistant", "media"],
    ready: ["ready"], failed: ["failed"]
  };
  return typeof payload.state === "string" && Object.hasOwn(phasesByState, payload.state)
    && typeof payload.phase === "string" && phasesByState[payload.state as RuntimeInitializationPayload["state"]].includes(payload.phase)
    && (payload.state !== "initializing" || payload.operation_id !== null)
    && (!payload.retryable || payload.state === "failed");
}

function isBootstrapStatusChangedPayload(payload: unknown): payload is BootstrapStatusChangedPayload {
  if (!isRecord(payload)
    || !hasExactKeys(payload, ["is_initialized", "llm", "voice", "has_qweather_api_key"])
    || typeof payload.is_initialized !== "boolean"
    || typeof payload.has_qweather_api_key !== "boolean"
    || !isRecord(payload.llm)
    || !isRecord(payload.voice)
    || !hasExactKeys(payload.llm, ["is_ready", "missing", "errors"])
    || typeof payload.llm.is_ready !== "boolean"
    || !isStringList(payload.llm.missing)
    || !isStringList(payload.llm.errors)) {
    return false;
  }
  return isBootstrapVoiceStatus(payload.voice);
}

function isBootstrapVoiceStatus(value: Record<string, unknown>): boolean {
  const requiredKeys = ["provider", "is_ready", "phase", "model_id", "role_name", "has_confirmed_voice"];
  if (Object.keys(value).length !== requiredKeys.length ||
      !requiredKeys.every(key => Object.hasOwn(value, key))) return false;
  return typeof value.provider === "string"
    && typeof value.is_ready === "boolean"
    && typeof value.phase === "string"
    && (value.model_id === null || typeof value.model_id === "string")
    && (value.role_name === null || typeof value.role_name === "string")
    && typeof value.has_confirmed_voice === "boolean";
}

/** 解析并返回权威材料快照，解析器的返回值就是 reducer 使用的结构。 */
function parseMaterialContextSnapshot(payload: unknown): MaterialContextSnapshot | null {
  try {
    return readMaterialContextSnapshot(payload);
  } catch {
    return null;
  }
}

/** 解析 Backend 字段名的 Turn 队列快照并转换为 Desktop 字段名。 */
function parseTurnSubmissionsSnapshot(payload: unknown): TurnSubmissionsSnapshot | null {
  try {
    return readTurnSubmissionsSnapshot(payload);
  } catch {
    return null;
  }
}

/** 校验已经释放的托管材料身份列表。 */
function isMaterialAssetsReleasedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["managed_asset_ids"])
    && isStringList(payload.managed_asset_ids);
}

/** 校验材料预检失败列表，避免内部异常对象进入 UI。 */
function isMaterialPreflightFailedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["items"])
    && Array.isArray(payload.items)
    && payload.items.every(item => isRecord(item)
      && hasExactKeys(item, ["material_id", "display_name", "reason"])
      && isNonEmptyString(item.material_id)
      && isNonEmptyString(item.display_name)
      && isNonEmptyString(item.reason));
}

/** 校验三类桌面快捷键的完整运行期状态快照。 */
function isShortcutStatusPayload(payload: unknown): boolean {
  if (!isRecord(payload) || !hasExactKeys(payload, ["items"]) || !Array.isArray(payload.items)) {
    return false;
  }
  const shortcutIds = payload.items.map(item => (
    isRecord(item) ? item.shortcut_id : undefined
  ));
  return payload.items.length === 3
    && new Set(shortcutIds).size === 3
    && ["followup_toggle", "avatar_visibility", "selection_capture"].every(
      shortcutId => shortcutIds.includes(shortcutId)
    )
    && payload.items.every(isShortcutStatusItem);
}

/** 校验单个快捷键状态，禁止 Main 进程诊断对象混入载荷。 */
function isShortcutStatusItem(value: unknown): boolean {
  return isRecord(value)
    && hasExactKeys(value, [
      "shortcut_id",
      "accelerator",
      "enabled",
      "status",
      "settings_revision",
      "checked_at",
      "message"
    ])
    && ["followup_toggle", "avatar_visibility", "selection_capture"].includes(
      String(value.shortcut_id)
    )
    && typeof value.accelerator === "string"
    && typeof value.enabled === "boolean"
    && ["registered", "disabled", "internal_conflict", "unavailable", "unknown"].includes(
      String(value.status)
    )
    && Number.isSafeInteger(value.settings_revision)
    && (value.settings_revision as number) >= 0
    && typeof value.checked_at === "string"
    && typeof value.message === "string";
}

/** 校验音频设备热更新请求及完整设备选择。 */
function isAudioDevicesChangeRequestedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["request_id", "selection", "requested_at"])
    && isNonEmptyString(payload.request_id)
    && isAudioDeviceSelectionPayload(payload.selection)
    && typeof payload.requested_at === "number"
    && Number.isFinite(payload.requested_at);
}

/** 校验音频设备选择的模式和四个可空设备字段。 */
function isAudioDeviceSelectionPayload(value: unknown): boolean {
  return isRecord(value)
    && hasExactKeys(value, [
      "mode",
      "microphone_id",
      "microphone_name",
      "speaker_loopback_id",
      "speaker_loopback_name"
    ])
    && (value.mode === "system_default" || value.mode === "manual")
    && isNullableString(value.microphone_id)
    && isNullableString(value.microphone_name)
    && isNullableString(value.speaker_loopback_id)
    && isNullableString(value.speaker_loopback_name);
}

/** 校验音频设备热更新终态。 */
function isAudioDevicesApplyResultPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["request_id", "status", "mode", "reason", "applied_at"])
    && isNonEmptyString(payload.request_id)
    && (payload.status === "applied" || payload.status === "failed")
    && (payload.mode === "system_default" || payload.mode === "manual")
    && isNullableString(payload.reason)
    && typeof payload.applied_at === "number"
    && Number.isFinite(payload.applied_at);
}

/** 校验 Shell 气泡投影的隐藏或可见两种互斥形状。 */
function isShellAvatarBubbleChangedPayload(payload: unknown): boolean {
  if (!isRecord(payload)) {
    return false;
  }
  if (payload.kind === "hidden") {
    return hasExactKeys(payload, ["kind"]);
  }
  return (payload.kind === "speech" || payload.kind === "notice")
    && hasExactKeys(payload, ["kind", "text", "ttl_ms"])
    && isNonEmptyString(payload.text)
    && typeof payload.ttl_ms === "number"
    && Number.isFinite(payload.ttl_ms);
}

/** 判断值是否为空或字符串。 */
function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}
