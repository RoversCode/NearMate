import { describe, expect, it, vi } from "vitest";
import type { MaterialAdmission } from "./materialsClient";
import {
  buildMaterialDeliveryFeedback,
  createMaterialDeliveryFeedbackService
} from "./materialDeliveryFeedbackService";

describe("materialDeliveryFeedbackService", () => {
  it("全部成功时播放上传音效并显示真实添加数", () => {
    expect(buildMaterialDeliveryFeedback(admission({ addedCount: 3, submittedCount: 3 }))).toEqual({
      operation: "add",
      kind: "success",
      addedCount: 3,
      submittedCount: 3,
      text: "已添加 3 项",
      sound: "upload"
    });
  });

  it("混合接纳生成可行动摘要并保存不含重复项的失败报告", () => {
    const avatarSend = vi.fn();
    const chatSend = vi.fn();
    const service = createMaterialDeliveryFeedbackService({
      getAvatarWindow: () => windowPort(avatarSend),
      getChatWindow: () => windowPort(chatSend)
    });
    const result = admission({
      submittedCount: 4,
      addedCount: 1,
      unsupportedCount: 1,
      unreadableCount: 1,
      duplicateCount: 1,
      issues: [
        issue(1, "bad.exe", "unsupported", "unsupported_extension"),
        issue(2, "empty.txt", "unreadable", "empty_file"),
        issue(3, "a.md", "duplicate", "duplicate")
      ]
    });

    expect(service.notifyMaterialAdmission(result)).toMatchObject({
      operation: "add",
      kind: "warning",
      text: "已添加 1 项，2 项未添加，打开聊天查看原因",
      sound: "upload"
    });
    expect(service.currentFailureReport()).toEqual({
      revision: 1,
      report: {
        phase: "admission",
        items: [
          expect.objectContaining({ displayName: "bad.exe", message: "暂不支持此类文件格式" }),
          expect.objectContaining({ displayName: "empty.txt", message: "文件内容为空" })
        ]
      }
    });
    expect(chatSend).toHaveBeenCalledWith(
      "chat:material-failure-report-changed",
      service.currentFailureReport()
    );
  });

  it("单项失败点名材料和原因且不携带音效", () => {
    expect(buildMaterialDeliveryFeedback(admission({
      submittedCount: 1,
      unreadableCount: 1,
      issues: [issue(0, "报告.pdf", "unreadable", "invalid_or_encrypted_pdf")]
    }))).toEqual({
      operation: "add",
      kind: "warning",
      addedCount: 0,
      submittedCount: 1,
      text: "“报告.pdf”文件已损坏或加密，未添加"
    });
  });

  it("删除成功清理旧报告并发布一次删除音效", () => {
    const send = vi.fn();
    const service = createMaterialDeliveryFeedbackService({
      getAvatarWindow: () => windowPort(send),
      getChatWindow: () => null
    });

    expect(service.notifyMaterialRemoved("报告.pdf")).toEqual({
      operation: "remove",
      kind: "success",
      text: "“报告.pdf”已删除",
      sound: "delete"
    });
    expect(send).toHaveBeenLastCalledWith("avatar:material-delivery-feedback", {
      operation: "remove",
      kind: "success",
      text: "“报告.pdf”已删除",
      sound: "delete"
    });
  });

  it("异步预检失败使用相同原因目录且不携带音效", () => {
    const service = createMaterialDeliveryFeedbackService({
      getAvatarWindow: () => null,
      getChatWindow: () => null
    });

    expect(service.notifyMaterialPreflightFailures([{
      materialId: "material-1",
      displayName: "图片.png",
      reason: "invalid_image"
    }])).toEqual({
      operation: "preflight",
      kind: "warning",
      text: "“图片.png”图片已损坏或格式与扩展名不一致，已从本轮材料移除"
    });
  });

  it("下一次材料操作开始时清除旧失败报告", () => {
    const service = createMaterialDeliveryFeedbackService({
      getAvatarWindow: () => null,
      getChatWindow: () => null
    });
    service.notifyMaterialPreflightFailures([{
      materialId: "material-1",
      displayName: "图片.png",
      reason: "invalid_image"
    }]);

    service.notifyMaterialProgress("正在检查 1 项…", 1);

    expect(service.currentFailureReport()).toEqual({ revision: 2, report: null });
  });
});

function admission(overrides: Partial<MaterialAdmission> = {}): MaterialAdmission {
  return {
    submittedCount: 0,
    addedCount: 0,
    unsupportedCount: 0,
    unreadableCount: 0,
    duplicateCount: 0,
    issues: [],
    ...overrides
  };
}

function issue(
  submittedIndex: number,
  displayName: string,
  category: "unsupported" | "unreadable" | "duplicate",
  reason: string
) {
  return { submittedIndex, displayName, category, reason };
}

function windowPort(send: ReturnType<typeof vi.fn>) {
  return {
    isDestroyed: () => false,
    webContents: { send }
  };
}
