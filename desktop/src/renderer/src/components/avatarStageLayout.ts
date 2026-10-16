import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import { calculateAvatarDragRegion, type AvatarDragRegion } from "../avatar/avatarDragRegion";
import { calculateAvatarModelLayout } from "./avatarModelLayout";
import type {
  AvatarStageLayoutApp,
  AvatarStageLayoutModel,
  AvatarStageLoadedModel
} from "./avatarStageTypes";

type ReportAvatarStagePetBoundsInput = {
  app: AvatarStageLayoutApp;
  mode: "pet" | "panel";
  model: AvatarStageLayoutModel;
  presentationProfile?: AvatarPresentationProfile;
  onPetBoundsChange?(bounds: AvatarDragRegion | null): void;
};

type SyncLoadedAvatarStageModelLayoutInput = {
  app: AvatarStageLayoutApp;
  mode: "pet" | "panel";
  model: AvatarStageLoadedModel;
  presentationProfile?: AvatarPresentationProfile;
  onPetBoundsChange?(bounds: AvatarDragRegion | null): void;
};

/**
 * reportAvatarStagePetBounds 负责把 Pixi 舞台与 Live2D 模型信息换算成 DOM 可用的桌宠拖拽边界。
 * 核心入参是 Pixi app 的 screen 坐标、当前展示模式、模型逻辑尺寸与上报回调，输出通过回调传递。
 * 边界异常：非 pet 模式没有桌宠拖拽面，会明确上报 null；模型尺寸异常由布局 helper 回退到稳定值。
 */
export function reportAvatarStagePetBounds({
  app,
  mode,
  model,
  presentationProfile,
  onPetBoundsChange
}: ReportAvatarStagePetBoundsInput) {
  const layout = calculateAvatarModelLayout({
    mode,
    stageWidth: app.screen.width,
    stageHeight: app.screen.height,
    modelWidth: model.internalModel.width,
    modelHeight: model.internalModel.height,
    presentationProfile
  });

  /**
   * 头像窗口只允许模型可见盒子成为拖拽区域。
   * 这里使用 Pixi screen 坐标而不是 renderer backing 像素，保证 DOM 拖拽层和模型显示位置在高分屏下仍对齐。
   */
  const petBounds =
    mode === "pet"
      ? calculateAvatarDragRegion({
          x: layout.x,
          y: layout.y,
          scale: layout.scale,
          modelWidth: model.internalModel.width,
          modelHeight: model.internalModel.height,
          interactionBounds: presentationProfile?.layout.interactionBounds
        })
      : null;

  onPetBoundsChange?.(petBounds);

  return layout;
}

/**
 * syncLoadedAvatarStageModelLayout 负责把“已挂到舞台上的当前模型”重新同步到最新窗口尺寸。
 * 核心入参是当前 Pixi app、已加载模型、展示模式与人物档案；输出为已应用到模型上的最新 layout。
 * 边界异常：仍复用统一布局计算与 petBounds 上报逻辑，避免首次加载与后续 resize 出现两套漂移规则。
 */
export function syncLoadedAvatarStageModelLayout({
  app,
  mode,
  model,
  presentationProfile,
  onPetBoundsChange
}: SyncLoadedAvatarStageModelLayoutInput) {
  const layout = reportAvatarStagePetBounds({
    app,
    mode,
    model,
    presentationProfile,
    onPetBoundsChange
  });

  model.scale.set(layout.scale);
  model.position.set(layout.x, layout.y);

  return layout;
}
