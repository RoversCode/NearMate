/**
 * AvatarViewState 只描述头像窗口自身的悬停展示状态。
 * 它不关心 Electron、模型加载或业务事件，方便单测稳定覆盖纯 UI 规则。
 */
export type AvatarViewState = {
  /**
   * controlsVisible 表示“打开面板”入口当前是否可见。
   * 这个状态仅由指针进入/离开驱动，避免把简单 hover 规则耦合进 React 组件内部。
   */
  controlsVisible: boolean;
};

/**
 * AvatarViewEvent 只保留当前任务需要的两种悬停事件。
 * 后续如果出现“拖拽中锁定显示”之类的新规则，再增量扩展即可。
 */
export type AvatarViewEvent =
  | {
      type: "pointer.enter";
    }
  | {
      type: "pointer.leave";
    };

/**
 * 初始态保持为纯桌宠展示，不提前露出任何悬浮控制。
 */
export const initialAvatarViewState: AvatarViewState = {
  controlsVisible: false
};

/**
 * reduceAvatarViewState 负责把简单悬停事件映射成稳定的显示状态。
 * 输入是上一帧纯状态与本次事件，输出是下一帧纯状态；没有副作用，也不会修改入参。
 */
export function reduceAvatarViewState(
  state: AvatarViewState,
  event: AvatarViewEvent
): AvatarViewState {
  if (event.type === "pointer.enter") {
    return {
      ...state,
      controlsVisible: true
    };
  }

  return {
    ...state,
    controlsVisible: false
  };
}
