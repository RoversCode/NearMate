import type { CSSProperties } from "react";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import { resolveAvatarActionRailRect } from "../avatar/avatarInteractionGeometry";
import { MessageCircle, Terminal, UserRound } from "lucide-react";

type AvatarActionRailProps = {
  visible: boolean;
  petBounds: AvatarDragRegion | null;
  onOpenChat(): void;
  onOpenModels(): void;
  onOpenConsole(): void;
};

type AvatarActionRailLayoutInput = {
  petBounds: AvatarDragRegion | null;
  viewportWidth: number;
  viewportHeight: number;
};

/**
 * resolveAvatarActionRailStyle 将动作栏锚定到人物右侧。
 *
 * Args:
 *   input: 当前人物边界与窗口尺寸。
 *
 * Returns:
 *   可直接传给动作栏的绝对定位样式。
 */
function resolveAvatarActionRailStyle(
  input: AvatarActionRailLayoutInput
): Pick<CSSProperties, "left" | "top"> {
  const rect = resolveAvatarActionRailRect(input);

  return {
    left: rect.x,
    top: rect.y
  };
}

/**
 * AvatarActionRail 渲染桌宠右侧的功能动作栏。
 *
 * Args:
 *   props: 可见性、人物边界与聊天、人物、Console 三个动作回调。
 *
 * Returns:
 *   三个圆形按钮组成的右侧动作栏。
 */
export function AvatarActionRail({
  visible,
  petBounds,
  onOpenChat,
  onOpenModels,
  onOpenConsole
}: AvatarActionRailProps) {
  if (!visible) {
    return null;
  }

  const style = resolveAvatarActionRailStyle({
    petBounds,
    viewportWidth: typeof window === "undefined" ? 66 : window.innerWidth,
    viewportHeight: typeof window === "undefined" ? 66 : window.innerHeight
  });

  return (
    <div
      className="avatar-action-rail"
      data-avatar-interactive="true"
      style={style}
      aria-label="桌宠功能栏"
    >
      <button
        className="avatar-action-button"
        data-action="chat"
        type="button"
        aria-label="打开聊天"
        title="聊天"
        onClick={onOpenChat}
      >
        <MessageCircle aria-hidden="true" size={18} strokeWidth={1.8} />
      </button>
      <button
        className="avatar-action-button"
        data-action="models"
        type="button"
        aria-label="切换人物形象"
        title="切换人物"
        onClick={onOpenModels}
      >
        <UserRound aria-hidden="true" size={18} strokeWidth={1.8} />
      </button>
      <button
        className="avatar-action-button"
        data-action="console"
        type="button"
        aria-label="打开 Console"
        title="Console"
        onClick={onOpenConsole}
      >
        <Terminal aria-hidden="true" size={18} strokeWidth={1.8} />
      </button>
    </div>
  );
}
