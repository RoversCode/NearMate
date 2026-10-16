import sharedDefaultSettings from "../../../../../shared/default-settings.json";
import type { ReminderSettings } from "../types/events";
import {
  isSettingsSnapshot,
  projectReminderSettings,
  type SettingsSnapshot
} from "../types/settingsSnapshot";
import type { PlayoutDeliveryAck } from "../avatar/playoutDeliveryCoordinator";
import {
  reportRendererDiagnostic,
  type RendererDiagnosticBridge
} from "./rendererDiagnostics";

const defaultSettings: ReminderSettings = {
  show_speech_bubbles: sharedDefaultSettings.show_speech_bubbles,
  followup_toggle_shortcut_enabled: sharedDefaultSettings.followup_toggle_shortcut_enabled,
  followup_toggle_shortcut: sharedDefaultSettings.followup_toggle_shortcut,
  avatar_visibility_shortcut_enabled: sharedDefaultSettings.avatar_visibility_shortcut_enabled,
  avatar_visibility_shortcut: sharedDefaultSettings.avatar_visibility_shortcut,
  selected_live2d_model_id: sharedDefaultSettings.selected_live2d_model_id
};

/**
 * 设置水合可回退默认值；成功确认必须读取权威快照，并允许调用方取消超时请求。
 *
 * Args:
 *   requireAuthoritative: 失败时抛错，避免默认设置被当成 Backend 已确认的结果。
 *   signal: 调用方用于取消超时或过期读取的信号。
 * Returns:
 *   本次设置读取的错误与取消策略。
 */
export type FetchSettingsOptions = { requireAuthoritative?: boolean; signal?: AbortSignal };

/**
 * 描述 renderer 对 Backend 发起的设置与播放回执请求。
 *
 * Args:
 *   不适用；这是 createControlHttpClient 返回的通信接口。
 * Returns:
 *   支持权威设置读取、设置保存和语音播放回执的窄接口。
 */
export type ControlHttpClient = {
  fetchSettings(options?: FetchSettingsOptions): Promise<ReminderSettings>;
  /**
   * 保存明确编辑的普通设置，保留完整快照中的其它用户值。
   * @param settings 本次编辑的字段；既有完整投影仍可传入，只读字段不提交。
   * @param onCommitted 可选的提交回调，接收服务端校验通过的完整新快照。
   * @returns 读取和提交完成；版本冲突或响应不合法时抛错。
   */
  updateSettings(settings: Partial<ReminderSettings>, onCommitted?: (snapshot: SettingsSnapshot) => void): Promise<void>;
  reportPlayoutDeliveryAck(ack: PlayoutDeliveryAck): Promise<void>;
};

/**
 * 创建控制层 HTTP 客户端。
 *
 * Args:
 *   httpBaseUrl: 控制网关 HTTP 根地址。
 *   diagnosticsBridge: 可选的安全诊断上报出口。
 * Returns:
 *   renderer 主动请求 Backend 的设置与播放回执接口。
 * 边界异常：普通设置水合失败返回共享默认值；显式要求权威读取或写操作失败时向上抛出。
 */
export function createControlHttpClient(
  httpBaseUrl: string,
  diagnosticsBridge?: RendererDiagnosticBridge
): ControlHttpClient {
  return {
    async fetchSettings(options = {}) {
      try {
        const response = options.signal
          ? await fetch(`${httpBaseUrl}/api/settings`, { signal: options.signal })
          : await fetch(`${httpBaseUrl}/api/settings`);
        if (!response.ok) {
          throw new Error(`settings fetch request failed: ${response.status}`);
        }

        const snapshot = await readSettingsSnapshot(response, diagnosticsBridge);
        return projectReminderSettings(snapshot);
      } catch (error) {
        // 默认快照只用于初次显示，不能冒充 Backend 已经确认了某次人物切换。
        if (options.requireAuthoritative) throw error;
        return {
          ...defaultSettings
        };
      }
    },

    async updateSettings(settings, onCommitted) {
      // 设置页、快捷键和人物共用一个版本。先读取真实完整快照，保留本次没有编辑的用户字段。
      const settingsResponse = await fetch(`${httpBaseUrl}/api/settings`);
      if (!settingsResponse.ok) throw new Error(`settings fetch request failed: ${settingsResponse.status}`);
      const snapshot = await readSettingsSnapshot(settingsResponse, diagnosticsBridge);
      // 只列出普通设置接口允许的字段，字幕校准等只读投影不能随人物请求一起提交。
      const patch = {
        expected_revision: snapshot.settings_revision,
        show_speech_bubbles: settings.show_speech_bubbles ?? snapshot.show_speech_bubbles,
        followup_toggle_shortcut_enabled: settings.followup_toggle_shortcut_enabled ?? snapshot.followup_toggle_shortcut_enabled,
        followup_toggle_shortcut: settings.followup_toggle_shortcut ?? snapshot.followup_toggle_shortcut,
        avatar_visibility_shortcut_enabled: settings.avatar_visibility_shortcut_enabled ?? snapshot.avatar_visibility_shortcut_enabled,
        avatar_visibility_shortcut: settings.avatar_visibility_shortcut ?? snapshot.avatar_visibility_shortcut,
        selection_capture_shortcut_enabled: settings.selection_capture_shortcut_enabled ?? snapshot.selection_capture_shortcut_enabled,
        selection_capture_shortcut: settings.selection_capture_shortcut ?? snapshot.selection_capture_shortcut,
        selected_live2d_model_id: settings.selected_live2d_model_id ?? snapshot.selected_live2d_model_id,
        user_self_intro: snapshot.user_self_intro,
        user_extra_notes: snapshot.user_extra_notes
      };
      const response = await fetch(`${httpBaseUrl}/api/settings`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(patch)
      });

      /**
       * 真实服务端明确返回失败时，不能再静默吞掉。
       * 版本冲突同样向上传错，让用户重试；不能用旧完整快照自动覆盖另一窗口刚提交的设置。
       */
      if (!response.ok) {
        throw new Error(`settings update request failed: ${response.status}`);
      }
      // 采用实际提交结果恢复 Avatar；客户端的旧投影不能盖回 Backend 的完整新快照。
      const committedSnapshot = await readSettingsSnapshot(response, diagnosticsBridge);
      onCommitted?.(committedSnapshot);
    },

    async reportPlayoutDeliveryAck(ack) {
      const response = await fetch(`${httpBaseUrl}/api/assistant/playout-delivery/ack`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(ack)
      });

      if (!response.ok) {
        throw new Error(`playout delivery acknowledgement failed: ${response.status}`);
      }
    }
  };
}

/**
 * 校验 Backend 的完整设置响应，拒绝默认值或残缺快照冒充权威状态。
 *
 * Args:
 *   response: 设置读取或提交成功后的 HTTP 响应。
 *   diagnosticsBridge: 可选的脱敏诊断出口。
 * Returns:
 *   合同完整的设置快照；坏 JSON 或字段不合法时抛错。
 */
async function readSettingsSnapshot(response: Response, diagnosticsBridge?: RendererDiagnosticBridge): Promise<SettingsSnapshot> {
  let snapshot: unknown;
  try {
    snapshot = await response.json();
  } catch {
    reportInvalidSettingsSnapshot(diagnosticsBridge);
    throw new Error("settings snapshot is not valid JSON");
  }
  if (!isSettingsSnapshot(snapshot)) {
    reportInvalidSettingsSnapshot(diagnosticsBridge);
    throw new Error("settings snapshot is invalid");
  }
  return snapshot;
}

/** 只上报稳定原因码，不携带可能包含用户配置的原始响应。 */
function reportInvalidSettingsSnapshot(
  diagnosticsBridge?: RendererDiagnosticBridge
): void {
  if (!diagnosticsBridge) {
    return;
  }
  reportRendererDiagnostic(
    diagnosticsBridge,
    "warn",
    "control.settings_snapshot_rejected",
    { reason_code: "invalid_settings_snapshot" }
  );
}
