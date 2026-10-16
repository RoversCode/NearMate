import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppSidebar } from "./AppSidebar";

describe("AppSidebar brand", () => {
  it("显示 NearMate 图标和正式产品名，不再显示历史占位品牌", () => {
    const { container } = render(
      <AppSidebar activeSectionId="settings" onSelectSection={vi.fn()} />
    );

    expect(container.textContent).toContain("NearMate");
    expect(container.textContent).not.toContain("VC");
    expect(container.textContent).not.toContain("虚拟伴侣");
    expect(container.querySelector('img[src*="icon"]')).not.toBeNull();
    const manualLink = container.querySelector<HTMLAnchorElement>('a[href="https://roverscode.github.io/NearMate/guide/index.html"]');
    expect(manualLink?.textContent).toContain("使用手册");
    expect(manualLink?.target).toBe("_blank");
  });
});
