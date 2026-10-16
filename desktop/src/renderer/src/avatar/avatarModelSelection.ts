import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import {
  isSelectedLive2dModelAvailable,
  resolveSelectedLive2dModelPath
} from "../../../shared/live2dModelPath";
import type { CompanionAvatarBridge } from "./avatarBridge";
import type { AvatarRuntime } from "./useAvatarRuntime";

type ResolveAvatarModelSelectionInput = {
  avatarBridge?: CompanionAvatarBridge;
  avatarRuntime: Pick<AvatarRuntime, "settings" | "settingsHydrated">;
  pendingModelId: string | null;
};

/**
 * resolveAvatarModelSelection 集中计算头像当前应加载的模型路径与展示档案。
 *
 * Args:
 *   input: preload 模型目录、当前运行时设置和 pending 切换 id。
 *
 * Returns:
 *   舞台加载路径、展示档案和用于切换状态机的模型真相源。
 */
export function resolveAvatarModelSelection({
  avatarBridge,
  avatarRuntime,
  pendingModelId
}: ResolveAvatarModelSelectionInput) {
  /**
   * 模型目录快照来自 preload。
   * 这里抽成局部常量，避免后面多次从可选链里重复读取，影响 JSX 与卫语句可读性。
   */
  const modelCatalog = avatarBridge?.runtime.live2d.catalog ?? [];
  /**
   * fallbackModelPath 单独抽出来，确保“目标人物路径解析”和“当前实际展示人物识别”都基于同一条回退路径。
   * 这样当设置里的人物已经失效时，后续档案与窗口 profile 的判断也能对齐真正会显示出来的默认人物。
   */
  const fallbackModelPath = avatarBridge?.runtime.live2d.defaultModelPath ?? "";
  /**
   * targetModelId 只服务于“下一次要加载哪个模型文件”。
   * 即使 pending 切换已经开始，旧模型在真正提交成功前仍然可能继续留在舞台上，所以展示档案不能直接跟着它跳。
   */
  const committedSelectedModelId = avatarRuntime.settings?.selected_live2d_model_id;
  const targetModelId = pendingModelId ?? committedSelectedModelId;
  /**
   * committedResolvedModelPath 表示“当前已提交设置最终会展示哪条模型路径”。
   * 它既覆盖正常已选人物，也覆盖“设置里记的是失效人物，但实际只能回退到默认人物”的场景。
   */
  const committedResolvedModelPath = resolveSelectedLive2dModelPath({
    catalog: modelCatalog,
    selectedModelId: committedSelectedModelId,
    fallbackModelPath
  });
  /**
   * committedPresentationModelId 是当前舞台真正已经在展示的人物真相源。
   * pending 切换尚未提交成功前，舞台 profile 仍应锚定这里，而不是提前跳到待切换的人物。
   */
  const committedPresentationModelId =
    modelCatalog.find((catalogEntry) => catalogEntry.modelPath === committedResolvedModelPath)?.id ??
    committedSelectedModelId ??
    null;
  const resolvedModelPath = resolveSelectedLive2dModelPath({
    catalog: modelCatalog,
    selectedModelId: targetModelId,
    fallbackModelPath
  });
  /**
   * stagedPresentationModelId 描述“当前这一次渲染应该把哪一个人物的展示档案交给 AvatarStage”。
   * 有 pending 时仍沿用已提交人物的档案；没有 pending 时则跟随真正会显示的那条模型路径，
   * 这样失效人物回退成默认人物时，不会再错误套用失效 id 的自定义窗口 profile。
   */
  const stagedPresentationModelId = pendingModelId
    ? committedPresentationModelId
    : modelCatalog.find((catalogEntry) => catalogEntry.modelPath === resolvedModelPath)?.id ??
      targetModelId ??
      null;
  /**
   * 设置里记住的人物目录名，可能因为资源升级或当前 core 兼容性变化而失效。
   * 这里单独显式感知，便于首次启动时即使没有 pending 切换，也能给出更具体的失败文案。
   */
  const rememberedModelUnavailable =
    !pendingModelId &&
    avatarRuntime.settingsHydrated &&
    !!avatarRuntime.settings?.selected_live2d_model_id &&
      !isSelectedLive2dModelAvailable({
        catalog: modelCatalog,
        selectedModelId: avatarRuntime.settings.selected_live2d_model_id
      });

  return {
    committedPresentationModelId,
    committedSelectedModelId,
    fallbackModelPath,
    modelCatalog,
    presentationProfile: getAvatarPresentationProfile(stagedPresentationModelId ?? undefined),
    rememberedModelUnavailable,
    resolvedModelPath,
    targetModelId
  };
}
