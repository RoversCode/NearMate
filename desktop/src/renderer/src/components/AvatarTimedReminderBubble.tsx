import type { CSSProperties } from "react";
import { BellOff, BellRing } from "lucide-react";
import type { TimedReminderCompact } from "../types/backendEvents";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import {
  calculateAssistantBubbleLayout,
  type AssistantBubbleAnchor
} from "./assistantBubbleLayout";

type AvatarTimedReminderBubbleProps = {
  reminders: readonly TimedReminderCompact[];
  petBounds: AvatarDragRegion | null;
  anchor: AssistantBubbleAnchor;
  feedbackActive?: boolean;
  requesting: boolean;
  errorMessage: string | null;
  onDismiss(): void;
};

const VIEWPORT_MARGIN = 18;
const HEAD_CLEARANCE = 26;
const BUBBLE_CHROME_HEIGHT = 92;
const REMINDER_ROW_HEIGHT = 42;
const ERROR_HEIGHT = 36;
const MAX_BUBBLE_HEIGHT = 320;

/**
 * 在桌宠头部上方展示当前全部响铃事项，并提供一次性批次关闭入口。
 *
 * Args:
 *   reminders: 当前权威快照中仍为响铃状态的事项。
 *   petBounds: 桌宠在头像窗口里的可见边界。
 *   anchor: 当前模型的头部锚点。
 *   feedbackActive: 是否正在播放本次响铃的短反馈动画。
 *   requesting: 当前批次是否正在等待 Backend 回执和后续快照。
 *   errorMessage: 最近一次关闭失败时需要保留在气泡里的提示。
 *   onDismiss: 用户点击“知道了”时执行的批次关闭动作。
 *
 * Returns:
 *   可交互的强提醒气泡；桌宠边界不可用或没有响铃事项时不渲染。
 */
export function AvatarTimedReminderBubble({
  reminders,
  petBounds,
  anchor,
  feedbackActive = false,
  requesting,
  errorMessage,
  onDismiss
}: AvatarTimedReminderBubbleProps) {
  if (!petBounds || reminders.length === 0) {
    return null;
  }

  const layout = calculateAssistantBubbleLayout({
    petBounds,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    text: reminders.map(reminder => reminder.content).join(" "),
    anchor
  });
  const desiredHeight = (
    BUBBLE_CHROME_HEIGHT
    + reminders.length * REMINDER_ROW_HEIGHT
    + (errorMessage ? ERROR_HEIGHT : 0)
  ) * layout.scale;
  const availableHeight = Math.max(0, window.innerHeight - VIEWPORT_MARGIN * 2);
  const height = Math.min(MAX_BUBBLE_HEIGHT * layout.scale, desiredHeight, availableHeight);
  const preferredTop = layout.anchorY - HEAD_CLEARANCE * layout.scale - height;
  const top = Math.max(
    VIEWPORT_MARGIN,
    Math.min(preferredTop, window.innerHeight - VIEWPORT_MARGIN - height)
  );
  const style = {
    left: `${layout.style.left}px`,
    top: `${top}px`,
    width: `${layout.style.width}px`,
    height: `${height}px`,
    "--avatar-timed-reminder-bubble-scale": layout.scale
  } as CSSProperties;

  return (
    <section
      className={feedbackActive
        ? "avatar-timed-reminder-bubble avatar-timed-reminder-bubble--feedback"
        : "avatar-timed-reminder-bubble"}
      data-avatar-interactive="true"
      data-placement={layout.placement}
      style={style}
      role="alert"
      aria-live="assertive"
      aria-label="正在提醒"
    >
      <header>
        <BellRing aria-hidden="true" size={18} />
        <strong>时间到了</strong>
      </header>
      <ul>
        {reminders.map(reminder => (
          <li key={reminder.reminder_id}>{reminder.content}</li>
        ))}
      </ul>
      {errorMessage ? <p className="avatar-timed-reminder-error">{errorMessage}</p> : null}
      <button
        type="button"
        disabled={requesting}
        onClick={onDismiss}
        title="关闭当前全部提醒"
      >
        <BellOff aria-hidden="true" size={16} />
        {requesting ? "处理中" : "知道了"}
      </button>
    </section>
  );
}
