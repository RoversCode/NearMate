import { fetchLiveKitSessionPayload } from "../../../shared/liveKitSessionRequest";

type LiveKitSessionPayload = {
  url: string;
  room_name: string;
  desktop_ui: RendererLiveKitParticipant;
  audio_input_status?: "available" | "audio_unavailable";
};

type RendererLiveKitSession = {
  url: string;
  roomName: string;
  connection: RendererLiveKitParticipant;
  audioInputStatus: "available" | "audio_unavailable";
};

/**
 * BackendLiveKitSessionPayload 描述 renderer 可能拿到的后端原始 session 形态。
 * 核心入参来自 Electron main 返回值或非 Electron HTTP 边界；预期输出交给归一化函数剥离成 UI-only payload。
 * 边界异常：字段保持 unknown，避免 renderer 在解析前信任后端附带的 desktop_aec 敏感结构。
 */
type BackendLiveKitSessionPayload = {
  url?: unknown;
  room_name?: unknown;
  desktop_ui?: unknown;
  desktop_aec?: unknown;
  audio_input_status?: unknown;
};

/**
 * BackendLiveKitParticipantPayload 描述嵌套参与者字段的最小可读结构。
 * 核心入参是 desktop_ui；预期输出只读取 renderer 连接房间需要的 identity/token。
 * 边界异常：desktop_aec 即使存在也不会被读取，避免 renderer 使用或暂存 AEC sidecar token。
 */
type BackendLiveKitParticipantPayload = {
  identity?: unknown;
  token?: unknown;
};

/**
 * RendererLiveKitParticipant 表示 renderer 允许使用的单个 UI participant。
 * 核心入参来自 desktop_ui；预期输出只承载 UI 身份与 UI token。
 * 边界异常：AEC sidecar participant 不使用这个类型，避免敏感 token 在 renderer 类型层面获得合法落点。
 */
type RendererLiveKitParticipant = {
  identity: string;
  token: string;
};

/**
 * 通过非 Electron 环境的 HTTP 边界获取 LiveKit session。
 * 核心入参是控制服务 HTTP 根地址和可取消 signal；预期输出是 renderer 进房所需 session 载荷。
 * 边界异常：请求和正文解析共享超时预算，非 2xx 响应只展示白名单错误分类。
 */
export async function fetchLiveKitSession(
  httpBaseUrl: string,
  signal: AbortSignal
): Promise<unknown> {
  return fetchLiveKitSessionPayload({ url: `${httpBaseUrl}/api/livekit/session`, signal });
}

/**
 * 回传音频设备热更新结果。
 * 核心入参是控制服务根地址、请求编号和应用状态；预期输出是后端收到结果并广播给 Console。
 * 边界异常：接口不可达或非 2xx 时抛出稳定错误，由调用方转成诊断，不泄露 LiveKit token。
 */
export async function reportAudioDeviceApplyResult(
  httpBaseUrl: string,
  requestId: string,
  status: "applied" | "failed",
  reason?: string
): Promise<void> {
  const body: {
    request_id: string;
    status: "applied" | "failed";
    reason?: string;
  } = {
    request_id: requestId,
    status
  };

  if (reason && reason.trim()) {
    body.reason = reason.trim();
  }

  const response = await fetch(`${httpBaseUrl}/api/audio/devices/apply-result`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new Error(`audio device apply-result request failed: ${response.status}`);
  }
}

/**
 * normalizeRendererLiveKitSession 把可能包含 desktop_aec 的 session 响应压缩为 renderer 连接配置。
 * 核心入参是 preload/main 或 HTTP 边界返回的未知 JSON；预期输出只包含房间地址、房间名和 UI 参与者凭据。
 * 边界异常：缺少 UI 连接必需字段时抛出稳定错误；desktop_aec 永远不会进入返回值，防止 AEC token 在 renderer 链路中继续传播。
 */
export function normalizeRendererLiveKitSession(payload: unknown): RendererLiveKitSession {
  /**
   * session 响应必须是对象。
   * 非对象无法安全读取 LiveKit 地址或 UI 参与者，直接失败比传递 undefined token 更容易诊断。
   */
  if (!payload || typeof payload !== "object") {
    throw new Error("LiveKit session response is missing renderer UI credentials.");
  }

  /**
   * candidate 是运行时校验后的后端响应视图。
   * 这里只读取 UI 所需字段，刻意不读取 desktop_aec，确保 HTTP 边界也不会使用 sidecar token。
   */
  const candidate = payload as BackendLiveKitSessionPayload;
  const desktopUi = readRendererLiveKitParticipant(candidate.desktop_ui);

  if (
    typeof candidate.url !== "string" ||
    typeof candidate.room_name !== "string" ||
    candidate.url.trim().length === 0 ||
    candidate.room_name.trim().length === 0 ||
    desktopUi === null
  ) {
    throw new Error("LiveKit session response is missing renderer UI credentials.");
  }

  return {
    url: candidate.url,
    roomName: candidate.room_name,
    connection: desktopUi,
    audioInputStatus: candidate.audio_input_status === "audio_unavailable" ? "audio_unavailable" : "available"
  };
}

/**
 * readRendererLiveKitParticipant 读取嵌套 UI participant。
 * 核心入参是 desktop_ui 字段；预期输出是 renderer 可连接房间的 identity/token 或 null。
 * 边界异常：非对象、空白 identity 或空白 token 都按缺失处理，由上层统一抛出 session 结构错误。
 */
function readRendererLiveKitParticipant(payload: unknown): RendererLiveKitParticipant | null {
  /**
   * 参与者字段必须是对象。
   * 这一步阻止字符串、null 或数组被当成合法身份来源。
   */
  if (!payload || typeof payload !== "object") {
    return null;
  }

  /**
   * candidate 只读取 identity/token 两个 UI 连接字段。
   * 任何额外字段都不会进入 renderer 运行时结构。
   */
  const candidate = payload as BackendLiveKitParticipantPayload;

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
 * 判断异常是否来自 AbortController 取消。
 * 核心入参是未知错误对象；预期输出是是否可以被视为正常卸载取消路径。
 */
export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException) {
    return error.name === "AbortError";
  }

  return error instanceof Error && error.name === "AbortError";
}

export type { LiveKitSessionPayload, RendererLiveKitSession };
