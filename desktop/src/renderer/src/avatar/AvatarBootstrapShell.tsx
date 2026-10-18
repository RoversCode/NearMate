import { useEffect, useState } from "react";
import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import type { AvatarBootstrapBubbleMode } from "../components/AvatarBootstrapBubble";
import { AvatarBootstrapBubble } from "../components/AvatarBootstrapBubble";
import { AvatarDragSurface } from "../components/AvatarDragSurface";
import { AvatarResizeFrame } from "../components/AvatarResizeFrame";
import { AvatarStage } from "../components/AvatarStage";
import type { CompanionAvatarBridge } from "./avatarBridge";
import { useAvatarPetSurface } from "./useAvatarPetSurface";

type BootstrapAvatarBridge = Pick<
  CompanionAvatarBridge,
  | "applyPresentationProfile"
  | "endDrag"
  | "endResize"
  | "hoverEnter"
  | "hoverLeave"
  | "moveDrag"
  | "moveResize"
  | "onVisibleAreaChanged"
  | "openConsole"
  | "reportDiagnostic"
  | "reportPetBounds"
  | "requestVisibleArea"
  | "runtime"
  | "startDrag"
  | "startResize"
>;

type AvatarBootstrapShellProps = {
  avatarBridge?: BootstrapAvatarBridge;
  mode: AvatarBootstrapBubbleMode;
  message?: string;
  onRetry(): void;
};

/**
 * 显示初始化期间的桌宠外壳与资源进度，只保留展示、移动和控制台入口。
 *
 * Args:
 *   avatarBridge: 桌宠展示与窗口操作的 preload bridge。
 *   mode: 当前初始化阻断模式。
 *   message: Backend 确认的资源进度；缺失时显示配置引导。
 *   onRetry: 重新读取本地服务状态。
 * Returns:
 *   桌宠舞台、拖拽缩放能力和持久气泡；不启动 control socket、麦克风或业务运行时。
 */
export function AvatarBootstrapShell({
  avatarBridge,
  mode,
  message,
  onRetry
}: AvatarBootstrapShellProps) {
  const modelCatalog = avatarBridge?.runtime.live2d.catalog ?? [];
  const fallbackModelPath = avatarBridge?.runtime.live2d.defaultModelPath ?? "";
  /**
   * defaultModelId 只用于应用人物展示档案。
   * 即使没有匹配 id，AvatarStage 仍可以用 fallbackModelPath 加载资源，主进程也会用默认尺寸兜底显示窗口。
   */
  const defaultModelId =
    modelCatalog.find((catalogEntry) => catalogEntry.modelPath === fallbackModelPath)?.id ??
    modelCatalog[0]?.id ??
    null;
  const presentationProfile = getAvatarPresentationProfile(defaultModelId ?? undefined);
  const [bubbleHeight, setBubbleHeight] = useState(132);
  const petSurface = useAvatarPetSurface({
    avatarBridge,
    controlsVisible: false,
    measureBubbleHeight: () => bubbleHeight,
    presentationProfile,
    resizeFrameInteractiveWhenHidden: true
  });

  useEffect(() => {
    /**
     * 如果当前默认人物能在目录中定位，就尽早把展示档案交给主进程。
     * 业务意图是让未初始化阶段也按真实桌宠尺寸出现，而不是先闪出一个普通面板尺寸的窗口。
     */
    if (!defaultModelId) {
      return;
    }

    avatarBridge?.applyPresentationProfile(defaultModelId);
  }, [avatarBridge, defaultModelId]);

  return (
    <main
      className="avatar-shell"
      data-window="avatar"
      onMouseEnter={petSurface.syncInteractionFromPointerEvent}
      onMouseMove={petSurface.syncInteractionFromPointerEvent}
      onMouseLeave={() => {
        /**
         * 鼠标离开桌宠窗口后恢复穿透，避免 bootstrap 壳长期占用透明窗口区域。
         */
        petSurface.setBridgeInteractionHeld(false);
      }}
    >
      <AvatarStage
        diagnosticsBridge={avatarBridge}
        modelPath={fallbackModelPath || null}
        presentationProfile={presentationProfile}
        assistantStateLabel="待初始化"
        controlStatusLabel="配置完成前暂停"
        mediaStatusLabel="能力未启动"
        mode="pet"
        onModelReady={() => true}
        onAvatarTap={() => {
          /**
           * bootstrap 未完成时点击桌宠不打开能力面板。
           * 业务意图是只保留 console 引导入口，避免用户进入一个尚不可用的控制流程。
           */
        }}
        onPetBoundsChange={petSurface.updatePetBounds}
      />
      <AvatarResizeFrame
        bounds={petSurface.petBounds}
        visible={true}
        onResizeStart={petSurface.resizeHandlers.onResizeStart}
        onResizeMove={petSurface.resizeHandlers.onResizeMove}
        onResizeEnd={petSurface.resizeHandlers.onResizeEnd}
      />
      <AvatarDragSurface
        bounds={petSurface.avatarInteractionHotspotBounds}
        onDragStart={petSurface.dragHandlers.onDragStart}
        onDragMove={petSurface.dragHandlers.onDragMove}
        onDragEnd={petSurface.dragHandlers.onDragEnd}
      />
      <AvatarBootstrapBubble
        mode={mode}
        message={message}
        onOpenConsole={
          avatarBridge?.openConsole
            ? () => avatarBridge.openConsole?.({ section: "settings" })
            : undefined
        }
        onRetry={onRetry}
        onHeightChange={setBubbleHeight}
        style={petSurface.bottomBubbleStyle}
      />
    </main>
  );
}
