import { describe, expect, it } from "vitest";

import { readChatMaterialFeedback } from "./chatMaterialFeedback";

describe("readChatMaterialFeedback", () => {
  it("读取主进程返回的警示反馈", () => {
    expect(readChatMaterialFeedback({
      accepted: true,
      feedback: {
        kind: "warning",
        addedCount: 1,
        submittedCount: 2,
        text: "已添加 1 项，1 项格式不支持，已过滤",
        sound: "upload"
      }
    })).toEqual({
      kind: "warning",
      text: "已添加 1 项，1 项格式不支持，已过滤"
    });
  });

  it("拒绝缺少稳定字段的反馈", () => {
    expect(() => readChatMaterialFeedback({ accepted: true, feedback: { kind: "warning" } }))
      .toThrow("材料接纳反馈字段无效");
  });
});
