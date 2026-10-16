import { isPlainObject } from "./validation";
import { isConsoleSettings, type ConsoleSettings } from "./settings";

/**
 * 描述 StreamVox 控制台运行状态。
 *
 * 核心字段：model/device/llama_backend/role_name 表示当前助手子进程实际使用的模型、设备、GGUF 后端和音色，sample_rate 来自 StreamVox runtime，
 * roles 是当前模型下已注册的角色列表。
 * 预期用途：设置页展示模型与音色管理状态，并在操作后刷新为后端事实源。
 * 边界异常：读取时缺少任意字段会被 isStreamVoxRuntimeState 拒绝，避免 UI 展示伪状态。
 */
export type StreamVoxRuntimeState = {
  model: string;
  device: string;
  llama_backend: string | null;
  role_name: string;
  streamvox_seed: number | null;
  inference_options: Record<string, unknown>;
  sample_rate: number;
  roles: string[];
  /** 配置切换成功时返回的统一设置提交点快照；旧 Backend 可省略。 */
  settings?: ConsoleSettings;
};

/**
 * 描述 StreamVox 运行配置保存载荷。
 *
 * 核心字段：model 是目标模型，device 是目标设备，llama_backend 是 GGUF 后端策略，role_name 是当前模型下要使用的角色名。
 * 预期用途：设置页保存语音模型时提交给 /api/streamvox/config。
 * 边界异常：空值由页面禁用和后端校验共同处理，本类型只表达 wire shape。
 */
export type StreamVoxConfigPayload = {
  model: string;
  device: string;
  llama_backend: string | null;
  role_name: string;
  streamvox_seed: number | null;
  inference_options: Record<string, unknown>;
};

/**
 * 描述 StreamVox 自定义角色创建载荷。
 *
 * 核心字段：role_name 是当前模型下的新角色名，prompt_text 是参考音频文本，audio_file 是浏览器上传文件。
 * 预期用途：设置页使用 FormData 提交给后端 multipart 接口。
 * 边界异常：空值由页面禁用和后端校验共同处理，本类型只表达调用契约。
 */
export type StreamVoxCreateRolePayload = {
  role_name: string;
  prompt_text: string;
  audio_file: File;
};

/**
 * 描述 StreamVox 角色推理试听载荷。
 *
 * 核心字段：role_name 是已创建角色名，text 是要合成的试听文本。
 * 预期用途：设置页点击角色试听按钮后调用 preview API 并播放返回 Blob。
 * 边界异常：只试听已创建角色；角色不存在由后端运行时返回错误。
 */
export type StreamVoxPreviewRolePayload = {
  role_name: string;
  text: string;
};

/**
 * 判断未知响应是否符合 StreamVox runtime 状态契约。
 *
 * Args:
 *   payload: JSON 解析后的未知值。
 *
 * Returns:
 *   所有关键字段类型正确时返回 true。
 */
export function isStreamVoxRuntimeState(payload: unknown): payload is StreamVoxRuntimeState {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.model === "string" &&
    typeof payload.device === "string" &&
    (payload.llama_backend === null || typeof payload.llama_backend === "string") &&
    typeof payload.role_name === "string" &&
    (payload.streamvox_seed === null || typeof payload.streamvox_seed === "number") &&
    isPlainObject(payload.inference_options) &&
    typeof payload.sample_rate === "number" &&
    Array.isArray(payload.roles) &&
    payload.roles.every((role) => typeof role === "string") &&
    (payload.settings === undefined || isConsoleSettings(payload.settings))
  );
}
