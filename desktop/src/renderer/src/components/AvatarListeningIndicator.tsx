import type { AvatarDragRegion } from "../avatar/avatarDragRegion";

type AvatarListeningIndicatorProps = {
  active: boolean;
  petBounds: AvatarDragRegion | null;
};

/**
 * AvatarListeningIndicator 在人物头侧显示纯本地的聆听波形。
 *
 * Args:
 *   active: clean-mic 当前是否检测到用户正在说话。
 *   petBounds: 人物在 Avatar 窗口中的真实可见边界。
 *
 * Returns:
 *   激活且人物边界可用时返回无文字提示，否则不渲染。
 */
export function AvatarListeningIndicator({
  active,
  petBounds
}: AvatarListeningIndicatorProps) {
  if (!active || !petBounds) {
    return null;
  }

  const style = {
    left: `${Math.round(petBounds.x + petBounds.width * 0.8)}px`,
    top: `${Math.round(petBounds.y + petBounds.height * 0.16)}px`
  };

  return (
    <div
      aria-hidden="true"
      className="avatar-listening-indicator"
      data-avatar-listening="true"
      style={style}
    >
      <span />
      <span />
      <span />
    </div>
  );
}
