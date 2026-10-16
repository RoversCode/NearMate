export type FollowupControlClient = {
  toggleFollowup(): Promise<{ interruptedRequestIds: string[]; speechInterruptedAt?: number }>;
};

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type CreateFollowupControlClientOptions = {
  httpBaseUrl: string;
  fetch?: FetchLike;
};

/**
 * 创建追问热区控制客户端。
 *
 * @param options 控制服务 HTTP 根地址与可注入 fetch。
 * @returns 只负责调用追问热区控制 API 的窄客户端。
 */
export function createFollowupControlClient(options: CreateFollowupControlClientOptions): FollowupControlClient {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);
  let pending: Promise<unknown> = Promise.resolve();

  return {
    toggleFollowup(): Promise<{ interruptedRequestIds: string[]; speechInterruptedAt?: number }> {
      // 连按按收到顺序执行；前一次失败释放队列，不能阻塞后续切换。
      const operation = pending.catch(() => undefined).then(async () => {
        const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/followup/toggle`, {
          method: "POST",
          signal: AbortSignal.timeout(8000)
        });
        if (!response.ok) throw new Error(`followup toggle request failed: ${response.status}`);
        const result: unknown = await response.json();
        if (!result || typeof result !== "object"
          || !("accepted" in result) || result.accepted !== true
          || !("followup_state" in result) || !["followup_hot", "followup_cool"].includes(String(result.followup_state))
          || !("interrupted_request_ids" in result)
          || !Array.isArray(result.interrupted_request_ids)
          || !result.interrupted_request_ids.every(id => typeof id === "string" && id.trim().length > 0)) {
          throw new Error("followup toggle response invalid");
        }
        const cutoff = "speech_interrupted_at" in result ? result.speech_interrupted_at : undefined;
        if (cutoff !== undefined && (typeof cutoff !== "number" || !Number.isFinite(cutoff) || cutoff < 0)) {
          throw new Error("followup toggle response invalid");
        }
        return { interruptedRequestIds: result.interrupted_request_ids as string[],
          ...(cutoff === undefined ? {} : { speechInterruptedAt: cutoff as number }) };
      });
      pending = operation;
      return operation;
    }
  };
}
