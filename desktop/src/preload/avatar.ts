import { contextBridge, ipcRenderer, webUtils } from "electron";
import {
  RENDERER_DIAGNOSTIC_CHANNEL,
  type RendererDiagnosticLevel,
  type RendererDiagnosticPayload
} from "../shared/rendererDiagnostics";
import { readWindowBootstrapArgument } from "../shared/windowBootstrap";
import type {
  AvatarPresentationProfileApplyRequest,
  AvatarResizeDirection,
  AvatarPetBounds,
  AvatarVisibleArea,
  Live2dModelSwitchRequest,
  Live2dModelSwitchState
} from "../shared/live2dModelTypes";
import type { ConsoleTarget, PanelView } from "../shared/shellNavigation";
import { isShortcutFeedback, SHORTCUT_FEEDBACK_CHANNEL, type ShortcutFeedback } from "../shared/shortcutFeedback";

const bootstrap = readWindowBootstrapArgument(process.argv, "avatar");

/**
 * AvatarDragPoint 是 preload 接收 renderer 拖拽坐标的最小数据契约。
 * 入参来自头像拖拽层，输出通过 IPC 交给主进程；边界校验仍由主进程兜底，避免信任 renderer。
 */
type AvatarDragPoint = {
  screenX: number;
  screenY: number;
};

/**
 * AvatarResizeStartPayload 表示一次缩放会话的起始入参。
 * preload 只负责把方向与屏幕坐标原样转发给主进程，不在这里做任何几何推导。
 */
type AvatarResizeStartPayload = {
  direction: AvatarResizeDirection;
  point: AvatarDragPoint;
};

/**
 * avatar 窗口的 bridge 只暴露悬浮头像壳层当前必需的最小能力。
 * 这样 renderer 可以先独立启动，同时把更高风险的 Electron API 隔离在 preload。
 */
if (bootstrap) {
contextBridge.exposeInMainWorld("companionAvatar", {
  runtime: {
    /**
     * 控制网关地址暴露给头像窗口运行时，用于订阅气泡事件与提交权限决策。
     * 这里只提供 URL，不暴露 Electron 或 Node 能力，保持 renderer 边界可审计。
     * HTTP 地址会被归一化为控制服务根地址，避免误填 /console 页面后让 renderer 拼出错误接口路径。
     */
    controlGateway: bootstrap.runtime.controlGateway,
    /**
     * avatar 当前默认模型路径仍然通过环境变量覆盖规则得出。
     * 这里额外暴露只读 catalog，给后续本地模型切换链路提供稳定输入。
     */
    live2d: bootstrap.runtime.live2d
  },
  /**
   * renderer 自己的中文诊断通过显式 IPC 上报给主进程终端。
   * 这样可以避开 Electron console-message 转发在 Windows 开发终端中的乱码问题。
   */
  reportDiagnostic(level: RendererDiagnosticLevel, message: string, details?: unknown): void {
    ipcRenderer.send(RENDERER_DIAGNOSTIC_CHANNEL, {
      level,
      message,
      details
    } satisfies RendererDiagnosticPayload);
  },
  /**
   * 首次挂载时读取聊天窗口的权威折叠状态，避免展开态误显示待答气泡。
   */
  getChatMode(): Promise<"collapsed" | "expanded" | null> {
    return ipcRenderer.invoke("avatar:get-chat-mode");
  },
  /**
   * 订阅聊天窗口模式变化；主进程只会下发已经校验过的两种稳定模式。
   */
  onChatModeChanged(listener: (mode: "collapsed" | "expanded") => void): () => void {
    const channel = "avatar:chat-mode-changed";
    const wrappedListener = (_event: unknown, payload: unknown): void => {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return;
      }
      const mode = (payload as { mode?: unknown }).mode;
      if (mode === "collapsed" || mode === "expanded") {
        listener(mode);
      }
    };
    ipcRenderer.on(channel, wrappedListener);
    return () => ipcRenderer.removeListener(channel, wrappedListener);
  },
  /**
   * 订阅实际快捷键和取消回执，播放器可在网络完成前立即停声。
   * @param listener 接收已经校验的反馈。
   * @returns 移除当前订阅的函数。
   */
  onShortcutFeedback(listener: (feedback: ShortcutFeedback) => void): () => void {
    const wrapped = (_event: unknown, payload: unknown): void => {
      if (isShortcutFeedback(payload)) listener(payload);
    };
    ipcRenderer.on(SHORTCUT_FEEDBACK_CHANNEL, wrapped);
    return () => ipcRenderer.removeListener(SHORTCUT_FEEDBACK_CHANNEL, wrapped);
  },
  /**
   * 用户点击待答气泡时只提交请求编号，展开和聚焦动作由主进程完成。
   */
  openBackgroundAgentInteraction(requestId: string): void {
    ipcRenderer.send("avatar:open-background-agent-interaction", { requestId });
  },
  /**
   * 展开 Chat 由主进程统一定位、显示并聚焦；Avatar 只提交无参数意图。
   */
  openChat(): void {
    ipcRenderer.send("avatar:open-chat");
  },
  /**
   * avatar 接收拖拽材料时只加入本轮材料上下文，不触发 assistant 回复。
   */
  addStagedMaterials(items: unknown[]): Promise<unknown> {
    return ipcRenderer.invoke("materials:add-staged", { items });
  },
  /**
   * renderer 订阅材料成功加入本轮材料上下文后的本地交付反馈。
   * 业务意图：反馈只表达“资料已被产品接住”，不进入 assistant 对话历史。
   */
  onMaterialDeliveryFeedback(listener: (payload: unknown) => void): () => void {
    const channel = "avatar:material-delivery-feedback";
    const wrappedListener = (_event: unknown, payload: unknown) => {
      listener(payload);
    };

    ipcRenderer.on(channel, wrappedListener);
    return () => {
      ipcRenderer.removeListener(channel, wrappedListener);
    };
  },
  /**
   * renderer 不能直接读取 Node 文件系统；本地路径解析收敛在 Electron preload 边界。
   */
  getPathForFile(file: File): string {
    return webUtils.getPathForFile(file);
  },
  /**
   * renderer 通过主进程创建 LiveKit session。
   * 核心输入为空，预期输出保持 unknown，让 renderer 后续按自身运行时契约解析；边界异常由 ipcRenderer.invoke 原样返回 rejected promise。
   */
  createLiveKitSession(): Promise<unknown> {
    return ipcRenderer.invoke("livekit:create-session");
  },
  /**
   * 取消旧媒体会话请求，避免窗口卸载后迟到请求重新打开麦克风。
   * @returns 主进程已接收取消请求；实际资源收口由原会话任务等待。
   */
  cancelLiveKitSession(): Promise<void> {
    return ipcRenderer.invoke("livekit:cancel-session");
  },
  /**
   * avatar 订阅来自 panel 的切换请求，真正执行模型加载与回退逻辑。
   */
  onModelSwitchRequested(
    listener: (request: Live2dModelSwitchRequest) => void
  ): () => void {
    const channel = "avatar:model-switch-requested";
    const wrappedListener = (_event: unknown, payload: Live2dModelSwitchRequest) => {
      listener(payload);
    };

    ipcRenderer.on(channel, wrappedListener);

    return () => {
      ipcRenderer.removeListener(channel, wrappedListener);
    };
  },
  /**
   * avatar 完成加载后把结果上报给主进程，再由主进程转发回 panel。
   */
  reportModelSwitchState(state: Live2dModelSwitchState): void {
    ipcRenderer.send("avatar:model-switch-state-changed", state);
  },
  /**
   * 鼠标进入头像区域时通知主进程，为后续悬浮态交互预留稳定事件名。
   */
  hoverEnter(): void {
    ipcRenderer.send("avatar:hover-enter");
  },
  /**
   * 鼠标离开头像区域时通知主进程，便于后续收拢面板或结束悬浮反馈。
   */
  hoverLeave(): void {
    ipcRenderer.send("avatar:hover-leave");
  },
  /**
   * 外部材料拖入头像窗口时，renderer 请求主进程临时收回窗口穿透。
   */
  startMaterialDrop(): void {
    ipcRenderer.send("avatar:material-drop-enter");
  },
  /**
   * 材料投递结束或取消后，renderer 请求主进程恢复窗口穿透。
   */
  endMaterialDrop(): void {
    ipcRenderer.send("avatar:material-drop-leave");
  },
  /**
   * 头像请求展开 panel 时，只传递意图，不在 preload 中承担业务决策。
   */
  openPanel(view: PanelView): void {
    ipcRenderer.send("avatar:open-panel", { view });
  },
  /**
   * renderer 只能提交结构化 Console 目标，地址拼接与安全校验全部留在主进程。
   */
  openConsole(target: ConsoleTarget): void {
    ipcRenderer.send("desktop:open-console", target);
  },
  /**
   * 拖拽起始动作由主进程接管，避免 renderer 直接持有原生窗口控制能力。
   */
  startDrag(point: AvatarDragPoint): void {
    ipcRenderer.send("avatar:drag-start", point);
  },
  /**
   * 拖拽移动只转发屏幕坐标，真实窗口位移统一由主进程计算。
   */
  moveDrag(point: AvatarDragPoint): void {
    ipcRenderer.send("avatar:drag-move", point);
  },
  /**
   * 拖拽结束时通知主进程收口状态，避免窗口长期停留在不可穿透的拖拽态。
   */
  endDrag(): void {
    ipcRenderer.send("avatar:drag-end");
  },
  /**
   * 缩放起点只把方向与屏幕坐标转发给主进程。
   * preload 不参与窗口几何计算，避免把原生窗口行为拆散到不必要的层级。
   */
  startResize(direction: AvatarResizeDirection, point: AvatarDragPoint): void {
    ipcRenderer.send("avatar:resize-start", {
      direction,
      point
    } satisfies AvatarResizeStartPayload);
  },
  /**
   * 缩放过程持续上报当前屏幕坐标。
   * 主进程会结合会话起点和人物展示档案里的最小尺寸实时计算新边界。
   */
  moveResize(point: AvatarDragPoint): void {
    ipcRenderer.send("avatar:resize-move", point);
  },
  /**
   * 缩放结束后通知主进程清空当前会话快照。
   */
  endResize(): void {
    ipcRenderer.send("avatar:resize-end");
  },
  /**
   * renderer 在水合出当前人物后，请求主进程应用对应展示档案。
   * 这样主进程才能在首次 show 前把安全默认尺寸切换到人物真实默认尺寸。
   */
  applyPresentationProfile(modelId: string): void {
    ipcRenderer.send("avatar:apply-presentation-profile", {
      modelId
    } satisfies AvatarPresentationProfileApplyRequest);
  },
  /**
   * renderer 上报当前人物在窗口本地坐标中的可见边界。
   * 主进程只在关键节点读取这份边界来计算侧边窗锚点，不做持续追踪。
   */
  reportPetBounds(bounds: AvatarPetBounds | null): void {
    ipcRenderer.send("avatar:report-pet-bounds", bounds);
  },
  /**
   * renderer 订阅角色窗当前可见区变化。
   * 业务意图：主进程在拖拽、缩放与档案变更后推送窗口本地可见区，renderer 据此把脚部气泡钳在可见身体最底部。
   * 边界异常：返回取消订阅函数，组件卸载时必须调用，避免热重载累计重复监听。
   */
  onVisibleAreaChanged(
    listener: (visibleArea: AvatarVisibleArea) => void
  ): () => void {
    const channel = "avatar:visible-area-changed";
    const wrappedListener = (_event: unknown, payload: AvatarVisibleArea) => {
      listener(payload);
    };

    ipcRenderer.on(channel, wrappedListener);

    return () => {
      ipcRenderer.removeListener(channel, wrappedListener);
    };
  },
  /**
   * renderer 挂载可见区监听后主动拉取一次初始值。
   * 业务意图：首次 show 时主进程的 push 可能早于 renderer 监听挂载，这里用 pull 补齐初始可见区。
   * 边界异常：runtime 尚未就绪时主进程返回 null，renderer 回退到按窗口本地高度兜底。
   */
  requestVisibleArea(): Promise<AvatarVisibleArea | null> {
    return ipcRenderer.invoke("avatar:request-visible-area");
  }
});
}
