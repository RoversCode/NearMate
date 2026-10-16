import { type ConsoleSettings, isConsoleSettings, normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, postJson } from "../httpClient";

/**
 * 从本地控制面读取当前设置。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址，不包含尾随接口路径。
 *
 * Returns:
 *   /api/settings 的控制台设置快照。
 */
export async function fetchSettings(baseUrl: string): Promise<ConsoleSettings> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/settings", "读取设置失败");

  // 后端响应是外部输入，即使来自本机也要校验关键字段，避免 UI 在异常数据下静默显示错误状态。
  if (!isConsoleSettings(payload)) {
    throw new Error("读取设置失败：响应格式不符合控制台契约");
  }

  return normalizeConsoleSettings(payload);
}

/**
 * 保存本地控制台设置。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   settings: 控制台当前持有的设置字段投影。
 *
 * Returns:
 *   从 Backend 提交结果中校验并归一化后的 Console 设置投影。
 */
export async function updateSettings(baseUrl: string, settings: ConsoleSettings): Promise<ConsoleSettings> {
  const settingsPayload = {
    expected_revision: settings.settings_revision,
    show_speech_bubbles: settings.show_speech_bubbles,
    followup_toggle_shortcut_enabled: settings.followup_toggle_shortcut_enabled,
    followup_toggle_shortcut: settings.followup_toggle_shortcut,
    avatar_visibility_shortcut_enabled: settings.avatar_visibility_shortcut_enabled,
    avatar_visibility_shortcut: settings.avatar_visibility_shortcut,
    selection_capture_shortcut_enabled: settings.selection_capture_shortcut_enabled,
    selection_capture_shortcut: settings.selection_capture_shortcut,
    selected_live2d_model_id: settings.selected_live2d_model_id,
    user_self_intro: settings.user_self_intro,
    user_extra_notes: settings.user_extra_notes
  };

  // 普通设置接口只拥有上面的十个字段；其他设置各走自己的接口，不能被完整 GET 快照顺带覆盖。
  const payload = await postJson<unknown>(baseUrl, "/api/settings", settingsPayload, "保存设置失败");

  // 保存响应决定下一次乐观锁版本，格式不完整时不能继续使用本地猜测快照。
  if (!isConsoleSettings(payload)) {
    throw new Error("保存设置失败：响应格式不符合控制台契约");
  }

  return normalizeConsoleSettings(payload);
}
