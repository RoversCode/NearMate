import { describe, expect, it } from "vitest";
import {
  DEFAULT_AVATAR_PRESENTATION_PROFILE,
  getAvatarPresentationProfile
} from "./avatarPresentationProfile";

describe("avatarPresentationProfile", () => {
  it("未指定人物或人物未配置时，会稳定回退到默认展示档案", () => {
    expect(getAvatarPresentationProfile(undefined)).toBe(
      DEFAULT_AVATAR_PRESENTATION_PROFILE
    );
    expect(getAvatarPresentationProfile("UnknownModel")).toBe(
      DEFAULT_AVATAR_PRESENTATION_PROFILE
    );
  });

  it("Hiyori 的关键展示档案使用明确且稳定的配置值", () => {
    const hiyoriProfile = getAvatarPresentationProfile("Hiyori");

    /**
     * 这里使用精确值断言，而不是只比较“比默认更大一点”，
     * 目的是直接锁住 Hiyori 的关键视觉规格，避免后续改动把人物主视觉悄悄漂移掉。
     */
    expect(hiyoriProfile.windowWidth).toBe(640);
    expect(hiyoriProfile.windowHeight).toBe(760);
    expect(hiyoriProfile.minWidth).toBe(520);
    expect(hiyoriProfile.minHeight).toBe(600);
    expect(hiyoriProfile.layout.targetWidthRatio).toBe(0.84);
    expect(hiyoriProfile.layout.targetHeightRatio).toBe(0.98);
    expect(hiyoriProfile.layout.anchorX).toBe(0.58);
    expect(hiyoriProfile.layout.anchorY).toBe(0.99);
    expect(hiyoriProfile.layout.headAnchor.x).toBe(0.5);
    expect(hiyoriProfile.layout.headAnchor.y).toBe(0.07);
    expect(hiyoriProfile.layout.footAnchor.x).toBe(0.5);
    expect(hiyoriProfile.layout.footAnchor.y).toBe(1);
    expect(hiyoriProfile.layout.interactionBounds?.widthRatio).toBe(0.64);
    expect(hiyoriProfile.layout.interactionBounds?.heightRatio).toBe(0.94);
    expect(hiyoriProfile.layout.interactionBounds?.offsetXRatio).toBe(-0.08);
    expect(hiyoriProfile.layout.interactionHotspot).toEqual({
      xRatio: 0.32,
      yRatio: 0.18,
      widthRatio: 0.36,
      heightRatio: 0.34
    });
  });

  it("默认档案与人物档案都会以冻结对象形式暴露，避免共享真相源被误改", () => {
    const hiyoriProfile = getAvatarPresentationProfile("Hiyori");

    /**
     * 展示档案会同时被 main 与 renderer 复用，
     * 所以这里必须锁住外层对象与 layout 子对象，避免任一消费者在运行时意外篡改共享配置。
     */
    expect(Object.isFrozen(DEFAULT_AVATAR_PRESENTATION_PROFILE)).toBe(true);
    expect(Object.isFrozen(DEFAULT_AVATAR_PRESENTATION_PROFILE.layout)).toBe(true);
    expect(Object.isFrozen(DEFAULT_AVATAR_PRESENTATION_PROFILE.layout.headAnchor)).toBe(true);
    expect(Object.isFrozen(DEFAULT_AVATAR_PRESENTATION_PROFILE.layout.footAnchor)).toBe(true);
    expect(Object.isFrozen(DEFAULT_AVATAR_PRESENTATION_PROFILE.layout.interactionHotspot)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile.layout)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile.layout.headAnchor)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile.layout.footAnchor)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile.layout.interactionBounds)).toBe(true);
    expect(Object.isFrozen(hiyoriProfile.layout.interactionHotspot)).toBe(true);
  });
});
