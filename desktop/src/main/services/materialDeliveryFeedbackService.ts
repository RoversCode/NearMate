import {
  describeMaterialFailure,
  type MaterialFailureReport,
  type MaterialFailureReportItem,
  type MaterialFailureReportSnapshot
} from "../../shared/materialFeedback";
import type { MaterialAdmission } from "./materialsClient";

const MATERIAL_DELIVERY_FEEDBACK_CHANNEL = "avatar:material-delivery-feedback";
const MATERIAL_FAILURE_REPORT_CHANNEL = "chat:material-failure-report-changed";

export type MaterialDeliveryFeedback =
  | {
      operation: "add";
      kind: "progress" | "success" | "warning";
      addedCount: number;
      submittedCount: number;
      text: string;
      sound?: "upload";
    }
  | {
      operation: "remove";
      kind: "success";
      text: string;
      sound: "delete";
    }
  | {
      operation: "preflight";
      kind: "warning";
      text: string;
    };

type FeedbackWindow = {
  isDestroyed(): boolean;
  webContents: {
    send(channel: string, payload: unknown): void;
  };
};

type MaterialDeliveryFeedbackServiceOptions = {
  getAvatarWindow(): FeedbackWindow | null;
  getChatWindow(): FeedbackWindow | null;
};

export type MaterialPreflightFailure = {
  materialId: string;
  displayName: string;
  reason: string;
};

export type MaterialDeliveryFeedbackService = {
  notifyMaterialAdmission(admission: MaterialAdmission): MaterialDeliveryFeedback | null;
  notifyMaterialProgress(text: string, submittedCount?: number): MaterialDeliveryFeedback;
  notifyMaterialWarning(text: string): MaterialDeliveryFeedback;
  notifyMaterialRemoved(displayName: string): MaterialDeliveryFeedback;
  notifyMaterialPreflightFailures(items: MaterialPreflightFailure[]): MaterialDeliveryFeedback | null;
  currentFailureReport(): MaterialFailureReportSnapshot;
  clearFailureReport(): void;
};

/**
 * 创建材料瞬时反馈和聊天失败报告的唯一运行期状态服务。
 *
 * @param options 当前头像与聊天窗口读取函数。
 * @returns 材料反馈发布、报告读取和生命周期清理能力。
 */
export function createMaterialDeliveryFeedbackService(
  options: MaterialDeliveryFeedbackServiceOptions
): MaterialDeliveryFeedbackService {
  let failureReport: MaterialFailureReport | null = null;
  let failureReportRevision = 0;

  function notify(feedback: MaterialDeliveryFeedback): MaterialDeliveryFeedback {
    for (const window of [options.getAvatarWindow(), options.getChatWindow()]) {
      if (window && !window.isDestroyed()) {
        window.webContents.send(MATERIAL_DELIVERY_FEEDBACK_CHANNEL, feedback);
      }
    }
    return feedback;
  }

  function setFailureReport(nextReport: MaterialFailureReport | null): void {
    failureReport = nextReport;
    failureReportRevision += 1;
    const snapshot = { revision: failureReportRevision, report: failureReport };
    const chatWindow = options.getChatWindow();
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send(MATERIAL_FAILURE_REPORT_CHANNEL, snapshot);
    }
  }

  return {
    notifyMaterialAdmission(admission) {
      const reportItems = admission.issues.flatMap(issue => (
        issue.category === "duplicate" ? [] : [{
          displayName: issue.displayName,
          category: issue.category,
          reason: issue.reason,
          message: describeMaterialFailure(issue.reason)
        } satisfies MaterialFailureReportItem]
      ));
      setFailureReport(reportItems.length > 0 ? { phase: "admission", items: reportItems } : null);
      const feedback = buildMaterialDeliveryFeedback(admission);
      return feedback ? notify(feedback) : null;
    },

    notifyMaterialProgress(text, submittedCount = 0) {
      setFailureReport(null);
      return notify({
        operation: "add",
        kind: "progress",
        addedCount: 0,
        submittedCount,
        text
      });
    },

    notifyMaterialWarning(text) {
      return notify({
        operation: "add",
        kind: "warning",
        addedCount: 0,
        submittedCount: 0,
        text
      });
    },

    notifyMaterialRemoved(displayName) {
      return notify({
        operation: "remove",
        kind: "success",
        text: `“${displayName}”已删除`,
        sound: "delete"
      });
    },

    notifyMaterialPreflightFailures(items) {
      if (items.length === 0) {
        return null;
      }
      const reportItems = items.map(item => ({
        materialId: item.materialId,
        displayName: item.displayName,
        category: "unreadable" as const,
        reason: item.reason,
        message: describeMaterialFailure(item.reason)
      }));
      setFailureReport({ phase: "preflight", items: reportItems });
      return notify({
        operation: "preflight",
        kind: "warning",
        text: reportItems.length === 1
          ? `“${reportItems[0].displayName}”${reportItems[0].message}，已从本轮材料移除`
          : `${reportItems.length} 项材料检查失败，已移除，打开聊天查看原因`
      });
    },

    currentFailureReport() {
      return { revision: failureReportRevision, report: failureReport };
    },

    clearFailureReport() {
      setFailureReport(null);
    }
  };
}

/**
 * 根据真实批次结果生成头像和聊天共用的瞬时反馈。
 *
 * @param admission Backend 返回并由 Desktop 严格校验的接纳结果。
 * @returns 空提交返回 null，其余返回带确定音效策略的添加反馈。
 */
export function buildMaterialDeliveryFeedback(
  admission: MaterialAdmission
): MaterialDeliveryFeedback | null {
  if (admission.submittedCount === 0) {
    return null;
  }

  const failedIssues = admission.issues.filter(item => item.category !== "duplicate");
  const rejectedCount = admission.unsupportedCount + admission.unreadableCount + admission.duplicateCount;
  let text: string;
  if (failedIssues.length === 1 && admission.addedCount === 0 && admission.duplicateCount === 0) {
    const issue = failedIssues[0];
    text = `“${issue.displayName}”${describeMaterialFailure(issue.reason)}，未添加`;
  } else if (failedIssues.length > 0) {
    text = admission.addedCount > 0
      ? `已添加 ${admission.addedCount} 项，${failedIssues.length} 项未添加，打开聊天查看原因`
      : `${failedIssues.length} 项材料未添加，打开聊天查看原因`;
  } else if (admission.duplicateCount === admission.submittedCount) {
    text = "所选内容已在本轮材料中";
  } else {
    const segments = admission.addedCount > 0 ? [`已添加 ${admission.addedCount} 项`] : [];
    if (admission.duplicateCount > 0) {
      segments.push(`${admission.duplicateCount} 项已在本轮材料中`);
    }
    text = segments.join("，");
  }

  return {
    operation: "add",
    kind: rejectedCount > 0 ? "warning" : "success",
    addedCount: admission.addedCount,
    submittedCount: admission.submittedCount,
    text,
    ...(admission.addedCount > 0 ? { sound: "upload" as const } : {})
  };
}
