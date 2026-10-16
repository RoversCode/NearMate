// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RecentMaterial } from "./materialTypes";
import { RecentMaterialShelf } from "./RecentMaterialShelf";

describe("RecentMaterialShelf", () => {
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

  it("没有近期材料时不占界面空间", () => {
    renderShelf([], vi.fn(), vi.fn());

    expect(container.textContent).toBe("");
  });

  it("展示名称、节点类型和状态，并默认折叠完整路径", () => {
    renderShelf([
      material({ status: "available" }),
      material({
        display_name: "project",
        path: "G:\\workspace\\project",
        node_kind: "directory",
        status: "unsafe"
      })
    ], vi.fn(), vi.fn());

    expect(container.textContent).toContain("NearMate 当前记得");
    expect(container.textContent).toContain("report.md");
    expect(container.textContent).toContain("文件 · 可用");
    expect(container.textContent).toContain("目录 · 路径不安全");
    expect(container.querySelector("details")?.hasAttribute("open")).toBe(false);
  });

  it("刷新和单项忘记在提交期间防止重复调用，失败后恢复按钮", async () => {
    let finishRefresh: (() => void) | null = null;
    let failForget: ((error: Error) => void) | null = null;
    const refresh = vi.fn(() => new Promise<void>(resolve => {
      finishRefresh = resolve;
    }));
    const forget = vi.fn(() => new Promise<void>((_resolve, reject) => {
      failForget = reject;
    }));
    renderShelf([material()], refresh, forget);
    const refreshButton = findButton("刷新");
    const forgetButton = findButton("忘记 report.md");

    act(() => {
      refreshButton.click();
      refreshButton.click();
      forgetButton.click();
      forgetButton.click();
    });
    expect(refresh).toHaveBeenCalledOnce();
    expect(forget).toHaveBeenCalledOnce();
    expect(refreshButton.disabled).toBe(true);
    expect(forgetButton.disabled).toBe(true);

    await act(async () => {
      finishRefresh?.();
      failForget?.(new Error("network"));
      await Promise.resolve();
    });
    expect(findButton("刷新").disabled).toBe(false);
    expect(findButton("忘记 report.md").disabled).toBe(false);
  });

  function renderShelf(
    materials: RecentMaterial[],
    refresh: () => Promise<void>,
    forget: (path: string) => Promise<void>
  ): void {
    act(() => root.render(
      <RecentMaterialShelf materials={materials} refresh={refresh} forget={forget} />
    ));
  }

  function findButton(label: string): HTMLButtonElement {
    const ariaButton = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
    const textButton = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find(item => item.textContent === label);
    const result = ariaButton ?? textButton;
    if (!result) {
      throw new Error(`button not found: ${label}`);
    }
    return result;
  }
});

function material(overrides: Partial<RecentMaterial> = {}): RecentMaterial {
  return {
    display_name: "report.md",
    path: "G:\\docs\\report.md",
    material_kind: "dragged_file",
    node_kind: "file",
    media_class: "text",
    status: "available",
    ...overrides
  };
}
