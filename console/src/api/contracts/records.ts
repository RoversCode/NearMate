export type LogRow = {
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  recorded_at: string;
};

/**
 * 校验本次运行日志的结构化响应。
 *
 * Args:
 *   value: 未受信任的 HTTP JSON 响应。
 *
 * Returns:
 *   带单调序号、结构化载荷和记录时间的日志数组。
 */
export function parseLogRows(value: unknown): LogRow[] {
  if (!Array.isArray(value) || !value.every(isLogRow)) {
    throw new Error("运行日志响应格式无效");
  }
  return value;
}

/**
 * 校验一条结构化运行日志。
 *
 * Args:
 *   value: 未受信任的日志值。
 *
 * Returns:
 *   序号、事件、对象载荷和时间全部合法时返回真。
 */
function isLogRow(value: unknown): value is LogRow {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const row = value as Partial<LogRow>;
  return (
    Number.isInteger(row.sequence) &&
    (row.sequence ?? 0) > 0 &&
    typeof row.event_type === "string" &&
    row.event_type.trim().length > 0 &&
    !!row.payload &&
    typeof row.payload === "object" &&
    !Array.isArray(row.payload) &&
    typeof row.recorded_at === "string" &&
    Number.isFinite(Date.parse(row.recorded_at))
  );
}
