import type { ActivityDetail } from "../../api/contracts/activities";
import { formatActivityStatus, formatActivityTime } from "./activityPresentation";

/**
 * 格式化「复制故障详情」的完整剪贴板文本。
 *
 * Args:
 *   activity: Console 当前展示的 Activity 详情。
 *
 * Returns:
 *   只包含公开字段的多行纯文本；Cognitive 自主失败额外包含具体原因。
 */
export function formatActivityFailureClipboard(activity: ActivityDetail): string {
  const failure = activity.failure;
  const lines: Array<[string, string]> = [
    ["任务编号", activity.activity_id],
    ["任务类型", activity.type_label || activity.kind],
    ["完整目标", activity.goal],
    ["当前状态", formatActivityStatus(activity.status)],
    ["失败阶段", failure ? `${failure.stage_label}（${failure.stage_code}）` : "—"],
    ["故障代码", failure?.code ?? "—"],
    ["原因摘要", failure?.summary ?? "—"]
  ];
  if (activity.failure_reason !== null) {
    lines.push(["具体失败原因", activity.failure_reason]);
  }
  lines.push(
    ["安全技术详情", failure?.technical_detail ? failure.technical_detail : "—"],
    ["处理建议", failure?.suggested_action ?? "—"],
    ["开始时间", formatActivityTime(activity.started_at)],
    ["失败时间", formatActivityTime(activity.finished_at)]
  );
  return lines.map(([label, value]) => `${label}：${value}`).join("\n");
}
