/** 媒体会话的公开错误只使用固定文案，避免后端正文或 SDK 异常带出凭据。 */
const LIVEKIT_FAILURES = {
  livekit_not_configured: { message: "语音服务尚未配置，请重新安装完整版本后重试。", retryable: false },
  livekit_connection_failed: { message: "语音连接暂时不可用，正在自动重试。", retryable: true },
  assistant_not_ready: { message: "助手正在准备，请稍候，连接会自动恢复。", retryable: true },
  livekit_request_timeout: { message: "语音连接等待超时，正在自动重试。", retryable: true },
  livekit_invalid_response: { message: "语音服务响应异常，请检查安装版本。", retryable: false },
  livekit_access_denied: { message: "语音连接未获许可，请先完成授权和初始化。", retryable: false }
} as const;

export type LiveKitSessionFailureCode = keyof typeof LIVEKIT_FAILURES;
const LIVEKIT_SESSION_REQUEST_TIMEOUT_MS = 20_000;

/**
 * 保存可跨主进程和 renderer 使用的安全错误分类。
 *
 * @param code 本地白名单中的错误码。
 */
export class LiveKitSessionRequestError extends Error {
  readonly retryable: boolean;

  constructor(readonly code: LiveKitSessionFailureCode) {
    super(LIVEKIT_FAILURES[code].message);
    this.name = "LiveKitSessionRequestError";
    this.retryable = LIVEKIT_FAILURES[code].retryable;
  }
}

/**
 * 将 IPC 或 SDK 错误压缩成安全分类，不保留任意异常正文。
 *
 * @param error 请求或进房过程中收到的未知错误。
 * @returns 只包含固定中文说明的错误。
 */
export function classifyLiveKitSessionError(error: unknown): LiveKitSessionRequestError {
  if (error instanceof LiveKitSessionRequestError) return error;
  const message = error instanceof Error ? error.message : "";
  // Electron invoke 只保留错误消息；只识别我们自己的完整文案或标准 IPC 后缀。
  for (const code of Object.keys(LIVEKIT_FAILURES) as LiveKitSessionFailureCode[]) {
    const knownMessage = LIVEKIT_FAILURES[code].message;
    if (message === knownMessage || message.endsWith(`Error: ${knownMessage}`)) {
      return new LiveKitSessionRequestError(code);
    }
  }
  return new LiveKitSessionRequestError("livekit_connection_failed");
}

type FetchLiveKitSessionOptions = {
  url: string;
  signal?: AbortSignal;
  fetch?: (input: string, init?: RequestInit) => Promise<Response>;
};

/**
 * 在同一超时预算内取得响应和 JSON 正文，并过滤服务器错误。
 *
 * @param options 会话接口地址、可选取消信号和测试传输实现。
 * @returns 未经参与者结构校验的会话载荷；失败抛出安全错误，取消抛出 AbortError。
 */
export async function fetchLiveKitSessionPayload(options: FetchLiveKitSessionOptions): Promise<unknown> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let rejectCancellation!: (error: Error) => void;
  const cancellation = new Promise<never>((_resolve, reject) => { rejectCancellation = reject; });
  const cancel = () => {
    rejectCancellation(new DOMException("请求已取消。", "AbortError"));
    controller.abort();
  };
  options.signal?.addEventListener("abort", cancel, { once: true });
  if (options.signal?.aborted) cancel();
  timeout = setTimeout(() => {
    rejectCancellation(new LiveKitSessionRequestError("livekit_request_timeout"));
    controller.abort();
  }, LIVEKIT_SESSION_REQUEST_TIMEOUT_MS);

  const request = async () => {
    if (controller.signal.aborted) throw new DOMException("请求已取消。", "AbortError");
    const response = await (options.fetch ?? globalThis.fetch)(options.url, { signal: controller.signal });
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new LiveKitSessionRequestError(response.ok ? "livekit_invalid_response" : "livekit_connection_failed");
    }
    if (response.ok) return payload;
    const body = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    // 仅兼容旧版本这一条固定英文错误，其他 detail 一律不作为显示或日志内容。
    if (body.code === "livekit_not_configured" || body.detail === "livekit is not configured") {
      throw new LiveKitSessionRequestError("livekit_not_configured");
    }
    if (body.code === "assistant_not_ready") throw new LiveKitSessionRequestError("assistant_not_ready");
    if (response.status === 401 || response.status === 403 || response.status === 409) {
      throw new LiveKitSessionRequestError("livekit_access_denied");
    }
    throw new LiveKitSessionRequestError("livekit_connection_failed");
  };
  try {
    // Promise.race 也约束不响应 AbortSignal 的 body reader；迟到拒绝仍有观察者。
    return await Promise.race([request(), cancellation]);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw classifyLiveKitSessionError(error);
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", cancel);
  }
}
