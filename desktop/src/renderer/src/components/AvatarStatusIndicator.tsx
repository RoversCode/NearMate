import { useEffect, useRef, useState } from "react";
import type { AvatarVisibleArea } from "../../../shared/live2dModelTypes";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import type { CompanionStatusPresentation } from "../companion/companionStatus";
import { CompanionStatusGlyph } from "../companion/CompanionStatusGlyph";

/**
 * 提供状态图案所需的当前角色状态和人物可见位置。
 *
 * Args:
 *   status: 后端事实投影出的角色状态、唤醒模式和手动切换版本。
 *   petBounds: 人物在窗口中的真实边界；尚未加载时不显示图案。
 *   visibleArea: 窗口在屏幕工作区内仍可见的范围。
 * Returns:
 *   不适用；这是状态图案组件的输入。
 */
export type AvatarStatusIndicatorProps = {
  status: CompanionStatusPresentation;
  petBounds: AvatarDragRegion | null;
  visibleArea?: AvatarVisibleArea | null;
};

const STATUS_SAFE_INSET = 24;
const MANUAL_TOGGLE_EMPHASIS_MS = 2000;

/**
 * 在人物头侧显示没有文字、不会挡住桌面点击的小光环和状态图标。
 *
 * Args:
 *   props: 当前角色状态、人物边界和屏幕可见区域。
 * Returns:
 *   可见区足够时返回状态图案；人物或可见空间不足时不渲染。
 */
export function AvatarStatusIndicator({ status, petBounds, visibleArea }: AvatarStatusIndicatorProps) {
  const [emphasized, setEmphasized] = useState(false);
  const previousToggleRevision = useRef<number | null>(null);
  const previousInputRuntimeId = useRef<string | undefined>(undefined);
  const hasHydratedWakeMode = useRef(false);
  const emphasisTimeoutId = useRef<number | null>(null);

  useEffect(() => {
    const previousRevision = previousToggleRevision.current;
    const nextRevision = status.manualToggleRevision;
    const hasRuntimeChanged = previousInputRuntimeId.current !== status.inputRuntimeId;
    const wasHydrated = hasHydratedWakeMode.current;
    // 同一版本轴内的重复或旧回执不重放反馈，也不能降低已确认的版本基线。
    if (wasHydrated && status.wakeMode !== "unknown" && nextRevision !== null
      && !hasRuntimeChanged && previousRevision !== null && nextRevision <= previousRevision) return;

    previousToggleRevision.current = nextRevision;
    previousInputRuntimeId.current = status.inputRuntimeId;
    hasHydratedWakeMode.current = status.wakeMode !== "unknown";

    // 首帧和连接重置只建立基线，不能把历史切换反馈带进新连接。
    if (!wasHydrated || status.wakeMode === "unknown" || nextRevision === null) {
      if (emphasisTimeoutId.current !== null) window.clearTimeout(emphasisTimeoutId.current);
      emphasisTimeoutId.current = null;
      setEmphasized(false);
      return;
    }
    // 自动唤醒或冷却只更新图案，确认光环仍从本次手动回执起完整保留两秒。
    if (emphasisTimeoutId.current !== null) window.clearTimeout(emphasisTimeoutId.current);
    setEmphasized(true);
    emphasisTimeoutId.current = window.setTimeout(() => {
      emphasisTimeoutId.current = null;
      setEmphasized(false);
    }, MANUAL_TOGGLE_EMPHASIS_MS);
  }, [status.inputRuntimeId, status.manualToggleRevision, status.wakeMode]);

  useEffect(() => () => {
    if (emphasisTimeoutId.current !== null) window.clearTimeout(emphasisTimeoutId.current);
  }, []);

  if (!petBounds) return null;

  const visibleLeft = Math.max(0, visibleArea?.visibleLeft ?? 0);
  const visibleTop = Math.max(0, visibleArea?.visibleTop ?? 0);
  const visibleRight = Math.min(window.innerWidth, visibleArea?.visibleRight ?? window.innerWidth);
  const visibleBottom = Math.min(window.innerHeight, visibleArea?.visibleBottom ?? window.innerHeight);

  // 窗口可以被用户拖到屏幕外；空间不足时收起图案，避免把残缺光环钉在屏幕边缘。
  if (visibleRight - visibleLeft < STATUS_SAFE_INSET * 2 || visibleBottom - visibleTop < STATUS_SAFE_INSET * 2) {
    return null;
  }

  const left = Math.min(
    visibleRight - STATUS_SAFE_INSET,
    Math.max(visibleLeft + STATUS_SAFE_INSET, petBounds.x + petBounds.width * 0.8)
  );
  // 现有本地聆听波形在头侧 0.16 高度处，图案向下留出一格，二者各自表达真实来源。
  const top = Math.min(
    visibleBottom - STATUS_SAFE_INSET,
    Math.max(visibleTop + STATUS_SAFE_INSET, petBounds.y + petBounds.height * 0.16 + 38)
  );

  return (
    <div
      role="img"
      aria-label={`${status.label}。${status.description}。${status.wakeLabel}`}
      className={`avatar-status-indicator${emphasized ? " avatar-status-indicator--emphasized" : ""}`}
      data-avatar-status={status.state}
      data-avatar-wake-mode={status.wakeMode}
      style={{ left: `${Math.round(left)}px`, top: `${Math.round(top)}px` }}
    >
      {/* 连续按键时只重建装饰光环，让每次新回执都从头播放确认动画。 */}
      <span
        key={`${status.inputRuntimeId ?? ""}:${status.manualToggleRevision ?? ""}`}
        className="avatar-status-halo"
        aria-hidden="true"
      />
      <span className="avatar-status-core" aria-hidden="true">
        <CompanionStatusGlyph state={status.state} size={16} />
      </span>
      <span className="avatar-status-wake-dot" aria-hidden="true" />
    </div>
  );
}
