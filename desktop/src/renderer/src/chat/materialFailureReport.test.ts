import { describe, expect, it } from "vitest";
import {
  readMaterialFailureReportSnapshot,
  reduceMaterialFailureReportSnapshot
} from "./materialFailureReport";

describe("materialFailureReport", () => {
  it("忽略主进程文案并按稳定原因生成安全用户说明", () => {
    expect(readMaterialFailureReportSnapshot({
      revision: 2,
      report: {
        phase: "preflight",
        items: [{
          materialId: "material-1",
          displayName: "报告.pdf",
          category: "unreadable",
          reason: "invalid_or_encrypted_pdf",
          message: "C:\\secret\\raw-error"
        }]
      }
    })).toEqual({
      revision: 2,
      report: {
        phase: "preflight",
        items: [{
          materialId: "material-1",
          displayName: "报告.pdf",
          category: "unreadable",
          reason: "invalid_or_encrypted_pdf",
          message: "文件已损坏或加密"
        }]
      }
    });
  });

  it("旧初始化响应不能覆盖更新的报告事件", () => {
    const current = readMaterialFailureReportSnapshot({ revision: 4, report: null });
    const incoming = readMaterialFailureReportSnapshot({
      revision: 3,
      report: {
        phase: "admission",
        items: [{ displayName: "bad.exe", category: "unsupported", reason: "unsupported_extension" }]
      }
    });

    expect(reduceMaterialFailureReportSnapshot(current, incoming)).toBe(current);
  });

  it("未知 Backend 原因降级为通用可行动文案", () => {
    const snapshot = readMaterialFailureReportSnapshot({
      revision: 1,
      report: {
        phase: "preflight",
        items: [{ displayName: "unknown.bin", category: "unreadable", reason: "new_backend_reason" }]
      }
    });

    expect(snapshot.report?.items[0].message).toBe("文件无法读取，请确认文件未被其他程序占用");
  });
});
