/**
 * 描述 LLM 初始化门禁状态。
 *
 * 核心字段：is_ready 表示全部必需 LLM 职责是否可运行，missing 与 errors 用于展示阻塞原因。
 * 预期用途：首次启动页和设置页用它提示用户补齐模型配置。
 * 边界异常：状态只包含缺失摘要，不承载任何明文 API Key。
 */
type LlmBootstrapStatus = {
  is_ready: boolean;
  missing: string[];
  errors: string[];
};

/**
 * 描述当前角色语音配置的门禁状态。
 *
 * 核心字段：is_ready 表示角色语音是否可运行，phase 描述配置进度，model_id 与 role_name 用于展示绑定关系。
 * 预期用途：首次启动页和设置页用它提示用户完成语音准备。
 * 边界异常：状态只包含安全摘要，不承载模型文件路径等本地敏感信息。
 */
type VoiceBootstrapStatus = {
  provider: string;
  is_ready: boolean;
  phase: string;
  model_id: string | null;
  role_name: string | null;
  /** 以前是否已经确认过角色语音；语音重配置期间用于恢复管理页。 */
  has_confirmed_voice: boolean;
};

/**
 * 受管模型的下载状态和当前选用关系；文件已下载不表示模型正在运行。
 *
 * Args:
 *   不适用；这是模型目录接口的字段定义。
 * Returns:
 *   local_state 为 unknown 时尚未取得磁盘状态，不能显示成未下载。
 */
export type TtsModelDescriptor = {
  model_id: string;
  display_name: string;
  repository: string | null;
  estimated_size_bytes: number | null;
  verification_status: string;
  sdk_version?: string;
  bundle_version?: string | null;
  manifest_sha256?: string | null;
  download_available: boolean;
  backend_note?: string | null;
  local_state?: "unknown" | "not_downloaded" | "incomplete" | "downloaded" | "unavailable";
  installed_size_bytes?: number | null;
  total_size_bytes?: number | null;
  is_current?: boolean;
  integrity_status?: "verified" | "not_verified";
};

/** 受管 TTS 配置任务的安全描述。 */
export type TtsSetupTask = {
  task_id: string | null;
  model_id: string | null;
  phase: string;
  progress: {
    phase: string;
    completed_bytes: number;
    total_bytes: number | null;
    current_file?: string | null;
    fraction?: number | null;
  } | null;
  error: string | null;
  can_cancel: boolean;
  can_confirm: boolean;
  can_retry: boolean;
  is_ready?: boolean;
  role_name?: string | null;
  actual_backends?: Record<string, string> | null;
};

/**
 * 描述本地控制台首次初始化状态。
 *
 * 核心字段：llm 与 voice 表示模型配置和角色语音是否满足，has_qweather_api_key 表示可选天气密钥是否已保存。
 * 预期用途：App 启动时用它决定渲染首次初始化页还是加载普通数据。
 * 边界异常：该类型仅描述后端已校验的状态载荷，不承载原始密钥，避免前端意外泄露敏感信息。
 */
export type BootstrapStatus = {
  llm: LlmBootstrapStatus;
  voice: VoiceBootstrapStatus;
  has_qweather_api_key: boolean;
  is_initialized: boolean;
};

/**
 * 描述启动服务密钥提交载荷。
 *
 * 核心字段：qweather_api_key 与 qweather_api_host 是按需配置的天气服务凭据。
 * 预期用途：首次初始化页和设置页去除首尾空白后提交给 initializeBootstrap。
 * 边界异常：空字符串由页面禁用提交并由后端兜底校验，本类型不额外表达可选状态。
 */
export type BootstrapInitializePayload = {
  qweather_api_key?: string | null;
  qweather_api_host?: string | null;
};

/**
 * 描述和风天气服务配置单独提交载荷。
 *
 * 核心字段：qweather_api_key 与 qweather_api_host 是用户在服务密钥面板填写的和风天气服务配置。
 * 预期用途：设置页单独验证并保存可选天气服务配置。
 * 边界异常：空字符串由页面禁用提交并由后端兜底校验。
 */
export type QWeatherApiKeyPayload = {
  qweather_api_key: string;
  qweather_api_host: string;
};

/**
 * 判断未知响应是否符合首次初始化状态契约。
 *
 * Args:
 *   payload: /api/bootstrap/status 的 JSON 解析结果。
 *
 * Returns:
 *   llm、voice、has_qweather_api_key、is_initialized 四个契约键全部存在且类型正确时返回 true。
 */
export function isBootstrapStatus(payload: unknown): payload is BootstrapStatus {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  const topLevelKeys = ["llm", "voice", "has_qweather_api_key", "is_initialized"];
  if (Object.keys(candidate).length !== topLevelKeys.length || !topLevelKeys.every((key) => Object.hasOwn(candidate, key))) {
    return false;
  }
  const llm = candidate.llm;

  if (llm === null || typeof llm !== "object" || Array.isArray(llm)) {
    return false;
  }

  const llmStatus = llm as Record<string, unknown>;

  return (
    Object.keys(llmStatus).length === 3 &&
    ["is_ready", "missing", "errors"].every((key) => Object.hasOwn(llmStatus, key)) &&
    typeof llmStatus.is_ready === "boolean" &&
    Array.isArray(llmStatus.missing) &&
    llmStatus.missing.every((item) => typeof item === "string") &&
    Array.isArray(llmStatus.errors) &&
    llmStatus.errors.every((item) => typeof item === "string") &&
    isVoiceStatus(candidate.voice) &&
    typeof candidate.has_qweather_api_key === "boolean" &&
    typeof candidate.is_initialized === "boolean"
  );
}

/**
 * 校验角色语音门禁状态的字段形状。
 *
 * Args:
 *   value: 状态载荷中的 voice 键值。
 *
 * Returns:
 *   六个核心字段齐全且类型正确时返回 true。
 */
function isVoiceStatus(value: unknown): value is VoiceBootstrapStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  const requiredKeys = ["provider", "is_ready", "phase", "model_id", "role_name", "has_confirmed_voice"];
  if (Object.keys(candidate).length !== requiredKeys.length ||
      !requiredKeys.every((key) => Object.hasOwn(candidate, key))) return false;
  return typeof candidate.provider === "string" &&
    typeof candidate.is_ready === "boolean" &&
    typeof candidate.phase === "string" &&
    (candidate.model_id === null || typeof candidate.model_id === "string") &&
    (candidate.role_name === null || typeof candidate.role_name === "string") &&
    typeof candidate.has_confirmed_voice === "boolean";
}
