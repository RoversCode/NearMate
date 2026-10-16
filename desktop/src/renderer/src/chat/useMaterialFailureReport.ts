import { useEffect, useState } from "react";
import type { MaterialFailureReportSnapshot } from "../../../shared/materialFeedback";
import {
  readMaterialFailureReportSnapshot,
  reduceMaterialFailureReportSnapshot
} from "./materialFailureReport";

type MaterialFailureReportBridge = {
  getMaterialFailureReport?(): Promise<unknown>;
  onMaterialFailureReportChanged?(listener: (payload: unknown) => void): () => void;
  reportDiagnostic?(level: "warn", message: string, details?: unknown): void;
};

const EMPTY_SNAPSHOT: MaterialFailureReportSnapshot = { revision: -1, report: null };

/**
 * 订阅主进程持有的运行期材料失败报告。
 *
 * @param bridge 聊天 preload 暴露的报告读取和订阅边界。
 * @returns 当前最高 revision 对应的失败报告。
 */
export function useMaterialFailureReport(bridge: MaterialFailureReportBridge | undefined) {
  const [snapshot, setSnapshot] = useState<MaterialFailureReportSnapshot>(EMPTY_SNAPSHOT);

  useEffect(() => {
    let disposed = false;
    function apply(payload: unknown): void {
      try {
        const incoming = readMaterialFailureReportSnapshot(payload);
        if (!disposed) {
          setSnapshot(current => reduceMaterialFailureReportSnapshot(current, incoming));
        }
      } catch (error) {
        bridge?.reportDiagnostic?.("warn", "materials.failure_report_invalid", error);
      }
    }

    const unsubscribe = bridge?.onMaterialFailureReportChanged?.(apply);
    void bridge?.getMaterialFailureReport?.().then(apply).catch(error => {
      bridge?.reportDiagnostic?.("warn", "materials.failure_report_fetch_failed", error);
    });
    return () => {
      disposed = true;
      unsubscribe?.();
    };
  }, [bridge]);

  return snapshot.report;
}
