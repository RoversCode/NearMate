// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMaterialFailureReport } from "./useMaterialFailureReport";

describe("useMaterialFailureReport", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("窗口重新挂载时读取主进程保存的最新报告", async () => {
    let listener: ((payload: unknown) => void) | null = null;
    const bridge = {
      getMaterialFailureReport: vi.fn(async () => snapshot(3, "报告.pdf")),
      onMaterialFailureReportChanged: vi.fn((next: (payload: unknown) => void) => {
        listener = next;
        return vi.fn();
      }),
      reportDiagnostic: vi.fn()
    };

    function Probe() {
      const report = useMaterialFailureReport(bridge);
      return <div>{report?.items[0]?.displayName ?? "empty"}</div>;
    }

    await act(async () => {
      root.render(<Probe />);
      await Promise.resolve();
    });
    expect(container.textContent).toBe("报告.pdf");

    act(() => listener?.({ revision: 4, report: null }));
    expect(container.textContent).toBe("empty");
  });
});

function snapshot(revision: number, displayName: string) {
  return {
    revision,
    report: {
      phase: "admission",
      items: [{
        displayName,
        category: "unreadable",
        reason: "invalid_or_encrypted_pdf"
      }]
    }
  };
}
