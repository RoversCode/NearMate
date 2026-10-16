import type { CapturedEnvironmentSnapshot } from "./contextCapture";

export type EnvironmentSnapshotMetadata = {
  request_id?: string | null;
  turn_id?: string | null;
  reason: "chat_focus" | "chat_submit" | "voice_turn_started";
};

export type EnvironmentContextClient = {
  upload(
    snapshot: CapturedEnvironmentSnapshot,
    metadata: EnvironmentSnapshotMetadata,
    signal?: AbortSignal
  ): Promise<string>;
};

type CreateEnvironmentContextClientOptions = {
  httpBaseUrl: string;
  fetch?: (input: string, init?: RequestInit) => Promise<Response>;
};

/**
 * 创建环境快照 multipart 上传客户端。
 *
 * Args:
 *   options: Backend HTTP 根地址与可注入 fetch。
 *
 * Returns:
 *   上传内存截图并返回权威 snapshot_id 的客户端。
 */
export function createEnvironmentContextClient(
  options: CreateEnvironmentContextClientOptions
): EnvironmentContextClient {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);

  return {
    async upload(snapshot, metadata, signal): Promise<string> {
      let response = await sendEnvironmentRequest(
        fetchImplementation,
        normalizedHttpBaseUrl,
        snapshot,
        metadata,
        signal
      );
      if (snapshot.screenshotBytes && [413, 415, 422].includes(response.status)) {
        response = await sendEnvironmentRequest(
          fetchImplementation,
          normalizedHttpBaseUrl,
          {
            ...snapshot,
            screenshotBytes: null,
            captureError: appendCaptureError(snapshot.captureError, `screenshot_upload_rejected:${response.status}`)
          },
          metadata,
          signal
        );
      }
      if (!response.ok) {
        throw new Error(`environment context request failed: ${response.status}`);
      }
      const payload: unknown = await response.json();
      const snapshotId = payload && typeof payload === "object" && !Array.isArray(payload)
        ? (payload as { snapshot_id?: unknown }).snapshot_id
        : null;
      if (typeof snapshotId !== "string" || !snapshotId.trim()) {
        throw new Error("environment context response is missing snapshot_id");
      }
      return snapshotId.trim();
    }
  };
}

/**
 * 构造并发送一次现场 multipart 请求。
 *
 * Args:
 *   fetchImplementation: 可注入的 HTTP 请求实现。
 *   httpBaseUrl: Backend HTTP 根地址。
 *   snapshot: 本次内存现场内容。
 *   metadata: 文本或语音 turn 的绑定标识。
 *
 * Returns:
 *   Backend 原始响应，由上层统一处理降级与 snapshot_id。
 */
async function sendEnvironmentRequest(
  fetchImplementation: (input: string, init?: RequestInit) => Promise<Response>,
  httpBaseUrl: string,
  snapshot: CapturedEnvironmentSnapshot,
  metadata: EnvironmentSnapshotMetadata,
  signal?: AbortSignal
): Promise<Response> {
  const body = new FormData();
  body.append("metadata", JSON.stringify({
    ...metadata,
    captured_at: snapshot.capturedAt,
    active_window_title: snapshot.activeWindowTitle,
    capture_error: snapshot.captureError
  }));
  if (snapshot.screenshotBytes) {
    body.append(
      "screenshot",
      new Blob([new Uint8Array(snapshot.screenshotBytes)], { type: "image/png" }),
      "environment.png"
    );
  }
  return fetchImplementation(`${httpBaseUrl}/api/environment/current`, {
    method: "POST",
    body,
    signal
  });
}

/**
 * 合并现场采集与上传阶段的错误编码。
 *
 * Args:
 *   currentError: 采集阶段已有的错误；没有时为 null。
 *   nextError: 本次图片上传拒绝错误。
 *
 * Returns:
 *   使用分号拼接的稳定错误文本。
 */
function appendCaptureError(currentError: string | null, nextError: string): string {
  return currentError ? `${currentError};${nextError}` : nextError;
}
