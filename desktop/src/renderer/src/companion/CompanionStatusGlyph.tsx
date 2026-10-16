import {
  AudioLines, Brain, Briefcase, CircleAlert, CircleHelp, LoaderCircle,
  MessageCircle, Moon, Sparkles, type LucideIcon
} from "lucide-react";
import type { CompanionDisplayState } from "./companionStatus";

const STATUS_ICONS: Record<CompanionDisplayState, LucideIcon> = {
  syncing: LoaderCircle,
  waiting_wake: Moon,
  available: Sparkles,
  listening: AudioLines,
  thinking: Brain,
  responding: MessageCircle,
  working: Briefcase,
  waiting_user: CircleHelp,
  error: CircleAlert
};

/**
 * 为状态球和虚拟形象提供相同的状态图标，避免两处视觉含义不一致。
 *
 * @param props 当前状态及可选图标尺寸。
 * @returns 不携带文字的装饰图标；无障碍说明由外层状态区域提供。
 */
export function CompanionStatusGlyph(props: { state: CompanionDisplayState; size?: number }) {
  const Icon = STATUS_ICONS[props.state];
  return <Icon size={props.size ?? 21} strokeWidth={1.65} aria-hidden="true" />;
}
