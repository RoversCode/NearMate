import type { ShortcutFeedback } from "../../../shared/shortcutFeedback";
import type { BackendEvent } from "../types/backendEvents";
import type { PlayoutDeliveryReady } from "./playoutDeliveryCoordinator";

const REQUEST_HISTORY_LIMIT = 512;

/**
 * 在全局快捷键与 Backend 取消之间保持本地静音，旧请求不能借迟到交付恢复声音。
 */
export class ShortcutSpeechGate {
  private readonly knownRequests = new Set<string>();
  private readonly blockedRequests = new Set<string>();
  private latestSequence = 0;
  private awaitingSettlement = false;
  private muted = false;
  private pendingDeliveryRequestId: string | null = null;
  private readonly pendingDeliveries = new Map<string, PlayoutDeliveryReady>();
  private recoveryRequestId: string | null = null;
  private requiresNewUserRequest = false;
  private interruptionTime: number | null = null;
  private pendingWakeTime: number | null = null;

  /**
   * @param setMuted 同步更新共享助手音轨的静音状态。
   * @param deliverReady 把实际允许播放的交付送入回执协调器。
   */
  constructor(
    private readonly setMuted: (muted: boolean) => void,
    private readonly deliverReady: (delivery: PlayoutDeliveryReady) => void
  ) {}

  /**
   * 接收真实按键和 HTTP 回执；失败也不重播已经被用户打断的表达。
   * @param feedback 主进程校验过的本地反馈。
   * @returns 无返回值。
   */
  handleFeedback(feedback: ShortcutFeedback): void {
    if (feedback.shortcutId !== "followup_toggle") return;
    if (feedback.phase === "pressed") {
      if (feedback.sequence <= this.latestSequence) return;
      this.latestSequence = feedback.sequence;
      this.awaitingSettlement = true;
      this.recoveryRequestId = null;
      this.requiresNewUserRequest = false;
      this.interruptionTime = null;
      this.pendingWakeTime = null;
      this.pendingDeliveryRequestId = null;
      this.pendingDeliveries.clear();
      this.knownRequests.forEach(id => this.remember(this.blockedRequests, id));
      this.muted = true;
      this.setMuted(true);
      return;
    }
    feedback.interruptedRequestIds?.forEach(id => this.remember(this.blockedRequests, id));
    if (feedback.sequence !== this.latestSequence) return;
    this.awaitingSettlement = false;
    // 请求失败时没有权威取消名单，只有按键后明确提交的新用户轮可以恢复。
    // 用户可能在 HTTP 等待期间已经开口，不能丢掉这一轮的身份或待播交付。
    if (feedback.interruptedRequestIds === null) {
      this.requiresNewUserRequest = true;
      this.knownRequests.forEach(id => {
        if (id !== this.recoveryRequestId) this.remember(this.blockedRequests, id);
      });
      if (this.pendingDeliveryRequestId !== this.recoveryRequestId) this.pendingDeliveryRequestId = null;
    } else {
      this.interruptionTime = feedback.speechInterruptedAt ?? null;
      if (this.pendingWakeTime !== null && this.interruptionTime !== null
        && this.pendingWakeTime > this.interruptionTime) {
        this.muted = false;
        this.setMuted(false);
      }
    }
    this.tryResume(this.pendingDeliveryRequestId);
    for (const delivery of this.pendingDeliveries.values()) {
      if (!this.muted && !this.blockedRequests.has(delivery.request_id)) this.deliverReady(delivery);
    }
    this.pendingDeliveries.clear();
  }

  /**
   * 记录正文身份，并只允许取消边界之后的新语音交付恢复播放。
   * @param event 已通过公共事件解析器的 Backend 事件。
   * @returns 当前事件的交付是否可以上报为正在播放。
   */
  handleBackendEvent(event: BackendEvent): boolean {
    if (event.type === "wake.detected" && event.payload.detected_at !== undefined) {
      this.pendingWakeTime = Math.max(this.pendingWakeTime ?? 0, event.payload.detected_at);
      // 唤醒短答没有助手请求编号，用 Backend 同一单调时钟证明它发生在取消之后。
      if (this.muted && !this.awaitingSettlement && !this.requiresNewUserRequest
        && this.interruptionTime !== null && this.pendingWakeTime > this.interruptionTime) {
        this.muted = false;
        this.setMuted(false);
      }
    }
    const payload = event.payload as Record<string, unknown>;
    const requestId = typeof payload.request_id === "string" ? payload.request_id : null;
    // HTTP 没有确认取消时，未知的迟到语音也可能属于旧轮；只有新的用户请求能恢复。
    if (event.type === "user.final_transcript" && (this.awaitingSettlement || this.requiresNewUserRequest)) {
      const candidate = requestId ?? (
        event.payload.submission_id ? `text-input-${event.payload.submission_id}` : null
      );
      if (candidate && !this.blockedRequests.has(candidate)) this.recoveryRequestId = candidate;
    }
    if (event.type.startsWith("assistant.") && requestId) this.remember(this.knownRequests, requestId);
    if (event.type !== "assistant.playout_delivery_ready" || !requestId) return true;
    if (this.blockedRequests.has(requestId)) return false;
    if (this.requiresNewUserRequest && requestId !== this.recoveryRequestId) {
      this.remember(this.blockedRequests, requestId);
      return false;
    }
    this.pendingDeliveryRequestId = requestId;
    this.tryResume(requestId);
    if (this.muted) {
      this.pendingDeliveries.set(event.payload.delivery_id, event.payload);
      if (this.pendingDeliveries.size > REQUEST_HISTORY_LIMIT) {
        this.pendingDeliveries.delete(this.pendingDeliveries.keys().next().value!);
      }
    } else {
      this.deliverReady(event.payload);
    }
    return !this.muted;
  }

  private tryResume(requestId: string | null): void {
    if (!this.muted || this.awaitingSettlement || !requestId || this.blockedRequests.has(requestId)) return;
    this.muted = false;
    this.requiresNewUserRequest = false;
    this.setMuted(false);
  }

  private remember(target: Set<string>, requestId: string): void {
    target.add(requestId);
    if (target.size > REQUEST_HISTORY_LIMIT) target.delete(target.values().next().value!);
  }
}
