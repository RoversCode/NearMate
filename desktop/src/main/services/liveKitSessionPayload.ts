/**
 * LiveKitSessionParticipant 表示后端为单个桌面参与者签发的身份与令牌。
 * 核心入参来自 /api/livekit/session 的嵌套字段；预期输出供主进程 IPC handler 组合给 renderer。
 * 边界：identity 与 token 都必须是非空字符串，缺失时说明后端响应不完整，不能继续建立房间连接。
 */
type LiveKitSessionParticipant = {
  identity: string;
  token: string;
};

/**
 * AecSidecarDebugAudioConfig 表示后端下发给桌面主进程的 sidecar 连续调试录音策略。
 * 核心入参来自 `/api/livekit/session` 的 aec_sidecar_debug_audio 字段；预期输出供 bootstrap 决定是否给 Python sidecar 追加调试参数。
 * 边界：这里只有 main 进程可以读取它，renderer 不应直接接触连续录音目录或保留策略。
 */
type AecSidecarDebugAudioConfig = {
  enabled: boolean;
  outputDir: string;
  maxRuns: number;
};

type AudioDeviceSelection = {
  mode: "system_default" | "manual";
  microphoneId: string | null;
  microphoneName: string | null;
  speakerLoopbackId: string | null;
  speakerLoopbackName: string | null;
};

/**
 * LiveKitDesktopSession 是主进程内部使用的标准 session 结构。
 * 它把后端 snake_case 字段转换为 TypeScript 侧 camelCase，避免后续主进程逻辑直接依赖 HTTP payload 细节。
 */
export type LiveKitDesktopSession = {
  url: string;
  roomName: string;
  desktopUi: LiveKitSessionParticipant;
  desktopAec: LiveKitSessionParticipant;
  aecSidecarDebugAudio: AecSidecarDebugAudioConfig;
  audioDeviceSelection: AudioDeviceSelection;
};

/**
 * BackendLiveKitSessionPayload 描述后端原始响应的最小形态。
 * 这里保留 unknown 入口，是为了让解析函数在运行时显式校验字段，而不是信任 JSON 反序列化后的结构。
 */
export type BackendLiveKitSessionPayload = {
  url?: unknown;
  room_name?: unknown;
  desktop_ui?: unknown;
  desktop_aec?: unknown;
  aec_sidecar_debug_audio?: unknown;
  audio_devices?: unknown;
};

/**
 * DISABLED_AEC_SIDECAR_DEBUG_AUDIO_CONFIG 是缺少 sidecar 调试配置时的兼容回退值。
 * 业务意图是让旧后端或测试替身在未透传该字段时，主进程仍以“关闭连续录音”的安全默认值继续建立 LiveKit session。
 */
const DISABLED_AEC_SIDECAR_DEBUG_AUDIO_CONFIG: AecSidecarDebugAudioConfig = {
  enabled: false,
  outputDir: "",
  maxRuns: 10
};

const SYSTEM_DEFAULT_AUDIO_DEVICE_SELECTION: AudioDeviceSelection = {
  mode: "system_default",
  microphoneId: null,
  microphoneName: null,
  speakerLoopbackId: null,
  speakerLoopbackName: null
};

/**
 * readLiveKitDesktopSession 把后端原始 JSON 转为主进程内部 session 结构。
 * 核心入参是未知可信度的 JSON object；预期输出是字段完整的 LiveKitDesktopSession。
 * 边界：url、room_name、desktop_ui、desktop_aec 任何关键字段缺失时都会抛出明确错误；可选调试与设备字段缺失时会安全回退。
 */
export function readLiveKitDesktopSession(
  payload: BackendLiveKitSessionPayload
): LiveKitDesktopSession {
  /**
   * LiveKit 服务地址和房间名是两端连接同一个房间的基础坐标。
   * 缺少任一字段都无法建立连接，因此要在主进程边界立即失败。
   */
  if (
    typeof payload.url !== "string" ||
    typeof payload.room_name !== "string" ||
    payload.url.trim().length === 0 ||
    payload.room_name.trim().length === 0
  ) {
    throw new Error("LiveKit session response is missing url or room_name.");
  }

  /**
   * 两个桌面参与者必须同时存在。
   * desktop_ui 供头像 UI 连接，desktop_aec 预留给桌面回声消除链路；只返回一半会让后续 Task 6 无法可靠接线。
   */
  const desktopUi = readLiveKitSessionParticipant(payload.desktop_ui);
  const desktopAec = readLiveKitSessionParticipant(payload.desktop_aec);
  const aecSidecarDebugAudio = readAecSidecarDebugAudioConfig(payload.aec_sidecar_debug_audio);
  const audioDeviceSelection = readAudioDeviceSelection(payload.audio_devices);

  if (!desktopUi || !desktopAec) {
    throw new Error("LiveKit session response is missing desktop_ui or desktop_aec.");
  }
  if (!aecSidecarDebugAudio) {
    throw new Error("LiveKit session response is missing aec_sidecar_debug_audio.");
  }
  if (!audioDeviceSelection) {
    throw new Error("LiveKit session response is missing audio_devices.");
  }

  return {
    url: payload.url,
    roomName: payload.room_name,
    desktopUi,
    desktopAec,
    aecSidecarDebugAudio,
    audioDeviceSelection
  };
}

/**
 * readLiveKitSessionParticipant 从嵌套对象中读取单个参与者身份。
 * 核心入参是后端 desktop_ui 或 desktop_aec 字段；预期输出是合法 identity/token 组合或 null。
 * 边界：非对象、空字符串或字段类型错误都返回 null，由上层统一抛出面向 session 响应的错误信息。
 */
function readLiveKitSessionParticipant(
  payload: unknown
): LiveKitSessionParticipant | null {
  /**
   * 参与者字段必须是对象。
   * 其他类型无法安全读取 identity/token，直接返回 null 让上层按缺字段处理。
   */
  if (!payload || typeof payload !== "object") {
    return null;
  }

  /**
   * candidate 是经过对象检查后的窄化视图。
   * 这里只读取需要的两个字段，不把后端额外字段泄漏进主进程内部结构。
   */
  const candidate = payload as Partial<LiveKitSessionParticipant>;

  if (
    typeof candidate.identity !== "string" ||
    typeof candidate.token !== "string" ||
    candidate.identity.trim().length === 0 ||
    candidate.token.trim().length === 0
  ) {
    return null;
  }

  return {
    identity: candidate.identity,
    token: candidate.token
  };
}

/**
 * readAecSidecarDebugAudioConfig 从后端响应中读取 sidecar 连续调试录音配置。
 * 核心入参是 aec_sidecar_debug_audio 原始 payload；预期输出是字段完整且类型安全的 camelCase 配置，或在无效时返回 null。
 * 边界：字段完全缺失时回退到默认关闭配置；若显式启用，则 output_dir 必须是非空字符串，max_runs 必须是正整数。
 */
function readAecSidecarDebugAudioConfig(
  payload: unknown
): AecSidecarDebugAudioConfig | null {
  /**
   * 缺失字段代表旧后端尚未透传 sidecar 调试配置。
   * 这里回退到默认关闭，保证正式会话链路不会因为新增可选调试能力而失败。
   */
  if (payload === undefined) {
    return DISABLED_AEC_SIDECAR_DEBUG_AUDIO_CONFIG;
  }

  /**
   * sidecar 调试配置必须是对象。
   * 主进程只接受后端明确签发的结构化配置，避免把 null、数组或字符串误当成“关闭开关”的合法形态。
   */
  if (!payload || typeof payload !== "object") {
    return null;
  }

  /**
   * candidate 是后端 sidecar 调试配置的窄化视图。
   * 这里只读取主进程真正需要的三个字段，不把未声明字段泄漏到内部运行时。
   */
  const candidate = payload as {
    enabled?: unknown;
    output_dir?: unknown;
    max_runs?: unknown;
  };

  if (typeof candidate.enabled !== "boolean") {
    return null;
  }

  /**
   * sidecar 调试录音关闭时，output_dir 和 max_runs 即使缺失也不会被主进程使用。
   * 这里直接回退到默认关闭配置，降低前后端兼容期与测试替身的脆弱性。
   */
  if (!candidate.enabled) {
    return DISABLED_AEC_SIDECAR_DEBUG_AUDIO_CONFIG;
  }

  if (
    typeof candidate.output_dir !== "string" ||
    candidate.output_dir.trim().length === 0
  ) {
    return null;
  }
  if (
    typeof candidate.max_runs !== "number" ||
    !Number.isInteger(candidate.max_runs) ||
    candidate.max_runs <= 0
  ) {
    return null;
  }

  return {
    enabled: candidate.enabled,
    outputDir: candidate.output_dir,
    maxRuns: candidate.max_runs
  };
}

/**
 * readAudioDeviceSelection 从后端响应中读取桌面音频设备选择。
 * 核心入参是 audio_devices 原始 payload；预期输出是系统默认或手动设备选择。
 * 边界：字段缺失回退系统默认，显式字段非法则返回 null 让上层抛出稳定响应结构错误。
 */
function readAudioDeviceSelection(payload: unknown): AudioDeviceSelection | null {
  /**
   * 缺失字段代表旧后端尚未透传设备选择。
   * 这里回退到系统默认，保持旧链路行为，同时新后端会明确下发用户保存的设置。
   */
  if (payload === undefined) {
    return SYSTEM_DEFAULT_AUDIO_DEVICE_SELECTION;
  }

  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as {
    mode?: unknown;
    microphone_id?: unknown;
    microphone_name?: unknown;
    speaker_loopback_id?: unknown;
    speaker_loopback_name?: unknown;
  };

  if (candidate.mode !== "system_default" && candidate.mode !== "manual") {
    return null;
  }

  if (
    !isNullableString(candidate.microphone_id) ||
    !isNullableString(candidate.microphone_name) ||
    !isNullableString(candidate.speaker_loopback_id) ||
    !isNullableString(candidate.speaker_loopback_name)
  ) {
    return null;
  }

  return {
    mode: candidate.mode,
    microphoneId: candidate.microphone_id,
    microphoneName: candidate.microphone_name,
    speakerLoopbackId: candidate.speaker_loopback_id,
    speakerLoopbackName: candidate.speaker_loopback_name
  };
}

/**
 * isNullableString 判断后端可选设备字段是否为字符串或 null。
 * 核心入参是不可信字段值；预期输出是可安全写入内部设备选择结构的类型收窄结果。
 */
function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
