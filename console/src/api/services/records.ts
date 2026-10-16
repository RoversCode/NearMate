import { parseLogRows, type LogRow } from "../contracts/records";
import { fetchJson } from "../httpClient";

/**
 * 读取结构化运行日志。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   事件类型、原始 JSON 载荷和记录时间。
 */
export async function fetchLogs(baseUrl: string): Promise<LogRow[]> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/console/logs", "读取运行日志失败");
  return parseLogRows(payload);
}
