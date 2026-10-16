import {
  type SpeakerVerificationProfile,
  type SpeakerVerificationSamplePayload,
  isSpeakerVerificationProfile
} from "../contracts/speakerVerification";
import { normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, fetchJsonWithInit, postJson, readHttpErrorDetail } from "../httpClient";

/**
 * 读取说话人确认白名单状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   开关、录音样本和开启前置条件。
 */
export async function fetchSpeakerVerificationProfile(baseUrl: string): Promise<SpeakerVerificationProfile> {
  const payload = await fetchJson<unknown>(
    baseUrl,
    "/api/speaker-verification/profile",
    "读取说话人确认状态失败"
  );

  if (!isSpeakerVerificationProfile(payload)) {
    throw new Error("读取说话人确认状态失败：响应格式不符合控制台契约");
  }

  return normalizeSpeakerVerificationProfileSettings(payload);
}

/**
 * 保存说话人确认开关。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   enabled: 目标开关状态。
 *
 * Returns:
 *   后端保存后的 profile。
 */
export async function updateSpeakerVerificationProfile(
  baseUrl: string,
  enabled: boolean
): Promise<SpeakerVerificationProfile> {
  const payload = await postJson<unknown>(
    baseUrl,
    "/api/speaker-verification/profile",
    { enabled },
    "保存说话人确认状态失败"
  );

  if (!isSpeakerVerificationProfile(payload)) {
    throw new Error("保存说话人确认状态失败：响应格式不符合控制台契约");
  }

  return normalizeSpeakerVerificationProfileSettings(payload);
}

/**
 * 保存或替换一段后端录音会话生成的说话人白名单录音。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   payload: 后端 VAD 完成事件返回的录音会话编号，可指定替换槽位。
 *
 * Returns:
 *   更新后的 profile。
 */
export async function createSpeakerVerificationSample(
  baseUrl: string,
  payload: SpeakerVerificationSamplePayload
): Promise<SpeakerVerificationProfile> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");
  const formData = new FormData();
  formData.append("recording_id", payload.recording_id);
  if (payload.replace_sample_id !== undefined && payload.replace_sample_id !== null) {
    formData.append("replace_sample_id", payload.replace_sample_id);
  }

  const response = await fetch(`${normalizedBaseUrl}/api/speaker-verification/samples`, {
    method: "POST",
    body: formData
  });

  if (!response.ok) {
    throw new Error(`保存说话人白名单录音失败：${await readHttpErrorDetail(response)}`);
  }

  const responsePayload: unknown = await response.json();
  if (!isSpeakerVerificationProfile(responsePayload)) {
    throw new Error("保存说话人白名单录音失败：响应格式不符合控制台契约");
  }
  return normalizeSpeakerVerificationProfileSettings(responsePayload);
}

/**
 * 删除一段说话人白名单录音。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   sampleId: 后端录音槽位编号。
 *
 * Returns:
 *   删除后的 profile。
 */
export async function deleteSpeakerVerificationSample(
  baseUrl: string,
  sampleId: string
): Promise<SpeakerVerificationProfile> {
  const encodedSampleId = encodeURIComponent(sampleId);
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/speaker-verification/samples/${encodedSampleId}`,
    {
      method: "DELETE"
    },
    "删除说话人白名单录音失败"
  );

  if (!isSpeakerVerificationProfile(payload)) {
    throw new Error("删除说话人白名单录音失败：响应格式不符合控制台契约");
  }

  return normalizeSpeakerVerificationProfileSettings(payload);
}

/**
 * 复制说话人确认响应中的可选统一设置快照。
 *
 * Args:
 *   payload: 已通过说话人确认 profile 契约校验的响应。
 *

 * Returns:
 *   保留 profile 字段，并复制规范化设置快照；旧 Backend 没有时原样返回。
 */
function normalizeSpeakerVerificationProfileSettings(
  payload: SpeakerVerificationProfile
): SpeakerVerificationProfile {
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}
