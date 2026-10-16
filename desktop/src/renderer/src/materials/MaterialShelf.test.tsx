// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MaterialShelf } from "./MaterialShelf";
import type { MaterialContextSnapshot } from "./materialTypes";

describe("MaterialShelf", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("本轮材料上下文为空时不渲染资料栏", () => {
    renderShelf(emptyMaterialContext(), vi.fn());

    expect(container.textContent).toBe("");
  });

  it("只有 active turn 材料时不渲染待提交资料栏", () => {
    renderShelf({
      revision: 1,
      turn_material_context: {
        staged_materials: [],
        active: {
          context_id: "turn-context-1",
          turn_id: "turn-1",
          created_at: "2026-07-06T10:00:00+08:00",
          attachments: [makeItem({ display_name: "已提交材料.md" })]
        }
      },
      recent_materials: []
    }, vi.fn());

    expect(container.textContent).toBe("");
  });

  it("以单行缩略图展示本轮材料，并在删除按钮点击时传出 material_id", async () => {
    const removeMaterialItem = vi.fn<(_: string) => Promise<void>>().mockResolvedValue(undefined);

    renderShelf({
      revision: 1,
      turn_material_context: {
        staged_materials: [
          makeItem({ material_id: "material-2", display_name: "a.md", thumbnailDataUrl: "data:image/png;base64,a" }),
          makeItem({ material_id: "material-3", display_name: "b.md" })
        ],
        active: null
      },
      recent_materials: []
    }, removeMaterialItem);

    expect(container.textContent).toContain("本轮材料");
    expect(container.textContent).toContain("2 项");
    expect(container.textContent).toContain("a.md");
    expect(container.textContent).toContain("b.md");
    expect(container.textContent).not.toContain("当前环境快照");
    expect(container.textContent).not.toContain("本轮材料.md");
    expect(container.querySelector(".material-shelf-strip")).toBeTruthy();
    expect(container.querySelectorAll(".material-shelf-thumb")).toHaveLength(2);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("data:image/png;base64,a");

    const deleteButton = container.querySelector('button[aria-label="删除 a.md"]') as HTMLButtonElement;
    await act(async () => {
      deleteButton.click();
      await Promise.resolve();
    });

    expect(removeMaterialItem).toHaveBeenCalledWith("material-2");
  });

  it("删除期间禁用当前按钮并阻止重复提交", async () => {
    let resolveDelete: (() => void) | null = null;
    const removeMaterialItem = vi.fn(() => new Promise<void>(resolve => {
      resolveDelete = resolve;
    }));
    renderShelf(materialContextWithOneItem(), removeMaterialItem);
    const deleteButton = container.querySelector('button[aria-label="删除 report.md"]') as HTMLButtonElement;

    act(() => {
      deleteButton.click();
      deleteButton.click();
    });

    expect(deleteButton.disabled).toBe(true);
    expect(removeMaterialItem).toHaveBeenCalledOnce();
    await act(async () => {
      resolveDelete?.();
      await Promise.resolve();
    });
    expect(deleteButton.disabled).toBe(false);
  });

  it("音频和视频材料使用明确的占位标识", () => {
    renderShelf({
      revision: 1,
      turn_material_context: {
        staged_materials: [
          makeItem({ material_id: "audio", display_name: "voice.mp3", media_class: "audio" }),
          makeItem({ material_id: "video", display_name: "movie.mp4", media_class: "video" })
        ],
        active: null
      },
      recent_materials: []
    }, vi.fn());

    const fallbacks = Array.from(container.querySelectorAll(".material-shelf-fallback"));
    expect(fallbacks.map(item => item.textContent)).toEqual(["音", "视"]);
  });

  it("删除失败后恢复按钮以便用户重试", async () => {
    const removeMaterialItem = vi.fn().mockRejectedValue(new Error("network"));
    renderShelf(materialContextWithOneItem(), removeMaterialItem);
    const deleteButton = container.querySelector('button[aria-label="删除 report.md"]') as HTMLButtonElement;

    await act(async () => {
      deleteButton.click();
      await Promise.resolve();
    });

    expect(deleteButton.disabled).toBe(false);
  });

  function renderShelf(
    pool: MaterialContextSnapshot,
    removeMaterialItem: (materialId: string) => Promise<void>
  ) {
    act(() => {
      root.render(<MaterialShelf pool={pool} removeMaterialItem={removeMaterialItem} />);
    });
  }
});

function materialContextWithOneItem(): MaterialContextSnapshot {
  return {
    revision: 1,
    turn_material_context: {
      staged_materials: [makeItem()],
      active: null
    },
    recent_materials: []
  };
}

function makeItem(
  overrides: Partial<MaterialContextSnapshot["turn_material_context"]["staged_materials"][number] & { thumbnailDataUrl: string }> = {}
): MaterialContextSnapshot["turn_material_context"]["staged_materials"][number] & { thumbnailDataUrl?: string } {
  return {
    material_id: "material-test",
    turn_id: "turn-test",
    kind: "dragged_file",
    source: "drag_drop",
    display_name: "report.md",
    path: "G:\\docs\\report.md",
    node_kind: "file",
    created_at: "2026-06-27T10:00:00+08:00",
    media_class: "text",
    ...overrides
  };
}

function emptyMaterialContext(): MaterialContextSnapshot {
  return {
    revision: 1,
    turn_material_context: {
      staged_materials: [],
      active: null
    },
    recent_materials: []
  };
}
