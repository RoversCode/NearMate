import { Leaf } from "lucide-react";
import type { ChatVoiceState } from "./useChatRuntime";
import { CompanionStatusGlyph } from "../companion/CompanionStatusGlyph";
import {
  createInitialCompanionStatusState, projectCompanionStatus,
  type CompanionStatusPresentation
} from "../companion/companionStatus";

/**
 * 展示当前角色的唤醒、回复和任务状态，不承担麦克风控制。
 *
 * @param props 角色名、权威状态与保留兼容的旧语音状态。
 * @returns 同时包含状态球、明确文案和唤醒要求的陪伴状态卡。
 */
export function ChatVoiceOrb(props: {
  state: ChatVoiceState;
  companionName: string;
  companionStatus?: CompanionStatusPresentation;
}) {
  const fallbackState = createInitialCompanionStatusState();
  fallbackState.connectionStatus = "connected";
  fallbackState.legacyVoice.voiceState = props.state;
  const status = props.companionStatus ?? projectCompanionStatus(fallbackState, []);
  return (
    <section className="chat-voice-status chat-companion-status-card" aria-label="陪伴状态" aria-live="polite">
      <header className="chat-companion-status-heading">
        <h2>陪伴状态</h2>
        <span aria-hidden="true">♡</span>
      </header>
      <div className="chat-companion-status-body">
        <div className={`chat-voice-orb chat-voice-orb--${status.state}`} aria-hidden="true">
          <span className="chat-voice-orb-core"><CompanionStatusGlyph state={status.state} /></span>
          <span className="chat-voice-orb-ring" />
        </div>
        <div className="chat-voice-copy">
          <strong className="chat-voice-companion-name">{props.companionName}</strong>
          <span className="chat-voice-state" data-voice-state={status.state}>
            <i className="chat-voice-state-dot" aria-hidden="true" />
            {status.label}
          </span>
          <small>{status.wakeLabel}</small>
        </div>
      </div>
      <p className="chat-companion-status-prompt"><Leaf size={14} aria-hidden="true" /> {status.description}</p>
    </section>
  );
}
