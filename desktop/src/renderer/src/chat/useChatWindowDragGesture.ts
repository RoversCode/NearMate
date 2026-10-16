import { useRef, type MouseEvent, type PointerEvent } from "react";

type ChatDragBridge = {
  startChatDrag?(point: ChatDragPoint): void;
  moveChatDrag?(point: ChatDragPoint): void;
  endChatDrag?(): void;
};

type ChatDragPoint = {
  screenX: number;
  screenY: number;
};

type ChatDragSession = {
  startPoint: ChatDragPoint;
  isDragging: boolean;
};

type UseChatWindowDragGestureInput = {
  bridge?: ChatDragBridge | null;
  thresholdPx?: number;
  onClick(): void;
};

/**
 * useChatWindowDragGesture 识别聊天窗口标题栏的点击与拖拽手势。
 *
 * Args:
 *   input: preload bridge、拖拽阈值和普通点击回调。
 *
 * Returns:
 *   可直接挂到聊天窗口标题栏拖拽区域上的事件处理器。
 */
export function useChatWindowDragGesture(input: UseChatWindowDragGestureInput) {
  const sessionRef = useRef<ChatDragSession | null>(null);
  const suppressNextClickRef = useRef(false);
  const thresholdPx = input.thresholdPx ?? 4;

  function readPoint(event: PointerEvent<HTMLElement>): ChatDragPoint {
    return {
      screenX: event.screenX,
      screenY: event.screenY
    };
  }

  return {
    onPointerDown(event: PointerEvent<HTMLElement>) {
      if (event.button !== 0) {
        return;
      }

      sessionRef.current = {
        startPoint: readPoint(event),
        isDragging: false
      };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    onPointerMove(event: PointerEvent<HTMLElement>) {
      const session = sessionRef.current;
      if (!session) {
        return;
      }

      const currentPoint = readPoint(event);
      const deltaX = currentPoint.screenX - session.startPoint.screenX;
      const deltaY = currentPoint.screenY - session.startPoint.screenY;
      const hasCrossedThreshold = Math.hypot(deltaX, deltaY) > thresholdPx;

      if (!session.isDragging && !hasCrossedThreshold) {
        return;
      }

      if (!session.isDragging) {
        input.bridge?.startChatDrag?.(session.startPoint);
        session.isDragging = true;
        suppressNextClickRef.current = true;
      }

      input.bridge?.moveChatDrag?.(currentPoint);
      event.preventDefault();
    },
    onPointerUp(event: PointerEvent<HTMLElement>) {
      if (sessionRef.current?.isDragging) {
        input.bridge?.endChatDrag?.();
        event.preventDefault();
      }

      sessionRef.current = null;
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    },
    onPointerCancel(event: PointerEvent<HTMLElement>) {
      if (sessionRef.current?.isDragging) {
        input.bridge?.endChatDrag?.();
      }

      sessionRef.current = null;
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    },
    onClick(event: MouseEvent<HTMLElement>) {
      if (suppressNextClickRef.current) {
        suppressNextClickRef.current = false;
        event.preventDefault();
        event.stopPropagation();
        return;
      }

      input.onClick();
    }
  };
}
