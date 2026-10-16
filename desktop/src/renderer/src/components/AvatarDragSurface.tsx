import { useRef } from "react";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";

/**
 * AvatarDragPoint 表示一次拖拽事件在屏幕坐标系中的位置。
 * 入参来自浏览器 PointerEvent，输出会交给 preload 和主进程计算真实窗口位移。
 * 边界：这里只保留 screenX 与 screenY，避免 renderer 参与窗口边界或显示器坐标决策。
 */
export type AvatarDragPoint = {
  screenX: number;
  screenY: number;
};

type AvatarDragSurfaceProps = {
  bounds: AvatarDragRegion | null;
  onDragStart?(point: AvatarDragPoint): void;
  onDragMove?(point: AvatarDragPoint): void;
  onDragEnd?(): void;
  onTap?(): void;
};

const TAP_MOVEMENT_THRESHOLD_PX = 4;

/**
 * AvatarDragSurface 渲染覆盖在模型可见区域上的透明拖拽层。
 * 核心入参是已换算到窗口坐标系的模型边界；当边界为空时不渲染任何节点。
 * 预期输出是一个不暴露给辅助技术的透明 div，把拖拽坐标和无明显位移的轻点意图分别交给上游。
 * 边界异常由上游几何计算负责规避；非左键输入会被忽略，避免右键或辅助按键误触移动窗口。
 */
export function AvatarDragSurface({
  bounds,
  onDragStart,
  onDragMove,
  onDragEnd,
  onTap
}: AvatarDragSurfaceProps) {
  /**
   * draggingRef 保存当前透明拖拽层是否已经接管一轮拖拽。
   * 使用 ref 而不是 state，是为了避免高频 pointermove 触发 React 重渲染影响拖拽手感。
   */
  const draggingRef = useRef(false);
  const dragStartPointRef = useRef<AvatarDragPoint | null>(null);
  const movedBeyondTapThresholdRef = useRef(false);
  const pointerTapHandledRef = useRef(false);

  /**
   * 模型尚未加载或 fallback 尚未提供可靠边界时，不创建拖拽层。
   * 这样可以避免把整个透明窗口意外变成拖拽区域，影响按钮点击和鼠标穿透预期。
   */
  if (!bounds) {
    return null;
  }

  return (
    <div
      aria-hidden="true"
      className="avatar-drag-surface"
      data-avatar-interactive="true"
      onPointerDown={event => {
        /**
         * 只允许鼠标左键发起窗口拖拽。
         * 这样可以保留未来右键菜单或其他指针手势的扩展空间，不把所有按键都解释成移动桌宠。
         */
        if (event.button !== 0) {
          return;
        }

        event.preventDefault();
        draggingRef.current = true;
        dragStartPointRef.current = { screenX: event.screenX, screenY: event.screenY };
        movedBeyondTapThresholdRef.current = false;
        pointerTapHandledRef.current = false;
        event.currentTarget.setPointerCapture?.(event.pointerId);
        onDragStart?.({ screenX: event.screenX, screenY: event.screenY });
      }}
      onPointerMove={event => {
        /**
         * 只有已经由左键按下开启的拖拽，才把移动坐标继续交给主进程。
         * 这能避免单纯悬停在模型上时持续发送窗口移动 IPC。
         */
        if (!draggingRef.current) {
          return;
        }

        const startPoint = dragStartPointRef.current;
        if (
          startPoint !== null &&
          Math.hypot(
            event.screenX - startPoint.screenX,
            event.screenY - startPoint.screenY
          ) > TAP_MOVEMENT_THRESHOLD_PX
        ) {
          movedBeyondTapThresholdRef.current = true;
        }
        onDragMove?.({ screenX: event.screenX, screenY: event.screenY });
      }}
      onPointerCancel={event => {
        /**
         * 系统取消指针序列时必须主动结束拖拽。
         * 否则窗口穿透状态可能停留在 dragging，导致后续悬停与点击表现不一致。
         */
        if (!draggingRef.current) {
          return;
        }

        draggingRef.current = false;
        dragStartPointRef.current = null;
        movedBeyondTapThresholdRef.current = false;
        pointerTapHandledRef.current = false;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
        onDragEnd?.();
      }}
      onPointerUp={event => {
        /**
         * 鼠标释放代表本轮窗口拖拽结束。
         * 这里释放指针捕获，并通知主进程把壳层状态从 dragging 收回到悬停锚点。
         */
        if (!draggingRef.current) {
          return;
        }

        const startPoint = dragStartPointRef.current;
        const releasedBeyondTapThreshold = startPoint !== null && Math.hypot(
          event.screenX - startPoint.screenX,
          event.screenY - startPoint.screenY
        ) > TAP_MOVEMENT_THRESHOLD_PX;
        movedBeyondTapThresholdRef.current ||= releasedBeyondTapThreshold;
        const shouldTriggerTap = !movedBeyondTapThresholdRef.current;
        draggingRef.current = false;
        dragStartPointRef.current = null;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
        onDragEnd?.();
        if (shouldTriggerTap) {
          pointerTapHandledRef.current = true;
          onTap?.();
        }
      }}
      onClick={() => {
        // Electron 自动化和部分辅助输入只产生 click；真实指针链路已处理时必须去重，避免面板被连续切换两次。
        if (pointerTapHandledRef.current) {
          pointerTapHandledRef.current = false;
          return;
        }
        if (movedBeyondTapThresholdRef.current) {
          movedBeyondTapThresholdRef.current = false;
          return;
        }
        onTap?.();
      }}
      style={{
        left: bounds.x,
        top: bounds.y,
        width: bounds.width,
        height: bounds.height
      }}
    />
  );
}
