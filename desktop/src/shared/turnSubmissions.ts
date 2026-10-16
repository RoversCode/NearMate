import { isStandardUuid } from "./uuid";

export type TurnSubmissionState = "preparing" | "queued" | "paused" | "failed";

export type TurnSubmissionItem = {
  submissionId: string;
  messageText: string;
  state: TurnSubmissionState;
  createdOrder: number;
  errorMessage: string | null;
};

export type TurnSubmissionsSnapshot = {
  revision: number;
  items: TurnSubmissionItem[];
};

export type TurnSubmissionCommandResponse = {
  accepted: true;
  turnSubmissions: TurnSubmissionsSnapshot;
};

const TURN_SUBMISSION_STATES = new Set<TurnSubmissionState>([
  "preparing",
  "queued",
  "paused",
  "failed"
]);

/**
 * 解析 Backend 返回的待发文本权威快照。
 *
 * Args:
 *   value: HTTP 或控制 WebSocket 收到的未知载荷。
 *
 * Returns:
 *   已转换为 Desktop 命名的严格快照。
 */
export function readTurnSubmissionsSnapshot(value: unknown): TurnSubmissionsSnapshot {
  const snapshot = readObject(value, "turn submissions snapshot");
  assertExactKeys(snapshot, ["revision", "items"], "turn submissions snapshot");
  if (!isNonNegativeInteger(snapshot.revision) || !Array.isArray(snapshot.items)) {
    throw new Error("turn submissions snapshot is invalid");
  }

  const items = snapshot.items.map(readTurnSubmissionItem);
  assertCreationOrder(items);
  return { revision: snapshot.revision, items };
}

/**
 * 解析 Main 进程通过 IPC 返回的 Turn 队列命令结果。
 *
 * Args:
 *   value: renderer 从 preload 收到的未知响应。
 *
 * Returns:
 *   已校验的接纳标记和 Desktop 命名快照。
 */
export function readTurnSubmissionCommandResponse(
  value: unknown
): TurnSubmissionCommandResponse {
  const response = readObject(value, "turn submission command response");
  assertExactKeys(
    response,
    ["accepted", "turnSubmissions"],
    "turn submission command response"
  );
  if (response.accepted !== true) {
    throw new Error("turn submission command was not accepted");
  }

  return {
    accepted: true,
    turnSubmissions: readDesktopTurnSubmissionsSnapshot(response.turnSubmissions)
  };
}

/**
 * 解析已经由 Main 进程转换成 Desktop 字段命名的队列快照。
 *
 * Args:
 *   value: 尚未确认字段结构的 IPC 快照。
 *
 * Returns:
 *   严格校验后的 Desktop 待发文本快照。
 */
function readDesktopTurnSubmissionsSnapshot(value: unknown): TurnSubmissionsSnapshot {
  const snapshot = readObject(value, "desktop turn submissions snapshot");
  assertExactKeys(snapshot, ["revision", "items"], "desktop turn submissions snapshot");
  if (!isNonNegativeInteger(snapshot.revision) || !Array.isArray(snapshot.items)) {
    throw new Error("desktop turn submissions snapshot is invalid");
  }

  const items = snapshot.items.map(readDesktopTurnSubmissionItem);
  assertCreationOrder(items);
  return { revision: snapshot.revision, items };
}

/**
 * 解析一条 Backend 命名的待发文本。
 *
 * Args:
 *   value: 控制 WebSocket 或 HTTP 返回的单条未知记录。
 *
 * Returns:
 *   已转换为 Desktop 字段名的严格记录。
 */
function readTurnSubmissionItem(value: unknown): TurnSubmissionItem {
  const item = readObject(value, "turn submission item");
  assertExactKeys(item, [
    "submission_id",
    "message_text",
    "state",
    "created_order",
    "error_message"
  ], "turn submission item");
  return validateTurnSubmissionItem({
    submissionId: readRequiredString(item.submission_id, "submission_id"),
    messageText: readRequiredString(item.message_text, "message_text"),
    state: item.state,
    createdOrder: item.created_order,
    errorMessage: item.error_message
  });
}

/**
 * 解析 IPC 快照中的一条 Desktop 命名待发文本。
 *
 * Args:
 *   value: Main 进程返回的单条未知记录。
 *
 * Returns:
 *   已完成字段值校验的待发文本。
 */
function readDesktopTurnSubmissionItem(value: unknown): TurnSubmissionItem {
  const item = readObject(value, "desktop turn submission item");
  assertExactKeys(item, [
    "submissionId",
    "messageText",
    "state",
    "createdOrder",
    "errorMessage"
  ], "desktop turn submission item");
  return validateTurnSubmissionItem({
    submissionId: item.submissionId,
    messageText: item.messageText,
    state: item.state,
    createdOrder: item.createdOrder,
    errorMessage: item.errorMessage
  });
}

/**
 * 校验一条已经完成字段名转换的待发文本。
 *
 * Args:
 *   item: 尚未确认字段值是否合法的候选记录。
 *
 * Returns:
 *   可以直接进入 renderer 状态的稳定记录。
 */
function validateTurnSubmissionItem(item: {
  submissionId: unknown;
  messageText: unknown;
  state: unknown;
  createdOrder: unknown;
  errorMessage: unknown;
}): TurnSubmissionItem {
  const submissionId = readRequiredString(item.submissionId, "submissionId");
  const messageText = readRequiredString(item.messageText, "messageText");
  if (!isStandardUuid(submissionId)) {
    throw new Error("turn submission id is invalid");
  }
  if (
    typeof item.state !== "string"
    || !TURN_SUBMISSION_STATES.has(item.state as TurnSubmissionState)
  ) {
    throw new Error("turn submission state is invalid");
  }
  if (!Number.isInteger(item.createdOrder) || Number(item.createdOrder) <= 0) {
    throw new Error("turn submission creation order is invalid");
  }
  if (item.errorMessage !== null && typeof item.errorMessage !== "string") {
    throw new Error("turn submission error message is invalid");
  }

  const errorMessage = typeof item.errorMessage === "string"
    ? item.errorMessage.trim() || null
    : null;
  if ((item.state === "failed") !== (errorMessage !== null)) {
    throw new Error("turn submission failure state is inconsistent");
  }
  return {
    submissionId,
    messageText,
    state: item.state as TurnSubmissionState,
    createdOrder: Number(item.createdOrder),
    errorMessage
  };
}

/**
 * 保证快照始终按 Backend 分配的创建顺序排列。
 *
 * Args:
 *   items: 已完成单项校验的待发文本列表。
 *
 * Returns:
 *   无；顺序异常时直接抛出协议错误。
 */
function assertCreationOrder(items: TurnSubmissionItem[]): void {
  for (let index = 1; index < items.length; index += 1) {
    if (items[index - 1].createdOrder >= items[index].createdOrder) {
      throw new Error("turn submissions are not in creation order");
    }
  }
}

/**
 * 读取一段普通对象载荷。
 *
 * Args:
 *   value: 尚未确认类型的协议值。
 *   label: 协议错误中使用的人类可读名称。
 *
 * Returns:
 *   可以继续逐字段校验的普通对象。
 */
function readObject(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is invalid`);
  }
  return value as Record<string, unknown>;
}

/**
 * 读取非空字符串字段。
 *
 * Args:
 *   value: 尚未确认类型的字段值。
 *   fieldName: 协议错误中使用的字段名。
 *
 * Returns:
 *   去除首尾空白后的字符串。
 */
function readRequiredString(value: unknown, fieldName: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`turn submission ${fieldName} is invalid`);
  }
  return value.trim();
}

/**
 * 判断字段是否为非负整数。
 *
 * Args:
 *   value: 尚未确认类型的 revision 值。
 *
 * Returns:
 *   值可安全作为进程内 revision 时返回真。
 */
function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/**
 * 要求对象字段集合完全匹配契约，拒绝未审查的新增字段。
 *
 * Args:
 *   value: 已确认是普通对象的协议值。
 *   expectedKeys: 当前结构必须且只能拥有的字段。
 *   label: 协议错误中使用的人类可读名称。
 *
 * Returns:
 *   无；字段缺失或多余时直接抛出协议错误。
 */
function assertExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
  label: string
): void {
  const actualKeys = Object.keys(value);
  if (
    actualKeys.length !== expectedKeys.length
    || expectedKeys.some(key => !Object.hasOwn(value, key))
  ) {
    throw new Error(`${label} fields are invalid`);
  }
}
