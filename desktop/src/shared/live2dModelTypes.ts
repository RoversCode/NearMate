/**
 * Live2dModelCatalogEntry 表示一个可供桌宠切换的本地模型。
 * id 与 label 当前都直接沿用资源一级目录名，modelPath 则是 renderer 可直接访问的公共资源 URL。
 */
export type Live2dModelCatalogEntry = {
  id: string;
  label: string;
  modelPath: string;
};

/**
 * 模型目录发现最终产出的就是一个稳定有序的只读目录列表。
 * 单独导出数组类型，目的是让 main、preload、renderer 共享同一份契约。
 */
export type Live2dModelCatalog = Live2dModelCatalogEntry[];

/**
 * AvatarResizeDirection 描述桌宠窗口允许的八方向缩放手柄。
 * 主进程与 renderer 需要共享同一套方向字面量，避免 IPC 两端各自维护字符串导致漂移。
 */
export type AvatarResizeDirection =
  | "n"
  | "s"
  | "e"
  | "w"
  | "ne"
  | "nw"
  | "se"
  | "sw";

/**
 * 人物面板提交模型目录名与本次请求编号；旧调用方可省略编号。
 * 新面板用编号区分同一模型的重试，迟到回执不能影响新请求。
 *
 * Args:
 *   modelId: 目标模型的稳定目录名。
 *   requestId: 当前点击的标准 UUID；旧调用方可省略。
 * Returns:
 *   主进程原样转发给 Avatar 的切换意图。
 */
export type Live2dModelSwitchRequest = {
  modelId: string;
  requestId?: string;
};

/**
 * AvatarPresentationProfileApplyRequest 表示 renderer 请求主进程应用某个人物展示档案。
 * 当前请求只依赖稳定的 modelId，具体窗口尺寸与最小缩放尺寸都由 main 从共享档案表中解析。
 */
export type AvatarPresentationProfileApplyRequest = {
  modelId: string;
};

/**
 * AvatarPetBounds 表示 renderer 计算出的真实人物可见边界。
 * 坐标以 avatar 窗口左上角为原点，主进程会在需要侧边窗定位时换算成屏幕坐标。
 */
export type AvatarPetBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarVisibleArea 描述「角色窗口当前有多少落在显示器工作区内」，全部以窗口本地坐标表示。
 * 主进程在拖拽/缩放/移动/显示器变化时计算并推给 renderer；renderer 据此把脚部气泡钳在「可见身体最底部」之上。
 * 这是本契约里唯一的 screen-aware 通道：renderer 自身不知道窗口的物理屏幕坐标，只消费这个本地坐标结果。
 */
export type AvatarVisibleArea = {
  visibleTop: number;
  visibleBottom: number;
  visibleLeft: number;
  visibleRight: number;
};

/**
 * Live2dModelSwitchState 表示 avatar 本地切换状态机对 panel 可见的最小事件。
 * 这里不用布尔 success，是为了给后续“开始切换 / 切换成功 / 切换失败”三态保留稳定语义。
 *
 * Args:
 *   modelId: 本次请求的目标目录名。
 *   requestId: 原请求携带的编号；旧请求及启动故障通知可省略。
 *   status: 接纳、成功或失败状态；失败时附有 reason。
 * Returns:
 *   人物面板可按请求编号过滤的最小回执。
 */
export type Live2dModelSwitchState = { requestId?: string } & (
  | {
      status: "switching";
      modelId: string;
    }
  | {
      status: "succeeded";
      modelId: string;
    }
  | {
      status: "failed";
      modelId: string;
      reason: string;
    });
