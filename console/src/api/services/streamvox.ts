import {
  type StreamVoxConfigPayload,
  type StreamVoxCreateRolePayload,
  type StreamVoxPreviewRolePayload,
  type StreamVoxRuntimeState,
  isStreamVoxRuntimeState
} from "../contracts/streamvox";
import { normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, fetchJsonWithInit, postJson, readHttpErrorDetail } from "../httpClient";

/**
 * 读取 StreamVox 当前运行状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   助手子进程内唯一 StreamVox provider 的状态快照。
 */
export async function fetchStreamVoxRuntime(baseUrl: string): Promise<StreamVoxRuntimeState> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/streamvox/runtime", "读取 StreamVox 状态失败");

  // StreamVox 状态来自后端运行时边界，仍按外部输入严格校验，避免 UI 展示不完整角色列表。
  if (!isStreamVoxRuntimeState(payload)) {
    throw new Error("读取 StreamVox 状态失败：响应格式不符合控制台契约");
  }

  return normalizeStreamVoxRuntimeSettings(payload);
}

/**
 * 保存并切换 StreamVox 运行配置。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   payload: 目标模型、设备和角色。
 *
 * Returns:
 *   最新 runtime 状态。
 */
export async function updateStreamVoxConfig(
  baseUrl: string,
  payload: StreamVoxConfigPayload
): Promise<StreamVoxRuntimeState> {
  const responsePayload = await postJson<unknown>(
    baseUrl,
    "/api/streamvox/config",
    payload,
    "保存 StreamVox 配置失败"
  );

  if (!isStreamVoxRuntimeState(responsePayload)) {
    throw new Error("保存 StreamVox 配置失败：响应格式不符合控制台契约");
  }

  return normalizeStreamVoxRuntimeSettings(responsePayload);
}

/**
 * 删除当前模型下的指定 StreamVox 角色。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   roleName: 待删除角色名。
 *
 * Returns:
 *   删除后的 runtime 状态。
 */
export async function deleteStreamVoxRole(baseUrl: string, roleName: string): Promise<StreamVoxRuntimeState> {
  const encodedRoleName = encodeURIComponent(roleName);
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/streamvox/roles/${encodedRoleName}`,
    {
      method: "DELETE"
    },
    "删除 StreamVox 音色失败"
  );

  if (!isStreamVoxRuntimeState(payload)) {
    throw new Error("删除 StreamVox 音色失败：响应格式不符合控制台契约");
  }

  return normalizeStreamVoxRuntimeSettings(payload);
}

/**
 * 创建当前 StreamVox 模型下的自定义音色。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   payload: 角色名、参考文本和上传音频文件。
 *
 * Returns:
 *   创建后的 runtime 状态。
 */
export async function createStreamVoxRole(
  baseUrl: string,
  payload: StreamVoxCreateRolePayload
): Promise<StreamVoxRuntimeState> {
  const formData = new FormData();
  formData.append("role_name", payload.role_name);
  formData.append("prompt_text", payload.prompt_text);
  formData.append("audio_file", payload.audio_file);

  // FormData 请求必须让浏览器自动设置 multipart boundary；这里刻意不传 headers。
  const responsePayload = await fetchJsonWithInit<unknown>(
    baseUrl,
    "/api/streamvox/roles",
    {
      method: "POST",
      body: formData
    },
    "创建 StreamVox 音色失败"
  );
  if (!isStreamVoxRuntimeState(responsePayload)) {
    throw new Error("创建 StreamVox 音色失败：响应格式不符合控制台契约");
  }
  return normalizeStreamVoxRuntimeSettings(responsePayload);
}

/**
 * 试听当前模型下已创建角色的推理声音。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   payload: 指定角色和试听文本。
 *
 * Returns:
 *   浏览器可播放的 audio/wav Blob。
 */
export async function previewStreamVoxRole(
  baseUrl: string,
  payload: StreamVoxPreviewRolePayload
): Promise<Blob> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");
  const encodedRoleName = encodeURIComponent(payload.role_name);
  const response = await fetch(`${normalizedBaseUrl}/api/streamvox/roles/${encodedRoleName}/preview`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      text: payload.text
    })
  });

  if (!response.ok) {
    throw new Error(`试听 StreamVox 音色失败：${await readHttpErrorDetail(response)}`);
  }
  return response.blob();
}

/**
 * 复制 StreamVox 响应中的可选统一设置快照。
 *
 * Args:
 *   payload: 已通过 StreamVox runtime 契约校验的响应。

 * Returns:
 *   保留 runtime 字段，并复制规范化设置快照；旧 Backend 没有时原样返回。
 */
function normalizeStreamVoxRuntimeSettings(payload: StreamVoxRuntimeState): StreamVoxRuntimeState {
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}
