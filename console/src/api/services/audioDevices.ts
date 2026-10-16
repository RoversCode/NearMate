import {
  type AudioDeviceSelectionPayload,
  type AudioDeviceSettingsUpdateResult,
  type AudioDeviceTestPayload,
  type AudioDeviceTestResult,
  type AudioDevicesPayload,
  isAudioDeviceSettingsUpdateResult,
  isAudioDeviceTestResult,
  isAudioDevicesPayload
} from "../contracts/audioDevices";
import { isConsoleSettings, normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, postJson } from "../httpClient";

/**
 * 读取本机音频设备和探测状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   麦克风、扬声器参考和当前选择。
 */
export async function fetchAudioDevices(baseUrl: string): Promise<AudioDevicesPayload> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/audio/devices", "读取音频设备失败");
  if (!isAudioDevicesPayload(payload)) {
    throw new Error("读取音频设备失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 测试指定麦克风能否打开。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 待测试的麦克风选择。
 *
 * Returns:
 *   麦克风测试结果。
 */
export async function testAudioDevice(baseUrl: string, payload: AudioDeviceTestPayload): Promise<AudioDeviceTestResult> {
  const responsePayload = await postJson<unknown>(baseUrl, "/api/audio/devices/test", payload, "测试麦克风失败");
  if (!isAudioDeviceTestResult(responsePayload)) {
    throw new Error("测试麦克风失败：响应格式不符合控制台契约");
  }
  return responsePayload;
}

/**
 * 保存音频设备选择。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 用户选择的音频设备。
 *
 * Returns:
 *   后端接受设备切换请求的结果。
 */
export async function updateAudioDeviceSettings(baseUrl: string, payload: AudioDeviceSelectionPayload): Promise<AudioDeviceSettingsUpdateResult> {
  const responsePayload = await postJson<unknown>(baseUrl, "/api/settings/audio-devices", payload, "保存音频设备失败");
  if (
    !isAudioDeviceSettingsUpdateResult(responsePayload) ||
    (responsePayload.settings !== undefined && !isConsoleSettings(responsePayload.settings))
  ) {
    throw new Error("保存音频设备失败：响应格式不符合控制台契约");
  }
  return responsePayload.settings === undefined
    ? responsePayload
    : {
        ...responsePayload,
        settings: normalizeConsoleSettings(responsePayload.settings)
      };
}
