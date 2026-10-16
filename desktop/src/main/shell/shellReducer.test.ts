import { describe, expect, it } from "vitest";
import { initialShellState, reduceShellState } from "./shellReducer";

describe("shellReducer", () => {
  it("启动时保持角色可见、面板关闭、交互入口隐藏", () => {
    expect(initialShellState.avatarMode).toBe("pass_through_idle");
    expect(initialShellState.summonMode).toBe("passive_visible");
    expect(initialShellState.panelMode).toBe("closed");
    expect(initialShellState.panelView).toBe("models");
    expect(initialShellState.controlsVisible).toBe(false);
  });

  it("启动可见态收到显隐请求会隐藏桌宠并关闭面板状态", () => {
    const nextState = reduceShellState(initialShellState, {
      type: "avatar.visibility_toggle_requested"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.summonMode).toBe("hidden");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("隐藏态再次收到显隐请求会进入显式置顶召回态", () => {
    const hiddenState = reduceShellState(initialShellState, {
      type: "avatar.visibility_toggle_requested"
    });
    const nextState = reduceShellState(hiddenState, {
      type: "avatar.visibility_toggle_requested"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.summonMode).toBe("summoned_topmost");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("隐藏态会忽略 hover enter，避免被鼠标事件重新唤出", () => {
    const hiddenState = reduceShellState(initialShellState, {
      type: "avatar.visibility_toggle_requested"
    });

    const nextState = reduceShellState(hiddenState, {
      type: "avatar.hover_enter"
    });

    expect(nextState).toBe(hiddenState);
  });

  it("悬停角色后显露交互入口，但不自动打开面板", () => {
    const nextState = reduceShellState(initialShellState, {
      type: "avatar.hover_enter"
    });

    expect(nextState.avatarMode).toBe("hover_revealed");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(true);
  });

  it("面板关闭时移出角色，回到穿透待机并隐藏交互入口", () => {
    const hoveredState = reduceShellState(initialShellState, {
      type: "avatar.hover_enter"
    });

    const nextState = reduceShellState(hoveredState, {
      type: "avatar.hover_leave"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("面板未关闭时移出角色，也会恢复头像穿透待机", () => {
    const openedState = reduceShellState(initialShellState, {
      type: "avatar.open_panel_clicked",
      payload: { preferredSide: "left", view: "models" }
    });

    const nextState = reduceShellState(openedState, {
      type: "avatar.hover_leave"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.panelMode).toBe("opening");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("开始拖拽时进入 dragging，并保持交互入口可见", () => {
    const nextState = reduceShellState(initialShellState, {
      type: "avatar.drag_started"
    });

    expect(nextState.avatarMode).toBe("dragging");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(true);
  });

  it("材料拖拽进入头像窗口时临时收回穿透但不显示普通控制入口", () => {
    const nextState = reduceShellState(initialShellState, {
      type: "avatar.material_drop_enter"
    });

    expect(nextState.avatarMode).toBe("material_drop_receiving");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("材料拖拽离开或投递完成后回到穿透待机", () => {
    const receivingState = reduceShellState(initialShellState, {
      type: "avatar.material_drop_enter"
    });

    const nextState = reduceShellState(receivingState, {
      type: "avatar.material_drop_leave"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("拖拽结束且面板关闭时，回到 hover_revealed", () => {
    const draggingState = reduceShellState(initialShellState, {
      type: "avatar.drag_started"
    });

    const nextState = reduceShellState(draggingState, {
      type: "avatar.drag_ended"
    });

    expect(nextState.avatarMode).toBe("hover_revealed");
    expect(nextState.panelMode).toBe("closed");
    expect(nextState.controlsVisible).toBe(true);
  });

  it("拖拽结束且面板未关闭时，回到人物悬停交互态", () => {
    const openedState = reduceShellState(initialShellState, {
      type: "avatar.open_panel_clicked",
      payload: { preferredSide: "right", view: "models" }
    });

    const draggingState = reduceShellState(openedState, {
      type: "avatar.drag_started"
    });

    const nextState = reduceShellState(draggingState, {
      type: "avatar.drag_ended"
    });

    expect(nextState.avatarMode).toBe("hover_revealed");
    expect(nextState.panelMode).toBe("opening");
    expect(nextState.controlsVisible).toBe(true);
  });

  it("面板聚焦后进入 focused_open，并让头像窗口回到穿透待机", () => {
    const openedState = reduceShellState(initialShellState, {
      type: "avatar.open_panel_clicked",
      payload: { preferredSide: "right", view: "models" }
    });

    const nextState = reduceShellState(openedState, {
      type: "panel.focused"
    });

    expect(nextState.avatarMode).toBe("pass_through_idle");
    expect(nextState.panelMode).toBe("focused_open");
    expect(nextState.panelView).toBe("models");
    expect(nextState.controlsVisible).toBe(false);
  });

  it("用户主动关闭面板后回到桌宠基线状态", () => {
    const openedState = reduceShellState(initialShellState, {
      type: "avatar.open_panel_clicked",
      payload: { preferredSide: "right", view: "models" }
    });

    const closedState = reduceShellState(openedState, {
      type: "panel.user_close_requested"
    });

    expect(closedState.avatarMode).toBe("pass_through_idle");
    expect(closedState.panelMode).toBe("closed");
    expect(closedState.controlsVisible).toBe(false);
  });

  it("外部点击关闭面板后回到桌宠基线状态", () => {
    const openedState = reduceShellState(initialShellState, {
      type: "avatar.open_panel_clicked",
      payload: { preferredSide: "right", view: "models" }
    });

    const closedState = reduceShellState(openedState, {
      type: "panel.external_click_close"
    });

    expect(closedState.avatarMode).toBe("pass_through_idle");
    expect(closedState.panelMode).toBe("closed");
    expect(closedState.controlsVisible).toBe(false);
  });
});
