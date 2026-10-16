import type { ActivityStageCode, ActivityStatus } from "../../api/contracts/activities";
import type { ComponentProps } from "react";
import type { Badge } from "../../components/ui/badge";

type ActivityBadgeVariant = NonNullable<ComponentProps<typeof Badge>["variant"]>;

/**
 * 将 Activity 状态转换为稳定的中文产品文案。
 *
 * Args:
 *   status: Activity 公共状态。
 *
 * Returns:
 *   面向用户的简体中文状态。
 */
export function formatActivityStatus(status: ActivityStatus): string {
  return {
    running: "运行中",
    waiting_user: "等待输入",
    cancelling: "取消中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断"
  }[status];
}

/**
 * 将 Activity 状态映射为控制台徽标语义。
 *
 * Args:
 *   status: Activity 公共状态。
 *
 * Returns:
 *   与状态严重程度匹配的徽标变体。
 */
export function activityStatusVariant(status: ActivityStatus): ActivityBadgeVariant {
  if (status === "completed") {
    return "success";
  }
  if (status === "failed" || status === "interrupted") {
    return "destructive";
  }
  if (status === "cancelled") {
    return "outline";
  }
  return "warning";
}

/**
 * 将 ISO 时间显示为包含日期和秒的本地时间。
 *
 * Args:
 *   value: 带时区的 ISO 时间；活动未结束时为空。
 *
 * Returns:
 *   本地化完整时间或空值占位。
 */
export function formatActivityTime(value: string | null): string {
  if (value === null) {
    return "—";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(new Date(value));
}

/**
 * 计算并格式化任务耗时。
 *
 * Args:
 *   startedAt: 带时区的 ISO 开始时间；启动前终态为空。
 *   finishedAt: 终态时间；仍在执行时为空并以 now 计算。
 *   now: 页面共享时钟的当前时间。
 *
 * Returns:
 *   小于一小时为 mm:ss，否则为 h:mm:ss 的耗时文本。
 */
export function formatActivityElapsed(
  startedAt: string | null,
  finishedAt: string | null,
  now: Date
): string {
  if (startedAt === null) {
    return "—";
  }
  const startedMs = Date.parse(startedAt);
  const finishedMs = finishedAt === null ? now.getTime() : Date.parse(finishedAt);
  if (!Number.isFinite(startedMs) || !Number.isFinite(finishedMs)) {
    return "—";
  }
  const totalSeconds = Math.max(0, Math.round((finishedMs - startedMs) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * 展示当前或最后执行阶段；没有阶段记录时返回占位。
 *
 * Args:
 *   stageLabel: Backend 返回的阶段用户文案。
 *   stageCode: Backend 返回的阶段代码。
 *
 * Returns:
 *   阶段用户文案或空值占位。
 */
export function formatActivityStage(
  stageLabel: string | null,
  stageCode: ActivityStageCode | null
): string {
  if (stageLabel !== null && stageLabel.trim().length > 0) {
    return stageLabel;
  }
  return stageCode === null ? "—" : stageCode;
}
