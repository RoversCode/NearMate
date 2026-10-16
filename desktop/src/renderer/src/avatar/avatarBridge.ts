import type {
  AvatarResizeDirection,
  AvatarVisibleArea,
  Live2dModelSwitchRequest,
  Live2dModelSwitchState
} from "../../../shared/live2dModelTypes";
import type { AvatarDragPoint } from "../components/AvatarDragSurface";
import type { AvatarDragRegion } from "./avatarDragRegion";
import type { ConsoleTarget, PanelView } from "../../../shared/shellNavigation";
import type { ShortcutFeedback } from "../../../shared/shortcutFeedback";

/**
 * avatar bridge 只声明头像窗口当前需要的最小 preload 契约。
 * 这样可以把本任务的窗口拆分边界收敛在组件附近，避免额外扩散共享类型。
 */
export type CompanionAvatarBridge = {
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  getChatMode?(): Promise<"collapsed" | "expanded" | null>;
  onChatModeChanged?(listener: (mode: "collapsed" | "expanded") => void): () => void;
  onShortcutFeedback?(listener: (feedback: ShortcutFeedback) => void): () => void;
  openBackgroundAgentInteraction?(requestId: string): void;
  openChat?(): void;
  runtime: {
    controlGateway: {
      websocketUrl: string;
      httpBaseUrl: string;
    };
    live2d: {
      defaultModelPath: string;
      catalog: Array<{
        id: string;
        label: string;
        modelPath: string;
      }>;
    };
  };
  onModelSwitchRequested(listener: (request: Live2dModelSwitchRequest) => void): () => void;
  reportModelSwitchState(state: Live2dModelSwitchState): void;
  hoverEnter(): void;
  hoverLeave(): void;
  startMaterialDrop?(): void;
  endMaterialDrop?(): void;
  onMaterialDeliveryFeedback?(listener: (payload: unknown) => void): () => void;
  startDrag(point: AvatarDragPoint): void;
  moveDrag(point: AvatarDragPoint): void;
  endDrag(): void;
  startResize(direction: AvatarResizeDirection, point: AvatarDragPoint): void;
  moveResize(point: AvatarDragPoint): void;
  endResize(): void;
  applyPresentationProfile(modelId: string): void;
  reportPetBounds?(bounds: AvatarDragRegion | null): void;
  addStagedMaterials?(items: unknown[]): Promise<unknown>;
  getPathForFile?(file: File): string;
  openPanel(view: PanelView): void;
  /**
   * openConsole 提交经过类型约束的 Console 导航目标。
   * 浏览器预览缺失 preload 时该能力为空，renderer 不自行打开外部地址。
   */
  openConsole?(target: ConsoleTarget): void;
  /**
   * onVisibleAreaChanged 订阅主进程推送的窗口本地可见区变化。
   * 核心输入是监听器，预期输出是取消订阅函数；边界异常是旧 preload 缺失时该能力可能不存在。
   */
  onVisibleAreaChanged?(listener: (visibleArea: AvatarVisibleArea) => void): () => void;
  /**
   * requestVisibleArea 在挂载后主动拉取一次初始可见区，补齐首次 show 的 push 竞态。
   * 核心输入为空，预期输出是当前可见区或 null；边界异常是旧 preload 缺失时该能力可能不存在。
   */
  requestVisibleArea?(): Promise<AvatarVisibleArea | null>;
};
