import { describe, expect, it } from "vitest";
import { buildMaterialShelfView, summarizeMaterialItem } from "./materialSummary";
import type { MaterialContextSnapshot } from "./materialTypes";

describe("materialSummary", () => {
  it("把不同材料类型转换成稳定的中文摘要", () => {
    expect(summarizeMaterialItem(makeItem({ kind: "dragged_file", display_name: "report.md" }))).toBe("文件 report.md");
    expect(summarizeMaterialItem(makeItem({ kind: "dragged_folder", display_name: "project" }))).toBe("文件夹 project");
    expect(summarizeMaterialItem(makeItem({ kind: "selected_image", display_name: "选中图片.png" }))).toBe("图片 选中图片.png");
    expect(summarizeMaterialItem(makeItem({ kind: "selected_web", display_name: "网页选区" }))).toBe("网页选区 网页选区");
    expect(summarizeMaterialItem(makeItem({ media_class: "audio", display_name: "采访.mp3" }))).toBe("音频 采访.mp3");
    expect(summarizeMaterialItem(makeItem({ media_class: "video", display_name: "现场.mp4" }))).toBe("视频 现场.mp4");
  });

  it("资料栏视图优先展示本轮待提交材料并返回缩略图条目", () => {
    const pool: MaterialContextSnapshot = {
      revision: 1,
      turn_material_context: {
        staged_materials: [
          makeItem({ material_id: "material-staged-1", kind: "selected_text", display_name: "选中文本.txt" }),
          makeItem({ material_id: "material-staged-2", kind: "selected_image", display_name: "选中图片.png" })
        ],
        active: {
          context_id: "turn-context-1",
          turn_id: "turn-1",
          created_at: "2026-07-06T10:00:00+08:00",
          attachments: [makeItem({ material_id: "material-active", display_name: "已提交材料.md" })]
        }
      },
      recent_materials: []
    };

    expect(buildMaterialShelfView(pool)).toEqual({
      isVisible: true,
      title: "本轮材料",
      countText: "2 项",
      items: [
        {
          materialId: "material-staged-1",
          kind: "selected_text",
          mediaClass: "text",
          displayName: "选中文本.txt",
          path: "G:\\docs\\report.md",
          thumbnailDataUrl: null
        },
        {
          materialId: "material-staged-2",
          kind: "selected_image",
          mediaClass: "text",
          displayName: "选中图片.png",
          path: "G:\\docs\\report.md",
          thumbnailDataUrl: null
        }
      ],
      snapshotText: null
    });
    expect(JSON.stringify(buildMaterialShelfView(pool))).not.toContain("已提交材料.md");
  });

  it("本轮材料上下文为空时不显示资料栏，长名称会被裁剪", () => {
    expect(buildMaterialShelfView({
      revision: 1,
      turn_material_context: {
        staged_materials: [],
        active: null
      },
      recent_materials: []
    }).isVisible).toBe(false);
    expect(
      summarizeMaterialItem(makeItem({
        kind: "dragged_file",
        display_name: "this-is-a-very-long-material-name-that-should-not-break-layout.md"
      }))
    ).toBe("文件 this-is-a-very-long-material-name...");
  });
});

function makeItem(
  overrides: Partial<MaterialContextSnapshot["turn_material_context"]["staged_materials"][number]> = {}
): MaterialContextSnapshot["turn_material_context"]["staged_materials"][number] {
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
