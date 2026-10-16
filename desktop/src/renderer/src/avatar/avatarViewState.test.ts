import { describe, expect, it } from "vitest";
import { initialAvatarViewState, reduceAvatarViewState } from "./avatarViewState";

describe("avatarViewState", () => {
  it("默认隐藏悬停入口", () => {
    expect(initialAvatarViewState.controlsVisible).toBe(false);
  });

  it("悬停后显示面板入口", () => {
    const nextState = reduceAvatarViewState(initialAvatarViewState, {
      type: "pointer.enter"
    });

    expect(nextState.controlsVisible).toBe(true);
  });

  it("离开后恢复为纯桌宠显示", () => {
    const hoveredState = reduceAvatarViewState(initialAvatarViewState, {
      type: "pointer.enter"
    });
    const nextState = reduceAvatarViewState(hoveredState, {
      type: "pointer.leave"
    });

    expect(nextState.controlsVisible).toBe(false);
  });
});
