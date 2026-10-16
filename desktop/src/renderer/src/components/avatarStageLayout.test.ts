// @vitest-environment jsdom

import { describe, expect, it, vi } from "vitest";

import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import {
  reportAvatarStagePetBounds,
  syncLoadedAvatarStageModelLayout
} from "./avatarStageLayout";

describe("avatarStageLayout", () => {
  it("上报基于 Pixi screen 坐标的桌宠拖拽边界，而不是 renderer backing 像素", () => {
    const onPetBoundsChange = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600
      }
    };

    reportAvatarStagePetBounds({
      app: {
        renderer: {
          width: 840,
          height: 1040
        },
        screen: {
          width: 420,
          height: 520
        }
      },
      mode: "pet",
      model,
      onPetBoundsChange
    });

    /**
     * 以 screen 420x520 计算时，pet 模式高度约束为 520 * 0.9，
     * scale = 468 / 600 = 0.78，位置为 x=218.4、y=509.6，拖拽盒四舍五入后如下。
     * 如果误用 renderer 840x1040，宽高与位置会翻倍，这个断言会失败。
     */
    expect(onPetBoundsChange).toHaveBeenCalledWith({
      x: 101,
      y: 42,
      width: 234,
      height: 468
    });
  });

  it("传入 Hiyori 档案后，桌宠模式会按人物构图上报更大的可见拖拽区域", () => {
    const onPetBoundsChange = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600
      }
    };

    reportAvatarStagePetBounds({
      app: {
        renderer: {
          width: 840,
          height: 1040
        },
        screen: {
          width: 420,
          height: 520
        }
      },
      mode: "pet",
      model,
      presentationProfile: getAvatarPresentationProfile("Hiyori"),
      onPetBoundsChange
    });

    /**
     * Hiyori 使用更宽的默认窗口与偏移后的交互盒：
     * scale = min(420 * 0.84 / 300, 520 * 0.98 / 600) = 0.849333...
     * x = 420 * 0.58 = 243.6，y = 520 * 0.99 = 514.8。
     * 最终透明拖拽盒会按 Hiyori 交互档案收窄并左移，对齐人物真实视觉重心。
     */
    expect(onPetBoundsChange).toHaveBeenCalledWith({
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
  });

  it("已加载模型在窗口尺寸变化后，会继续按当前 Hiyori 档案重布局并更新 petBounds", () => {
    const onPetBoundsChange = vi.fn();
    const scaleSet = vi.fn();
    const positionSet = vi.fn();
    const model = {
      internalModel: {
        width: 300,
        height: 600
      },
      scale: {
        set: scaleSet
      },
      position: {
        set: positionSet
      }
    };
    const presentationProfile = getAvatarPresentationProfile("Hiyori");

    const initialLayout = syncLoadedAvatarStageModelLayout({
      app: {
        renderer: {
          width: 840,
          height: 1040
        },
        screen: {
          width: 420,
          height: 520
        }
      },
      mode: "pet",
      model,
      presentationProfile,
      onPetBoundsChange
    });

    const resizedLayout = syncLoadedAvatarStageModelLayout({
      app: {
        renderer: {
          width: 1120,
          height: 1520
        },
        screen: {
          width: 560,
          height: 760
        }
      },
      mode: "pet",
      model,
      presentationProfile,
      onPetBoundsChange
    });

    /**
     * 第一次同步锁住当前 Hiyori 档案在 420x520 下的初始构图，
     * 第二次同步模拟窗口被放大后，已加载模型不重新载入文件，只根据新尺寸继续更新 scale、position 与拖拽边界。
     */
    expect(initialLayout.scale).toBeCloseTo(0.8493333333333334);
    expect(initialLayout.x).toBeCloseTo(243.6);
    expect(initialLayout.y).toBeCloseTo(514.8);
    expect(resizedLayout.scale).toBeCloseTo(1.2413333333333332);
    expect(resizedLayout.x).toBeCloseTo(324.8);
    expect(resizedLayout.y).toBeCloseTo(752.4);
    expect(scaleSet).toHaveBeenNthCalledWith(1, expect.closeTo(0.8493333333333334, 12));
    expect(positionSet).toHaveBeenNthCalledWith(1, 243.6, 514.8);
    expect(scaleSet).toHaveBeenNthCalledWith(2, expect.closeTo(1.2413333333333332, 12));
    expect(positionSet).toHaveBeenNthCalledWith(
      2,
      expect.closeTo(324.8, 12),
      752.4
    );
    expect(onPetBoundsChange).toHaveBeenNthCalledWith(1, {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
    expect(onPetBoundsChange).toHaveBeenNthCalledWith(2, {
      x: 176,
      y: 52,
      width: 238,
      height: 700
    });
  });
});
