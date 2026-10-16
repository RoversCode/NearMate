import { describe, expect, it } from "vitest";
import {
  readMaterialContextSnapshot,
  reduceMaterialContextSnapshot,
  type MaterialContextSnapshot
} from "./materialContext";

describe("material context revision reducer", () => {
  it("忽略旧 revision、重复响应和乱序广播", () => {
    const current = snapshot(5);

    expect(reduceMaterialContextSnapshot(current, snapshot(4))).toBe(current);
    expect(reduceMaterialContextSnapshot(current, snapshot(5))).toBe(current);
    expect(reduceMaterialContextSnapshot(current, snapshot(6)).revision).toBe(6);
  });

  it.each(["audio", "video"] as const)("接受 %s 材料媒体类别", mediaClass => {
    const parsed = readMaterialContextSnapshot({
      revision: 1,
      turn_material_context: {
        staged_materials: [{
          material_id: "material-media",
          turn_id: "turn-a",
          kind: "dragged_file",
          source: "drag_drop",
          display_name: mediaClass === "audio" ? "voice.mp3" : "movie.mp4",
          path: "G:\\media\\input",
          node_kind: "file",
          created_at: "2026-07-30T10:00:00+08:00",
          media_class: mediaClass
        }],
        active: null
      },
      recent_materials: []
    });

    expect(parsed.turn_material_context.staged_materials[0]?.media_class).toBe(mediaClass);
  });

  it("精确解析近期材料状态和节点类型", () => {
    const parsed = readMaterialContextSnapshot({
      revision: 2,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: [{
        display_name: "project",
        path: "G:\\workspace\\project",
        material_kind: "dragged_folder",
        node_kind: "directory",
        media_class: "mixed_or_document",
        status: "available"
      }]
    });

    expect(parsed.recent_materials).toEqual([{
      display_name: "project",
      path: "G:\\workspace\\project",
      material_kind: "dragged_folder",
      node_kind: "directory",
      media_class: "mixed_or_document",
      status: "available"
    }]);
  });

  it("拒绝缺少近期材料字段或节点类型的旧快照", () => {
    expect(() => readMaterialContextSnapshot({
      revision: 1,
      turn_material_context: { staged_materials: [], active: null }
    })).toThrow("material context snapshot fields are invalid");
  });
});

function snapshot(revision: number): MaterialContextSnapshot {
  return {
    revision,
    turn_material_context: { staged_materials: [], active: null },
    recent_materials: []
  };
}
