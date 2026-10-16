import { describe, expect, it } from "vitest";
import {
  DEFAULT_AVATAR_PRESENTATION_PROFILE,
  getAvatarPresentationProfile
} from "../../../shared/avatarPresentationProfile";
import { calculateAvatarModelLayout } from "./avatarModelLayout";

describe("calculateAvatarModelLayout", () => {
  it("桌宠模式会优先使用展示档案里的构图比例", () => {
    const layout = calculateAvatarModelLayout({
      mode: "pet",
      stageWidth: 420,
      stageHeight: 520,
      modelWidth: 2,
      modelHeight: 2,
      presentationProfile: DEFAULT_AVATAR_PRESENTATION_PROFILE
    });

    /**
     * 这里直接对默认档案中的桌宠构图参数做精确断言，
     * 目的是防止未来又把 pet 模式比例悄悄散落回组件内部硬编码。
     */
    expect(layout.scale).toBeCloseTo(184.8);
    expect(layout.x).toBeCloseTo(218.4);
    expect(layout.y).toBeCloseTo(509.6);
  });

  it("Hiyori 在桌宠模式下使用更贴近上半身的大头像构图", () => {
    const layout = calculateAvatarModelLayout({
      mode: "pet",
      stageWidth: 640,
      stageHeight: 760,
      modelWidth: 2,
      modelHeight: 2,
      presentationProfile: getAvatarPresentationProfile("Hiyori")
    });

    /**
     * 这里直接锁住 Hiyori 的桌宠展示结果，
     * 这样一旦人物档案被改小或锚点漂移，测试会第一时间指出真实视觉回归。
     */
    expect(layout.scale).toBeCloseTo(268.8);
    expect(layout.x).toBeCloseTo(371.2);
    expect(layout.y).toBeCloseTo(752.4);
  });

  it("面板模式继续使用既有的克制比例，不受人物展示档案影响", () => {
    const layout = calculateAvatarModelLayout({
      mode: "panel",
      stageWidth: 620,
      stageHeight: 540,
      modelWidth: 2,
      modelHeight: 2,
      presentationProfile: getAvatarPresentationProfile("Hiyori")
    });

    expect(layout.scale).toBeCloseTo(156.6);
    expect(layout.x).toBeCloseTo(310);
    expect(layout.y).toBeCloseTo(507.6);
  });

  it("模型尺寸异常时会按展示档案锚点回退到稳定位置，避免渲染链出现 NaN", () => {
    const layout = calculateAvatarModelLayout({
      mode: "pet",
      stageWidth: 640,
      stageHeight: 760,
      modelWidth: 0,
      modelHeight: 0,
      presentationProfile: getAvatarPresentationProfile("Hiyori")
    });

    expect(layout.scale).toBe(1);
    expect(layout.x).toBeCloseTo(371.2);
    expect(layout.y).toBeCloseTo(752.4);
  });
});
