export type TimedReminderUpdate = {
  content?: string;
  triggerAt?: string;
};

export type DismissTimedRemindersResult = {
  runtimeId: string;
  dismissedIds: string[];
};

/** 修改等待中的提醒，只把用户实际改过的字段发送给 Backend。 */
export async function updateTimedReminder(
  httpBaseUrl: string,
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
  const response = await fetch(timedReminderUrl(httpBaseUrl, reminderId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw reminderMutationError(response.status, "修改");
  }
}

/** 取消一条仍在等待触发的提醒。 */
export async function cancelTimedReminder(
  httpBaseUrl: string,
  reminderId: string
): Promise<void> {
  const response = await fetch(timedReminderUrl(httpBaseUrl, reminderId), { method: "DELETE" });
  if (!response.ok) {
    throw reminderMutationError(response.status, "取消");
  }
}

/** 原子关闭当前界面冻结的响铃批次。 */
export async function dismissTimedReminders(
  httpBaseUrl: string,
  runtimeId: string,
  reminderIds: readonly string[]
): Promise<DismissTimedRemindersResult> {
  const response = await fetch(
    `${httpBaseUrl.replace(/\/$/, "")}/api/timed-reminders/dismiss`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runtime_id: runtimeId, reminder_ids: reminderIds })
    }
  );
  if (!response.ok) {
    throw reminderMutationError(response.status, "关闭");
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

/** 拼出一条提醒的管理接口地址。 */
function timedReminderUrl(httpBaseUrl: string, reminderId: string): string {
  return `${httpBaseUrl.replace(/\/$/, "")}/api/timed-reminders/${encodeURIComponent(reminderId)}`;
}

/** 把 HTTP 状态收敛成用户能直接处理的稳定提示。 */
function reminderMutationError(
  status: number,
  action: "修改" | "取消" | "关闭"
): Error {
  if (status === 404) {
    return new Error("提醒已不在本次运行中。");
  }
  if (status === 409) {
    return new Error(`提醒状态已经变化，当前不能${action}。`);
  }
  if (status === 422) {
    return new Error("提醒内容或时间无效，请检查后重试。");
  }
  if (status === 503) {
    return new Error("提醒运行时暂不可用，请稍后重试。");
  }
  return new Error(`${action}提醒失败（HTTP ${status}）`);
}

/** 严格校验关闭回执，防止错误运行期或重复编号污染本地状态。 */
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
