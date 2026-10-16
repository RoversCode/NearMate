import {
  isBackgroundAgentUsageResponse,
  type BackgroundAgentUsageResponse
} from "../contracts/backgroundAgentUsage";
import { fetchJsonWithInit } from "../httpClient";

export type BackgroundAgentUsageRange = {
  fromDate: string;
  toDate: string;
};

/**
 * 查询一段包含首尾日期的后台工作用量。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   range: 包含首尾两天的固化本地日期区间。
 *   signal: 页面切换或筛选变化时用于取消旧请求的信号。
 *
 * Returns:
 *   已通过公式和字段校验的统计响应。
 */
export async function fetchBackgroundAgentUsage(
  baseUrl: string,
  range: BackgroundAgentUsageRange,
  signal?: AbortSignal
): Promise<BackgroundAgentUsageResponse> {
  const params = new URLSearchParams({
    from_date: range.fromDate,
    to_date: range.toDate
  });
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/background-agent/usage?${params.toString()}`,
    { signal },
    "读取后台工作用量失败"
  );
  if (!isBackgroundAgentUsageResponse(payload)) {
    throw new Error("读取后台工作用量失败：响应格式不符合契约");
  }
  return payload;
}
