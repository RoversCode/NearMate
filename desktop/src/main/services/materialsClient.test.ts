import { describe, expect, it, vi } from "vitest";

import { createMaterialsClient } from "./materialsClient";

describe("createMaterialsClient", () => {
  it("追加 staged 材料并返回唯一权威材料快照", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      accepted: true,
      admission: {
        submitted_count: 1,
        added_count: 1,
        unsupported_count: 0,
        unreadable_count: 0,
        duplicate_count: 0,
        issues: []
      },
      material_context: snapshot(1, ["asset-retained"])
    }), {
      status: 202,
      headers: { "Content-Type": "application/json" }
    }));
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });
    const items = [
      {
        kind: "selected_text" as const,
        source: "selection_capture",
        display_name: "selected-text.txt",
        path: "G:\\materials\\selected-text.txt",
        managed_asset_id: "asset-retained"
      }
    ];

    await expect(client.appendStagedMaterials(items)).resolves.toEqual({
      admission: {
        submittedCount: 1,
        addedCount: 1,
        unsupportedCount: 0,
        unreadableCount: 0,
        duplicateCount: 0,
        issues: []
      },
      materialContext: snapshot(1, ["asset-retained"])
    });
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/materials/turn/staged", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items })
    });
  });

  it("拒绝缺失或计数不一致的材料接纳响应", async () => {
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => new Response(JSON.stringify({
        accepted: true,
        admission: {
          submitted_count: 1,
          added_count: 1,
          unsupported_count: 1,
          unreadable_count: 0,
          duplicate_count: 0
        }
      }), { status: 202, headers: { "Content-Type": "application/json" } }))
    });

    await expect(client.appendStagedMaterials([{
      kind: "dragged_file",
      source: "selection_capture",
      display_name: "bad.exe",
      path: "G:\\docs\\bad.exe",
    }])).rejects.toThrow("admission totals are inconsistent");
  });

  it("严格解析按提交顺序返回的材料问题明细", async () => {
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => new Response(JSON.stringify({
        accepted: true,
        admission: {
          submitted_count: 2,
          added_count: 0,
          unsupported_count: 1,
          unreadable_count: 1,
          duplicate_count: 0,
          issues: [
            { submitted_index: 0, display_name: "bad.exe", category: "unsupported", reason: "unsupported_extension" },
            { submitted_index: 1, display_name: "empty.txt", category: "unreadable", reason: "empty_file" }
          ]
        },
        material_context: snapshot(1)
      }), { status: 202, headers: { "Content-Type": "application/json" } }))
    });

    await expect(client.appendStagedMaterials([
      { kind: "dragged_file", source: "test", display_name: "bad.exe", path: "G:\\bad.exe" },
      { kind: "dragged_file", source: "test", display_name: "empty.txt", path: "G:\\empty.txt" }
    ])).resolves.toMatchObject({
      admission: {
        issues: [
          { submittedIndex: 0, displayName: "bad.exe", category: "unsupported", reason: "unsupported_extension" },
          { submittedIndex: 1, displayName: "empty.txt", category: "unreadable", reason: "empty_file" }
        ]
      }
    });
  });

  it("读取 Backend 当前材料权威快照用于不确定写入协调", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(snapshot(7)), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });

    await expect(client.readCurrentMaterialContext()).resolves.toEqual(snapshot(7));
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/materials/contexts/current"
    );
  });

  it("删除 staged 材料并返回权威 revision 快照", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      accepted: true,
      deleted_material_id: "material-1",
      deleted_material: { material_id: "material-1", display_name: "report.md" },
      released_managed_asset_ids: ["asset-1"],
      material_context: snapshot(2)
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: fetchMock
    });

    await expect(client.deleteStagedMaterial("material-1")).resolves.toEqual({
      deletedMaterialId: "material-1",
      deletedDisplayName: "report.md",
      releasedManagedAssetIds: ["asset-1"],
      materialContext: snapshot(2)
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/materials/turn/staged/material-1",
      { method: "DELETE" }
    );
  });

  it("通过 JSON body 忘记近期材料并返回权威快照", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      accepted: true,
      material_context: snapshot(3)
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    const client = createMaterialsClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });

    await expect(client.forgetRecentMaterial(" G:\\docs\\report.md ")).resolves.toEqual(snapshot(3));
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/materials/recent/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: "G:\\docs\\report.md" })
    });
  });
});

function snapshot(revision: number, managedAssetIds: string[] = []) {
  return {
    revision,
    turn_material_context: {
      staged_materials: managedAssetIds.map((managedAssetId, index) => ({
        material_id: `material-${index}`,
        turn_id: "staged",
        kind: "selected_text" as const,
        source: "selection_capture",
        display_name: `material-${index}.txt`,
        path: `G:\\materials\\material-${index}.txt`,
        node_kind: "file" as const,
        created_at: "2026-07-22T10:00:00+08:00",
        media_class: "text" as const,
        managed_asset_id: managedAssetId
      })),
      active: null
    },
    recent_materials: []
  };
}
