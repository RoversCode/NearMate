import type { ShellEvent, ShellState } from "./shellTypes";

/**
 * initialShellState 定义桌宠外壳在应用启动瞬间的基线状态。
 * 入参：无。
 * 输出：提供 reducer 与测试共享的默认状态常量。
 * 边界：启动时只保留最基础的桌宠壳可见性，不主动展开任何附属 UI。
 */
export const initialShellState: ShellState = {
  avatarMode: "pass_through_idle",
  summonMode: "passive_visible",
  panelMode: "closed",
  controlsVisible: false,
  preferredPanelSide: "right",
  panelView: "models"
};

/**
 * reduceShellState 根据单个壳层事件推导下一份纯状态。
 * 入参：当前 ShellState 与一条 ShellEvent。
 * 输出：新的不可变状态快照。
 * 边界：所有事件分支都必须在这里被穷尽处理，避免未来新增事件时静默退化成无操作。
 */
export function reduceShellState(state: ShellState, event: ShellEvent): ShellState {
  switch (event.type) {
    case "avatar.hover_enter":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        // 面板打开不再改变 avatar 鼠标接管语义；只有真实人物命中才进入悬停交互。
        avatarMode: "hover_revealed",
        controlsVisible: true
      };

    case "avatar.hover_leave":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        avatarMode: "pass_through_idle",
        controlsVisible: false
      };

    case "avatar.material_drop_enter":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        // 材料投递只需要窗口接收系统 drag/drop，不应该顺带展开普通悬浮控制。
        avatarMode: "material_drop_receiving",
        controlsVisible: false
      };

    case "avatar.material_drop_leave":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        avatarMode: "pass_through_idle",
        controlsVisible: false
      };

    case "avatar.visibility_toggle_requested":
      if (state.summonMode !== "hidden") {
        return {
          ...state,
          avatarMode: "pass_through_idle",
          summonMode: "hidden",
          panelMode: "closed",
          controlsVisible: false
        };
      }

      return {
        ...state,
        avatarMode: "pass_through_idle",
        summonMode: "summoned_topmost",
        controlsVisible: false
      };

    case "avatar.open_panel_clicked":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        avatarMode: "pass_through_idle",
        panelMode: "opening",
        controlsVisible: false,
        preferredPanelSide: event.payload.preferredSide,
        panelView: event.payload.view
      };

    case "avatar.drag_started":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        avatarMode: "dragging",
        controlsVisible: true
      };

    case "avatar.drag_ended":
      if (state.summonMode === "hidden") {
        return state;
      }

      return {
        ...state,
        // 拖拽结束后回到悬停交互态，由下一次 hover_leave 决定是否恢复穿透。
        avatarMode: "hover_revealed",
        controlsVisible: true
      };

    case "panel.focused":
      return {
        ...state,
        avatarMode: "pass_through_idle",
        panelMode: "focused_open",
        controlsVisible: false
      };

    case "panel.user_close_requested":
    case "panel.external_click_close":
      return {
        ...state,
        avatarMode: "pass_through_idle",
        panelMode: "closed",
        controlsVisible: false
      };
  }

  // 这里故意保留编译期穷尽检查，确保未来给 ShellEvent 加新分支时会立刻在 reducer 处报错。
  const exhaustiveEvent: never = event;
  return exhaustiveEvent;
}
