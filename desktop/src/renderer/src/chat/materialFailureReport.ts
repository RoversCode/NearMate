import {
  describeMaterialFailure,
  type MaterialFailureReport,
  type MaterialFailureReportSnapshot
} from "../../../shared/materialFeedback";

/**
 * 校验主进程保存的材料失败报告快照。
 *
 * @param payload IPC 初始化响应或变更事件。
 * @returns 只包含安全展示字段且带单调 revision 的报告快照。
 */
export function readMaterialFailureReportSnapshot(payload: unknown): MaterialFailureReportSnapshot {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("材料失败报告快照无效");
  }
  const candidate = payload as Record<string, unknown>;
  if (!Number.isInteger(candidate.revision) || (candidate.revision as number) < 0) {
    throw new Error("材料失败报告 revision 无效");
  }
  return {
    revision: candidate.revision as number,
    report: candidate.report === null ? null : readReport(candidate.report)
  };
}

export function reduceMaterialFailureReportSnapshot(
  current: MaterialFailureReportSnapshot,
  incoming: MaterialFailureReportSnapshot
): MaterialFailureReportSnapshot {
  return incoming.revision > current.revision ? incoming : current;
}

function readReport(payload: unknown): MaterialFailureReport {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("材料失败报告无效");
  }
  const candidate = payload as Record<string, unknown>;
  if ((candidate.phase !== "admission" && candidate.phase !== "preflight") || !Array.isArray(candidate.items)) {
    throw new Error("材料失败报告字段无效");
  }
  const items = candidate.items.map(item => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("材料失败项无效");
    }
    const issue = item as Record<string, unknown>;
    if (typeof issue.displayName !== "string" || !issue.displayName.trim()
      || (issue.category !== "unsupported" && issue.category !== "unreadable")
      || typeof issue.reason !== "string" || !issue.reason.trim()) {
      throw new Error("材料失败项字段无效");
    }
    return {
      ...(typeof issue.materialId === "string" && issue.materialId.trim()
        ? { materialId: issue.materialId.trim() }
        : {}),
      displayName: issue.displayName.trim(),
      category: issue.category as "unsupported" | "unreadable",
      reason: issue.reason.trim(),
      message: describeMaterialFailure(issue.reason.trim())
    };
  });
  if (items.length === 0) {
    throw new Error("材料失败报告不能为空");
  }
  return { phase: candidate.phase, items };
}
