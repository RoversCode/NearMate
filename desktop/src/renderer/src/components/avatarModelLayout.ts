import {
  DEFAULT_AVATAR_PRESENTATION_PROFILE,
  type AvatarPresentationProfile
} from "../../../shared/avatarPresentationProfile";

type AvatarModelLayoutMode = "pet" | "panel";

type AvatarModelLayoutInput = {
  mode: AvatarModelLayoutMode;
  stageWidth: number;
  stageHeight: number;
  modelWidth: number;
  modelHeight: number;
  presentationProfile?: AvatarPresentationProfile;
};

type AvatarModelLayout = {
  scale: number;
  x: number;
  y: number;
};

/**
 * calculateAvatarModelLayout 负责把 Live2D 模型逻辑尺寸换算成 Pixi 舞台上的像素布局。
 * 核心入参是舞台像素尺寸、模型逻辑宽高和展示模式，输出为 scale 与锚点位置。
 * 边界异常：当模型尺寸尚未稳定或返回 0/NaN 时，回退到窗口中心底部的安全布局，避免渲染链产生无效矩阵。
 */
export function calculateAvatarModelLayout(input: AvatarModelLayoutInput): AvatarModelLayout {
  const {
    mode,
    stageWidth,
    stageHeight,
    modelWidth,
    modelHeight,
    presentationProfile = DEFAULT_AVATAR_PRESENTATION_PROFILE
  } = input;

  /**
   * 舞台尺寸来自 Pixi renderer，模型尺寸来自 Live2D internalModel。
   * 任一尺寸异常都说明还不能做比例换算，此时返回可预测的保守位置，方便继续渲染与诊断。
   */
  if (!isPositiveFinite(stageWidth) || !isPositiveFinite(stageHeight)) {
    return { scale: 1, x: 0, y: 0 };
  }

  if (!isPositiveFinite(modelWidth) || !isPositiveFinite(modelHeight)) {
    return {
      scale: 1,
      x:
        stageWidth *
        (mode === "pet" ? presentationProfile.layout.anchorX : 0.5),
      y:
        stageHeight *
        (mode === "pet" ? presentationProfile.layout.anchorY : 0.94)
    };
  }

  /**
   * 桌宠窗口要优先保证“人物本体肉眼可见”，面板预览则稍微克制，避免抢走控制面板层级。
   * 这里按宽高同时约束，既防止超出窗口，也防止 Cubism 逻辑尺寸被小数 scale 压到亚像素。
   */
  const targetWidthRatio =
    mode === "pet" ? presentationProfile.layout.targetWidthRatio : 0.54;
  const targetHeightRatio =
    mode === "pet" ? presentationProfile.layout.targetHeightRatio : 0.58;
  const widthScale = (stageWidth * targetWidthRatio) / modelWidth;
  const heightScale = (stageHeight * targetHeightRatio) / modelHeight;
  const scale = Math.min(widthScale, heightScale);

  return {
    scale,
    x: stageWidth * (mode === "pet" ? presentationProfile.layout.anchorX : 0.5),
    y: stageHeight * (mode === "pet" ? presentationProfile.layout.anchorY : 0.94)
  };
}

/**
 * isPositiveFinite 用于过滤几何换算中的无效输入。
 * 核心入参是待检查数值，输出为是否可安全参与除法和矩阵缩放。
 */
function isPositiveFinite(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}
