import {
  readTurnSubmissionsSnapshot,
  type TurnSubmissionsSnapshot
} from "../../shared/turnSubmissions";

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type CreateTurnSubmissionsClientOptions = {
  httpBaseUrl: string;
  fetch?: FetchLike;
};

export type TurnSubmissionsClient = {
  reserve(input: { submissionId: string; messageText: string }): Promise<TurnSubmissionsSnapshot>;
  commitContext(input: { submissionId: string; environmentSnapshotId: string | null }): Promise<TurnSubmissionsSnapshot>;
  edit(input: { submissionId: string; messageText: string }): Promise<TurnSubmissionsSnapshot>;
  delete(submissionId: string): Promise<TurnSubmissionsSnapshot>;
  resume(submissionId: string): Promise<TurnSubmissionsSnapshot>;
  retry(submissionId: string): Promise<TurnSubmissionsSnapshot>;
  reportPreparationFailed(submissionId: string): Promise<TurnSubmissionsSnapshot>;
};

/**
 * 创建只负责 Turn 队列 HTTP 协议的客户端。
 *
 * Args:
 *   options: 控制服务根地址和可注入的 fetch。
 *
 * Returns:
 *   reservation、环境提交和队列操作客户端。
 */
export function createTurnSubmissionsClient(
  options: CreateTurnSubmissionsClientOptions
): TurnSubmissionsClient {
  const httpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);

  /**
   * 执行一条 Turn 队列命令并解析权威快照。
   *
   * Args:
   *   path: 相对控制服务根地址的 API 路径。
   *   method: 当前命令使用的 HTTP 方法。
   *   body: 可选的 JSON 请求正文。
   *
   * Returns:
   *   Backend 接纳命令后返回的完整待发文本快照。
   */
  async function request(
    path: string,
    method: "POST" | "PUT" | "PATCH" | "DELETE",
    body?: Record<string, unknown>
  ): Promise<TurnSubmissionsSnapshot> {
    const response = await fetchImplementation(`${httpBaseUrl}${path}`, {
      method,
      ...(body
        ? {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          }
        : {})
    });
    if (!response.ok) {
      throw new Error(`turn submission request failed: ${response.status}`);
    }
    const payload = await response.json() as Record<string, unknown>;
    if (payload.accepted !== true) {
      throw new Error("turn submission response was not accepted");
    }
    return readTurnSubmissionsSnapshot(payload.turn_submissions);
  }

  /**
   * 生成单条待发文本的安全 API 路径。
   *
   * Args:
   *   submissionId: Desktop 创建的稳定文本提交编号。
   *
   * Returns:
   *   已完成路径编码的资源地址。
   */
  function pathFor(submissionId: string): string {
    return `/api/turn-submissions/${encodeURIComponent(submissionId)}`;
  }

  return {
    reserve(input) {
      return request("/api/turn-submissions", "POST", {
        submission_id: input.submissionId,
        message_text: input.messageText
      });
    },
    commitContext(input) {
      return request(`${pathFor(input.submissionId)}/context`, "PUT", {
        environment_snapshot_id: input.environmentSnapshotId
      });
    },
    edit(input) {
      return request(pathFor(input.submissionId), "PATCH", {
        message_text: input.messageText
      });
    },
    delete(submissionId) {
      return request(pathFor(submissionId), "DELETE");
    },
    resume(submissionId) {
      return request(`${pathFor(submissionId)}/resume`, "POST");
    },
    retry(submissionId) {
      return request(`${pathFor(submissionId)}/retry`, "POST");
    },
    reportPreparationFailed(submissionId) {
      return request(`${pathFor(submissionId)}/preparation-failed`, "POST", {
        reason: "environment_capture_failed"
      });
    }
  };
}
