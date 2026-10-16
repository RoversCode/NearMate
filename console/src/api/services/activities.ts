import {
  parseActivityDetail,
  parseActivitySnapshot,
  type ActivityDetail,
  type ActivitySnapshot
} from "../contracts/activities";
import { readHttpErrorDetail } from "../httpClient";

/** 表示保留 HTTP 状态码的 Activity 接口错误。 */
export class ActivityApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ActivityApiError";
    this.status = status;
  }
}

/**
 * 读取本次运行的 Activity 列表。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   signal: 用于淘汰旧请求的中止信号。
 *
 * Returns:
 *   当前活动与最多两百条终态记录。
 */
export async function fetchActivities(
  baseUrl: string,
  signal?: AbortSignal
): Promise<ActivitySnapshot> {
  const response = await fetch(buildUrl(baseUrl, "/api/console/activities"), { signal });
  await assertOk(response, "读取任务列表失败");
  return parseActivitySnapshot(await response.json());
}

/**
 * 读取一项 Activity 公开详情。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   activityId: 标准 Activity UUID。
 *   signal: 用于淘汰旧请求的中止信号。
 *
 * Returns:
 *   不包含内部推理和原始事件的公共详情。
 */
export async function fetchActivityDetail(
  baseUrl: string,
  activityId: string,
  signal?: AbortSignal
): Promise<ActivityDetail> {
  const response = await fetch(
    buildUrl(baseUrl, `/api/console/activities/${encodeURIComponent(activityId)}`),
    { signal }
  );
  await assertOk(response, "读取任务详情失败");
  return parseActivityDetail(await response.json());
}

/**
 * 提交 Activity 真实取消命令。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   activityId: 标准 Activity UUID。
 *
 * Returns:
 *   后端接受命令入队后完成；已取消必须等待后续 Activity 终态。
 */
export async function cancelActivity(baseUrl: string, activityId: string): Promise<void> {
  const response = await fetch(
    buildUrl(baseUrl, `/api/activities/${encodeURIComponent(activityId)}/cancel`),
    { method: "POST" }
  );
  if (response.ok) {
    return;
  }
  const messageByStatus: Partial<Record<number, string>> = {
    404: "任务已不在本次运行中。",
    409: "任务状态已经变化，当前不能取消。",
    503: "后台运行时暂不可用，请稍后重试。"
  };
  throw new ActivityApiError(
    response.status,
    messageByStatus[response.status] ?? `取消任务失败：${await readHttpErrorDetail(response)}`
  );
}

/**
 * 拼接同源控制面 API 地址。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 以斜杠开头的 API 路径。
 *
 * Returns:
 *   去除重复斜杠后的完整地址。
 */
function buildUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

/**
 * 将非成功 HTTP 响应转换为保留状态码的 Activity 错误。
 *
 * Args:
 *   response: 待检查的 HTTP 响应。
 *   label: 面向用户的操作错误前缀。
 *
 * Returns:
 *   成功响应直接完成。
 */
async function assertOk(response: Response, label: string): Promise<void> {
  if (response.ok) {
    return;
  }
  throw new ActivityApiError(
    response.status,
    `${label}：${await readHttpErrorDetail(response)}`
  );
}
