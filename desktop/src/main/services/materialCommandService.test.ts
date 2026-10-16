import { describe, expect, it, vi } from "vitest";
import { createMaterialCommandService } from "./materialCommandService";

describe("materialCommandService", () => {
  it("添加时统一发布反馈、释放未接纳副本并返回权威快照", async () => {
    const release = vi.fn(async () => true);
    const notifyMaterialProgress = vi.fn();
    const notifyMaterialAdmission = vi.fn(() => ({
      operation: "add" as const,
      kind: "success" as const,
      addedCount: 1,
      submittedCount: 2,
      text: "已添加 1 项",
      sound: "upload" as const
    }));
    const context = snapshot(4, ["asset-1"]);
    const service = createMaterialCommandService({
      materialsClient: {
        appendStagedMaterials: vi.fn(async () => ({
          admission: {
            submittedCount: 2,
            addedCount: 1,
            unsupportedCount: 1,
            unreadableCount: 0,
            duplicateCount: 0,
            issues: [{
              submittedIndex: 1,
              displayName: "bad.exe",
              category: "unsupported" as const,
              reason: "unsupported_extension"
            }]
          },
          materialContext: context
        }))
      } as never,
      managedAssetStore: { release },
      feedback: {
        notifyMaterialProgress,
        notifyMaterialAdmission,
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(service.add([
      item("asset-1", "a.md"),
      item("asset-2", "bad.exe")
    ])).resolves.toMatchObject({ materialContext: context });

    expect(notifyMaterialProgress).toHaveBeenCalledWith("正在检查 2 项…", 2);
    expect(release).toHaveBeenCalledWith("asset-2");
    expect(notifyMaterialAdmission).toHaveBeenCalledOnce();
  });

  it("删除以 Backend 快照确认目标消失并释放受管资产", async () => {
    const release = vi.fn(async () => true);
    const context = snapshot(8);
    const service = createMaterialCommandService({
      materialsClient: {
        deleteStagedMaterial: vi.fn(async () => ({
          deletedMaterialId: "material-1",
          deletedDisplayName: "a.md",
          releasedManagedAssetIds: ["asset-1"],
          materialContext: context
        }))
      } as never,
      managedAssetStore: { release },
      feedback: {
        notifyMaterialProgress: vi.fn(),
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(service.remove("material-1")).resolves.toEqual({
      materialContext: context,
      removedDisplayName: "a.md"
    });
    expect(release).toHaveBeenCalledWith("asset-1");
  });

  it("写入响应丢失后用权威快照确认受管资产并完成事务", async () => {
    const release = vi.fn(async () => true);
    const notifyMaterialWarning = vi.fn(() => ({
      operation: "add" as const,
      kind: "warning" as const,
      addedCount: 0,
      submittedCount: 0,
      text: "材料已登记，已从当前状态恢复确认"
    }));
    const context = snapshot(9, ["asset-1"]);
    const service = createMaterialCommandService({
      materialsClient: {
        appendStagedMaterials: vi.fn(async () => {
          throw new Error("response lost");
        }),
        readCurrentMaterialContext: vi.fn(async () => context)
      } as never,
      managedAssetStore: { release },
      feedback: {
        notifyMaterialProgress: vi.fn(),
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialWarning,
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(service.add([item("asset-1", "a.md")])).resolves.toEqual({
      feedback: expect.objectContaining({ kind: "warning" }),
      materialContext: context
    });
    expect(release).not.toHaveBeenCalled();
    expect(notifyMaterialWarning).toHaveBeenCalledWith(
      "材料已登记，已从当前状态恢复确认"
    );
  });

  it("权威快照未接纳不确定写入时释放副本并保留失败", async () => {
    const release = vi.fn(async () => true);
    const notifyMaterialWarning = vi.fn();
    const service = createMaterialCommandService({
      materialsClient: {
        appendStagedMaterials: vi.fn(async () => {
          throw new Error("response lost");
        }),
        readCurrentMaterialContext: vi.fn(async () => snapshot(10))
      } as never,
      managedAssetStore: { release },
      feedback: {
        notifyMaterialProgress: vi.fn(),
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialWarning,
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(service.add([item("asset-1", "a.md")])).rejects.toThrow("response lost");
    expect(release).toHaveBeenCalledWith("asset-1");
    expect(notifyMaterialWarning).toHaveBeenCalledWith("材料添加失败，请稍后重试");
  });

  it("忘记近期材料只委托 Backend，不触碰 staged 资产", async () => {
    const release = vi.fn(async () => true);
    const context = snapshot(11);
    const forgetRecentMaterial = vi.fn(async () => context);
    const service = createMaterialCommandService({
      materialsClient: { forgetRecentMaterial } as never,
      managedAssetStore: { release },
      feedback: {
        notifyMaterialProgress: vi.fn(),
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(service.forgetRecent("G:\\docs\\report.md")).resolves.toBe(context);
    expect(forgetRecentMaterial).toHaveBeenCalledWith("G:\\docs\\report.md");
    expect(release).not.toHaveBeenCalled();
  });
});

function item(assetId: string, name: string) {
  return {
    kind: "dragged_file" as const,
    source: "drag_drop",
    display_name: name,
    path: `G:\\docs\\${name}`,
    managed_asset_id: assetId
  };
}

function snapshot(revision: number, managedAssetIds: string[] = []) {
  return {
    revision,
    turn_material_context: {
      staged_materials: managedAssetIds.map((managedAssetId, index) => ({
        material_id: `material-${index}`,
        turn_id: "staged",
        kind: "dragged_file" as const,
        source: "drag_drop",
        display_name: `material-${index}.md`,
        path: `G:\\docs\\material-${index}.md`,
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
