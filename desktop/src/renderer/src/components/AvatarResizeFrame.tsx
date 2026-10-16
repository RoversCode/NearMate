import { useRef } from "react";
import type { AvatarDragPoint } from "./AvatarDragSurface";
import type { AvatarResizeDirection } from "../../../shared/live2dModelTypes";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import { resolveAvatarResizeFrameRect } from "../avatar/avatarInteractionGeometry";

type AvatarResizeFrameProps = {
  bounds: AvatarDragRegion | null;
  visible: boolean;
  onResizeStart?(direction: AvatarResizeDirection, point: AvatarDragPoint): void;
  onResizeMove?(point: AvatarDragPoint): void;
  onResizeEnd?(): void;
};

/**
 * RESIZE_DIRECTIONS 定义桌宠窗口允许交互的八方向缩放热区。
 * 这里使用稳定字面量数组驱动渲染，避免 JSX 中手写重复节点时方向名漂移。
 */
const RESIZE_DIRECTIONS: AvatarResizeDirection[] = [
  "n",
  "s",
  "e",
  "w",
  "ne",
  "nw",
  "se",
  "sw"
];

/**
 * AvatarResizeFrame 负责渲染围绕人物边界的缩放提示框与热区。
 * 核心入参是人物边界、可见性和缩放回调；输出是一组可交互但不直接触达主进程的透明 handle。
 * 边界异常：人物边界缺失时不渲染；非左键不会开启缩放；pointerup 与 pointercancel 都会统一收口，避免一次会话悬挂。
 */
export function AvatarResizeFrame({
  bounds,
  visible,
  onResizeStart,
  onResizeMove,
  onResizeEnd
}: AvatarResizeFrameProps) {
  /**
   * activePointerIdRef 保存当前已接管的那根指针，避免其他指针事件误闯入同一轮缩放会话。
   */
  const activePointerIdRef = useRef<number | null>(null);

  /**
   * activeDirectionRef 保存本轮缩放的方向，确保后续 move 与 end 只服务于同一轮已开始的会话。
   */
  const activeDirectionRef = useRef<AvatarResizeDirection | null>(null);
  const frameRect = resolveAvatarResizeFrameRect(bounds);

  /**
   * finishResize 统一处理正常结束与系统取消两种收口路径。
   * 这样可以把状态清理与回调触发放在同一个出口，避免遗漏某一条指针生命周期分支。
   */
  const finishResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (activePointerIdRef.current !== event.pointerId) {
      return;
    }

    activePointerIdRef.current = null;
    activeDirectionRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    onResizeEnd?.();
  };

  if (!frameRect) {
    return null;
  }

  return (
    <div
      aria-hidden="true"
      className="avatar-resize-frame"
      data-visible={visible ? "true" : "false"}
      style={{
        left: frameRect.x,
        top: frameRect.y,
        width: frameRect.width,
        height: frameRect.height
      }}
    >
      {RESIZE_DIRECTIONS.map(direction => (
        <div
          key={direction}
          aria-hidden="true"
          className={`avatar-resize-handle avatar-resize-handle-${direction}`}
          data-avatar-interactive="true"
          data-resize-direction={direction}
          onPointerDown={event => {
            /**
             * 只允许鼠标左键发起缩放。
             * 这样可以保留右键菜单、辅助键或未来手势扩展空间，不把所有按键都解释成窗口缩放。
             */
            if (event.button !== 0) {
              return;
            }

            event.preventDefault();
            activePointerIdRef.current = event.pointerId;
            activeDirectionRef.current = direction;
            event.currentTarget.setPointerCapture?.(event.pointerId);
            onResizeStart?.(direction, {
              screenX: event.screenX,
              screenY: event.screenY
            });
          }}
          onPointerMove={event => {
            /**
             * 只有已经由当前 handle 成功接管的那轮缩放，才持续上报屏幕坐标。
             * 这样能避免单纯悬停或其他指针序列造成无意义的 resize IPC。
             */
            if (
              activePointerIdRef.current !== event.pointerId ||
              activeDirectionRef.current !== direction
            ) {
              return;
            }

            onResizeMove?.({
              screenX: event.screenX,
              screenY: event.screenY
            });
          }}
          onPointerUp={event => {
            /**
             * 指针正常释放表示本轮缩放结束，需要和取消路径使用同一套收口逻辑。
             */
            finishResize(event);
          }}
          onPointerCancel={event => {
            /**
             * 浏览器或系统取消指针序列时，也必须主动结束缩放。
             * 否则主进程可能误以为窗口仍处于持续缩放态。
             */
            finishResize(event);
          }}
        />
      ))}
    </div>
  );
}
