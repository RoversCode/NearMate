/**
 * AvatarMode 表示桌宠外壳当前暴露给渲染层的角色姿态。
 * 入参：无，作为 ShellState 的字段被 reducer 写入。
 * 输出：约束角色在不同交互阶段的可选模式字符串。
 * 边界：新增姿态时需要同步更新 reducer 分支，避免出现未处理状态。
 */
type AvatarMode =
  | "pass_through_idle"
  | "hover_revealed"
  | "dragging"
  | "material_drop_receiving";

/**
 * AvatarSummonMode 表示用户显式控制的桌宠召回状态。
 * 入参：无，作为 ShellState 的字段被 reducer 写入。
 * 输出：约束桌宠在默认显示、召回置顶和隐藏之间切换。
 * 边界：该状态只表达用户意图，不承载 hover 或拖拽这类瞬时交互。
 */
export type AvatarSummonMode =
  | "passive_visible"
  | "summoned_topmost"
  | "hidden";

/**
 * PanelMode 表示桌宠面板的生命周期阶段。
 * 入参：无，作为 ShellState 的字段被 reducer 写入。
 * 输出：约束面板从关闭到打开再到关闭过程的阶段字符串。
 * 边界：opening 只表示打开过程中的短暂状态，消费方不应假设它可长期停留。
 */
type PanelMode = "closed" | "opening" | "focused_open";

/**
 * PanelSide 表示面板优先停靠的方向。
 * 入参：无，通常由点击打开面板事件携带。
 * 输出：限制为 left 或 right，便于后续窗口层做定位。
 * 边界：如果未来支持更多停靠策略，需要同时扩展事件与状态定义。
 */
type PanelSide = "left" | "right";

/**
 * ShellState 是纯桌宠外壳状态快照。
 * 入参：无，由 reducer 基于事件逐步推导。
 * 输出：为窗口层和渲染层提供单一可信状态源。
 * 边界：该结构只描述桌宠壳状态，不承载业务任务或会话数据。
 */
export type ShellState = {
  avatarMode: AvatarMode;
  summonMode: AvatarSummonMode;
  panelMode: PanelMode;
  controlsVisible: boolean;
  preferredPanelSide: PanelSide;
  panelView: PanelView;
};

/**
 * ShellEvent 描述桌宠外壳 reducer 可以接收的全部事件。
 * 入参：来自角色悬停、拖拽、面板聚焦等交互信号。
 * 输出：作为 reduceShellState 的事件联合类型。
 * 边界：新增事件时必须在 reducer 中补齐分支，否则状态机会静默忽略新输入。
 */
export type ShellEvent =
  | { type: "avatar.hover_enter" }
  | { type: "avatar.hover_leave" }
  | { type: "avatar.material_drop_enter" }
  | { type: "avatar.material_drop_leave" }
  | { type: "avatar.visibility_toggle_requested" }
  | {
      type: "avatar.open_panel_clicked";
      payload: { preferredSide: PanelSide; view: PanelView };
    }
  | { type: "avatar.drag_started" }
  | { type: "avatar.drag_ended" }
  | { type: "panel.focused" }
  | { type: "panel.user_close_requested" }
  | { type: "panel.external_click_close" };
import type { PanelView } from "../../shared/shellNavigation";
