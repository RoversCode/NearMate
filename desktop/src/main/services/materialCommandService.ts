import type { MaterialContextSnapshot } from "../../shared/materialContext";
import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";
import type { MaterialDeliveryFeedback, MaterialDeliveryFeedbackService } from "./materialDeliveryFeedbackService";
import type { MaterialItemPayload, MaterialsClient } from "./materialsClient";

type MaterialCommandServiceOptions = {
  materialsClient: MaterialsClient;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "release">;
  feedback: MaterialDeliveryFeedbackService;
};

type MaterialAddCommandResult = {
  feedback: MaterialDeliveryFeedback | null;
  materialContext: MaterialContextSnapshot;
};

type MaterialRemoveCommandResult = {
  materialContext: MaterialContextSnapshot;
  removedDisplayName: string;
};

export type MaterialCommandService = {
  add(items: MaterialItemPayload[]): Promise<MaterialAddCommandResult | null>;
  remove(materialId: string): Promise<MaterialRemoveCommandResult>;
  clear(): Promise<MaterialContextSnapshot>;
  forgetRecent(path: string): Promise<MaterialContextSnapshot>;
};

/**
 * 创建所有显式材料 mutation 的唯一业务入口。
 *
 * @param options Backend 客户端、受管资产仓储和统一反馈总线。
 * @returns 可添加、删除和清空 staged 材料的命令服务。
 */
export function createMaterialCommandService(options: MaterialCommandServiceOptions): MaterialCommandService {
  return {
    async add(items) {
      if (items.length === 0) {
        return null;
      }

      options.feedback.notifyMaterialProgress(`正在检查 ${items.length} 项…`, items.length);
      let result;
      try {
        result = await options.materialsClient.appendStagedMaterials(items);
      } catch (error) {
        const reconciled = await reconcileUncertainAdd(options, items);
        if (reconciled !== null) {
          return reconciled;
        }
        options.feedback.notifyMaterialWarning("材料添加失败，请稍后重试");
        throw error;
      }
      const retainedIds = managedAssetIdsFromContext(result.materialContext);
      const rejectedAssetIds = items.flatMap(item => {
        const assetId = item.managed_asset_id?.trim();
        return assetId && !retainedIds.has(assetId) ? [assetId] : [];
      });
      await releaseManagedAssets(options.managedAssetStore, rejectedAssetIds);
      return {
        feedback: options.feedback.notifyMaterialAdmission(result.admission),
        materialContext: result.materialContext
      };
    },

    async remove(materialId) {
      options.feedback.clearFailureReport();
      const result = await options.materialsClient.deleteStagedMaterial(materialId);
      if (result.materialContext.turn_material_context.staged_materials.some(item => item.material_id === materialId)) {
        throw new Error("Backend 删除响应仍包含目标材料");
      }
      await releaseManagedAssets(options.managedAssetStore, result.releasedManagedAssetIds);
      return {
        materialContext: result.materialContext,
        removedDisplayName: result.deletedDisplayName
      };
    },

    async clear() {
      options.feedback.clearFailureReport();
      const result = await options.materialsClient.clearStagedMaterials();
      await releaseManagedAssets(options.managedAssetStore, result.releasedManagedAssetIds);
      return result.materialContext;
    },

    async forgetRecent(path) {
      return await options.materialsClient.forgetRecentMaterial(path);
    }
  };
}

async function reconcileUncertainAdd(
  options: MaterialCommandServiceOptions,
  items: MaterialItemPayload[]
): Promise<MaterialAddCommandResult | null> {
  /**
   * 只有全部输入都带受管编号时，权威快照才能无歧义地确认这次写入结果。
   * 查询本身失败时保留本地副本，避免误删 Backend 可能已经持有的文件。
   */
  const submittedAssetIds = items.map(item => item.managed_asset_id?.trim() ?? "");
  if (submittedAssetIds.some(assetId => !assetId)) {
    return null;
  }

  let materialContext: MaterialContextSnapshot;
  try {
    materialContext = await options.materialsClient.readCurrentMaterialContext();
  } catch {
    return null;
  }

  const retainedAssetIds = managedAssetIdsFromContext(materialContext);
  const missingAssetIds = submittedAssetIds.filter(assetId => !retainedAssetIds.has(assetId));
  await releaseManagedAssets(options.managedAssetStore, missingAssetIds);

  const retainedCount = submittedAssetIds.length - missingAssetIds.length;
  if (retainedCount === 0) {
    return null;
  }
  const feedback = options.feedback.notifyMaterialWarning(
    retainedCount === submittedAssetIds.length
      ? "材料已登记，已从当前状态恢复确认"
      : `已确认 ${retainedCount} 项材料，${missingAssetIds.length} 项未添加`
  );
  return { feedback, materialContext };
}

function managedAssetIdsFromContext(context: MaterialContextSnapshot): Set<string> {
  /**
   * 材料快照是受管资产所有权的唯一事实源；禁止再与平行 ID 列表做二次合并。
   */
  return new Set([
    ...context.turn_material_context.staged_materials,
    ...(context.turn_material_context.active?.attachments ?? [])
  ].flatMap(item => item.managed_asset_id ? [item.managed_asset_id] : []));
}

async function releaseManagedAssets(
  store: Pick<ManagedMaterialAssetStore, "release">,
  assetIds: string[]
): Promise<void> {
  /**
   * 同一资产也可能由 WebSocket 生命周期观察器先释放；release 的幂等 false 不属于错误。
   */
  await Promise.all([...new Set(assetIds)].map(assetId => store.release(assetId)));
}
