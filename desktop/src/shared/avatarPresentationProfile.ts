/**
 * AvatarPresentationProfile 统一描述某个人物在桌宠展示时的默认窗口与构图档案。
 * 核心字段包括窗口安全尺寸、最小可接受尺寸，以及 pet 模式下的模型构图比例、锚点与交互命中盒。
 * 边界：panel 模式不消费这里的 layout 参数，因为面板预览需要保持当前更克制的独立视觉比例。
 */
export type AvatarPresentationProfile = Readonly<{
  windowWidth: number;
  windowHeight: number;
  minWidth: number;
  minHeight: number;
  layout: Readonly<{
    targetWidthRatio: number;
    targetHeightRatio: number;
    anchorX: number;
    anchorY: number;
    headAnchor: Readonly<{
      x: number;
      y: number;
    }>;
    footAnchor: Readonly<{
      x: number;
      y: number;
    }>;
    interactionBounds?: Readonly<{
      widthRatio: number;
      heightRatio: number;
      offsetXRatio?: number;
      /** 相对模型画布高度移动交互边界，裁掉半身模型下方的透明留白。 */
      offsetYRatio?: number;
    }>;
    interactionHotspot?: AvatarInteractionHotspot;
  }>;
}>;

export type AvatarInteractionHotspot = Readonly<{
  xRatio: number;
  yRatio: number;
  widthRatio: number;
  heightRatio: number;
}>;

/**
 * DEFAULT_AVATAR_INTERACTION_HOTSPOT 是人物默认主交互热区。
 * 该矩形相对人物语义边界定位，用于首次激活和拖拽，避免整个人物外接盒挡住其它窗口。
 */
export const DEFAULT_AVATAR_INTERACTION_HOTSPOT: AvatarInteractionHotspot =
  Object.freeze({
    xRatio: 0.32,
    yRatio: 0.18,
    widthRatio: 0.36,
    heightRatio: 0.34
  });

/**
 * freezeAvatarPresentationProfile 负责把展示档案冻结成运行时不可变对象。
 * 核心入参是尚未冻结的档案字面量，输出为同结构的只读对象，并同步冻结 layout 子对象。
 */
function freezeAvatarPresentationProfile(
  profile: AvatarPresentationProfile
): AvatarPresentationProfile {
  Object.freeze(profile.layout.headAnchor);
  Object.freeze(profile.layout.footAnchor);

  if (profile.layout.interactionBounds) {
    Object.freeze(profile.layout.interactionBounds);
  }
  if (profile.layout.interactionHotspot) {
    Object.freeze(profile.layout.interactionHotspot);
  }

  Object.freeze(profile.layout);
  return Object.freeze(profile);
}

/**
 * DEFAULT_AVATAR_PRESENTATION_PROFILE 是所有未配置人物的稳定回退档案，
 * 同时也是主进程创建 avatar window 时唯一可依赖的安全默认窗口尺寸来源。
 */
export const DEFAULT_AVATAR_PRESENTATION_PROFILE: AvatarPresentationProfile =
  freezeAvatarPresentationProfile({
    windowWidth: 420,
    windowHeight: 520,
    minWidth: 360,
    minHeight: 420,
    layout: {
      targetWidthRatio: 0.88,
      targetHeightRatio: 0.9,
      anchorX: 0.52,
      anchorY: 0.98,
      headAnchor: {
        x: 0.54,
        y: 0.08
      },
      footAnchor: {
        x: 0.52,
        y: 1
      },
      interactionHotspot: DEFAULT_AVATAR_INTERACTION_HOTSPOT
    }
  });

/**
 * AVATAR_PRESENTATION_PROFILES 保存人物各自的构图与交互边界。
 * 半身模型按真实身体收窄热区，动作伸出的道具仍保留在窗口留白内。
 */
const AVATAR_PRESENTATION_PROFILES: Readonly<
  Record<string, AvatarPresentationProfile>
> = Object.freeze({
  DeepSeekChibi: freezeAvatarPresentationProfile({
    windowWidth: 480,
    windowHeight: 480,
    minWidth: 360,
    minHeight: 360,
    layout: {
      // 锤子会伸出原画布右缘，人物略偏左并留出两侧空间，避免动作被窗口裁掉。
      targetWidthRatio: 0.8,
      targetHeightRatio: 0.9,
      anchorX: 0.44,
      anchorY: 0.98,
      headAnchor: {
        x: 0.44,
        y: 0.04
      },
      footAnchor: {
        x: 0.5,
        y: 1
      },
      // 模型画布为正方形，桌前身体约占 x=0.13..0.98、y=0.08..0.87。
      interactionBounds: {
        widthRatio: 0.85,
        heightRatio: 0.79,
        offsetXRatio: 0.055,
        offsetYRatio: -0.13
      },
      interactionHotspot: {
        xRatio: 0.16,
        yRatio: 0.08,
        widthRatio: 0.48,
        heightRatio: 0.62
      }
    }
  }),
  Hiyori: freezeAvatarPresentationProfile({
    windowWidth: 640,
    windowHeight: 760,
    minWidth: 520,
    minHeight: 600,
    layout: {
      targetWidthRatio: 0.84,
      targetHeightRatio: 0.98,
      anchorX: 0.58,
      anchorY: 0.99,
      headAnchor: {
        x: 0.5,
        y: 0.07
      },
      footAnchor: {
        x: 0.5,
        y: 1
      },
      interactionBounds: {
        widthRatio: 0.64,
        heightRatio: 0.94,
        offsetXRatio: -0.08
      },
      interactionHotspot: DEFAULT_AVATAR_INTERACTION_HOTSPOT
    }
  })
});

/**
 * getAvatarPresentationProfile 负责根据人物目录名返回展示档案。
 * 核心入参是可选 modelId，输出为稳定可用的展示档案；未配置人物时会回退到默认档案。
 */
export function getAvatarPresentationProfile(
  modelId?: string
): AvatarPresentationProfile {
  const normalizedModelId = modelId?.trim();

  if (!normalizedModelId) {
    return DEFAULT_AVATAR_PRESENTATION_PROFILE;
  }

  return (
    AVATAR_PRESENTATION_PROFILES[normalizedModelId] ??
    DEFAULT_AVATAR_PRESENTATION_PROFILE
  );
}
