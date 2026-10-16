import type { PointerEvent } from "react";

const RESIZE_EDGES = ["n", "e", "s", "w", "ne", "nw", "se", "sw"] as const;
type ChatResizeEdge = typeof RESIZE_EDGES[number];

type ChatResizeBridge = {
  startChatResize?(input: { edge: ChatResizeEdge; screenX: number; screenY: number }): void;
  moveChatResize?(point: { screenX: number; screenY: number }): void;
  endChatResize?(): void;
};

/**
 * 渲染透明无边框聊天窗口的八方向缩放热区。
 *
 * Args:
 *   props.bridge: preload 暴露的受控缩放桥接。
 *
 * Returns:
 *   不参与视觉层级、只处理指针手势的缩放热区。
 */
export function ChatResizeHandles({ bridge }: { bridge?: ChatResizeBridge | null }) {
  function handlePointerDown(edge: ChatResizeEdge, event: PointerEvent<HTMLDivElement>): void {
    if (event.button !== 0) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    bridge?.startChatResize?.({ edge, screenX: event.screenX, screenY: event.screenY });
    event.preventDefault();
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>): void {
    if (!event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      return;
    }
    bridge?.moveChatResize?.({ screenX: event.screenX, screenY: event.screenY });
    event.preventDefault();
  }

  function handlePointerEnd(event: PointerEvent<HTMLDivElement>): void {
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    }
    bridge?.endChatResize?.();
  }

  return (
    <div className="chat-resize-handles" aria-hidden="true">
      {RESIZE_EDGES.map(edge => (
        <div
          className={`chat-resize-handle chat-resize-handle-${edge}`}
          data-edge={edge}
          key={edge}
          onPointerCancel={handlePointerEnd}
          onPointerDown={event => handlePointerDown(edge, event)}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerEnd}
        />
      ))}
    </div>
  );
}
