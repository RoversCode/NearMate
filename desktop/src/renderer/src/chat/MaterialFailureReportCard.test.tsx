// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MaterialFailureReportCard } from "./MaterialFailureReportCard";

describe("MaterialFailureReportCard", () => {
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

  it("单项失败直接展示名称和原因", () => {
    act(() => root.render(<MaterialFailureReportCard report={{
      phase: "admission",
      items: [{
        displayName: "报告.pdf",
        category: "unreadable",
        reason: "invalid_or_encrypted_pdf",
        message: "文件已损坏或加密"
      }]
    }} />));

    expect(container.textContent).toContain("1 项材料未添加");
    expect(container.textContent).toContain("报告.pdf");
    expect(container.textContent).toContain("文件已损坏或加密");
  });

  it("多项默认折叠并通过无障碍按钮展开", () => {
    act(() => root.render(<MaterialFailureReportCard report={{
      phase: "preflight",
      items: [
        { displayName: "a.png", category: "unreadable", reason: "invalid_image", message: "图片内容无效" },
        { displayName: "b.pdf", category: "unreadable", reason: "invalid_or_encrypted_pdf", message: "文件已损坏或加密" }
      ]
    }} />));

    const button = container.querySelector("button") as HTMLButtonElement;
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).not.toContain("a.png");

    act(() => button.click());

    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("a.png");
    expect(container.textContent).toContain("b.pdf");
  });
});
