import type { CSSProperties } from "react";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import { resolveAvatarActionRailRect } from "../avatar/avatarInteractionGeometry";
import { MessageCircle, Sparkles, Terminal, UserRound } from "lucide-react";

type AvatarActionRailProps = {
  visible: boolean;
  petBounds: AvatarDragRegion | null;
  onOpenChat(): void;
  onOpenModels(): void;
  onOpenConsole(): void;
  onPlayReaction?(): void;
};

type AvatarActionRailLayoutInput = {
  petBounds: AvatarDragRegion | null;
  viewportWidth: number;
  viewportHeight: number;
  hasReactionButton?: boolean;
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
): Pick<CSSProperties, "left" | "top" | "height"> {
  const rect = resolveAvatarActionRailRect(input);

  return {
    left: rect.x,
    top: rect.y,
    height: rect.height
  };
}

/**
 * AvatarActionRail 渲染桌宠右侧的功能动作栏。
 *
 * Args:
 *   props: 可见性、人物边界、功能入口，以及模型支持时提供的逗一逗回调。
 *
 * Returns:
 *   三个功能按钮；支持动作的模型额外显示逗一逗按钮。
 */
export function AvatarActionRail({
  visible,
  petBounds,
  onOpenChat,
  onOpenModels,
  onOpenConsole,
  onPlayReaction
}: AvatarActionRailProps) {
  if (!visible) {
    return null;
  }

  const style = resolveAvatarActionRailStyle({
    petBounds,
    viewportWidth: typeof window === "undefined" ? 66 : window.innerWidth,
    viewportHeight: typeof window === "undefined" ? 66 : window.innerHeight,
    hasReactionButton: !!onPlayReaction
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
      {onPlayReaction ? (
        <button
          className="avatar-action-button"
          data-action="reaction"
          type="button"
          aria-label="逗一逗"
          title="逗一逗"
          onClick={onPlayReaction}
        >
          <Sparkles aria-hidden="true" size={18} strokeWidth={1.8} />
        </button>
      ) : null}
    </div>
  );
}
