import {
  parseTimedReminderSnapshot,
  type TimedReminderSnapshot
} from "../contracts/timedReminders";
import { readHttpErrorDetail } from "../httpClient";

export type TimedReminderUpdate = {
  content?: string;
  triggerAt?: string;
};

export type DismissTimedRemindersResult = {
  runtimeId: string;
  dismissedIds: string[];
};

/** 表示保留 HTTP 状态码的定时提醒接口错误。 */
export class TimedReminderApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "TimedReminderApiError";
    this.status = status;
  }
}

/**
 * 读取当前 Backend 运行期的完整定时提醒快照。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   signal: 用于淘汰旧请求的中止信号。
 *
 * Returns:
 *   当前活跃提醒和最近二十条终态记录。
 */
export async function fetchTimedReminders(
  baseUrl: string,
  signal?: AbortSignal
): Promise<TimedReminderSnapshot> {
  const response = await fetch(buildUrl(baseUrl, "/api/timed-reminders"), { signal });
  await assertOk(response, "读取定时提醒失败");
  return parseTimedReminderSnapshot(await response.json());
}

/** 修改一条仍在等待触发的提醒，只发送用户实际改过的字段。 */
export async function updateTimedReminder(
  baseUrl: string,
  reminderId: string,
  update: TimedReminderUpdate
): Promise<void> {
  const body = {
    ...(update.content !== undefined ? { content: update.content } : {}),
    ...(update.triggerAt !== undefined ? { trigger_at: update.triggerAt } : {})
  };
  if (Object.keys(body).length === 0) {
    throw new Error("修改提醒时至少需要一个变化字段。");
  }
  const response = await fetch(
    buildUrl(baseUrl, `/api/timed-reminders/${encodeURIComponent(reminderId)}`),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }
  );
  if (response.ok) {
    return;
  }
  throw await mutationError(response, "修改");
}

/** 取消一条仍在等待触发的记录。 */
export async function cancelTimedReminder(baseUrl: string, reminderId: string): Promise<void> {
  const response = await fetch(
    buildUrl(baseUrl, `/api/timed-reminders/${encodeURIComponent(reminderId)}`),
    { method: "DELETE" }
  );
  if (response.ok) {
    return;
  }
  throw await mutationError(response, "取消");
}

/** 原子关闭调用方当前看见的响铃批次。 */
export async function dismissTimedReminders(
  baseUrl: string,
  runtimeId: string,
  reminderIds: readonly string[]
): Promise<DismissTimedRemindersResult> {
  const response = await fetch(buildUrl(baseUrl, "/api/timed-reminders/dismiss"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runtime_id: runtimeId, reminder_ids: reminderIds })
  });
  if (!response.ok) {
    throw await mutationError(response, "关闭");
  }
  const payload: unknown = await response.json();
  if (!isDismissResult(payload)) {
    throw new Error("关闭提醒响应格式无效");
  }
  return {
    runtimeId: payload.runtime_id,
    dismissedIds: [...payload.dismissed_ids]
  };
}

/** 拼接同源控制面 API 地址。 */
function buildUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

/** 将读取失败转换为保留状态码的提醒错误。 */
async function assertOk(response: Response, label: string): Promise<void> {
  if (response.ok) {
    return;
  }
  throw new TimedReminderApiError(
    response.status,
    `${label}：${await readHttpErrorDetail(response)}`
  );
}

/** 把写接口状态收敛成用户可以直接理解的提示。 */
async function mutationError(
  response: Response,
  action: "修改" | "取消" | "关闭"
): Promise<Error> {
  const messageByStatus: Partial<Record<number, string>> = {
    404: "提醒已不在本次运行中。",
    409: `提醒状态已经变化，当前不能${action}。`,
    422: "提醒内容或时间无效，请检查后重试。",
    503: "提醒运行时暂不可用，请稍后重试。"
  };
  return new TimedReminderApiError(
    response.status,
    messageByStatus[response.status]
      ?? `${action}提醒失败：${await readHttpErrorDetail(response)}`
  );
}

/** 严格校验批次关闭回执，避免用错误运行期收起当前列表。 */
function isDismissResult(
  value: unknown
): value is { runtime_id: string; dismissed_ids: string[] } {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const result = value as { runtime_id?: unknown; dismissed_ids?: unknown };
  return Object.keys(value).length === 2
    && typeof result.runtime_id === "string"
    && result.runtime_id.length > 0
    && Array.isArray(result.dismissed_ids)
    && result.dismissed_ids.length <= 5
    && result.dismissed_ids.every(id => typeof id === "string" && id.length > 0)
    && new Set(result.dismissed_ids).size === result.dismissed_ids.length;
}
