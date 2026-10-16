/**
 * 关联持续 LiveKit 音轨的可播放事实与 Backend 的单次语音交付事件。
 */

export type PlayoutDeliveryReady = {
  delivery_id: string;
  request_id: string;
  generation_id: string;
};

export type PlayoutDeliveryAck = PlayoutDeliveryReady & {
  client_instance_id: string;
  outcome: "playing" | "failed";
  failure_code?: string;
  failure_message?: string;
};

type PlaybackFailure = {
  code: string;
  message: string;
};

type AvatarPlayoutDeliveryCoordinatorOptions = {
  clientInstanceId: string;
  reportAck(ack: PlayoutDeliveryAck): Promise<void>;
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
};

const PLAYOUT_DELIVERY_LEDGER_LIMIT = 256;

/**
 * AvatarPlayoutDeliveryCoordinator 只负责一次性交付回执，不保存业务任务状态。
 *
 * Backend 的 ready 事件可能早于或晚于 LiveKit 播放成功；本类把两种顺序归一，
 * 每个 delivery_id 最多发一次回执。回执失败不会自动重试，避免重复确认一次语音。
 */
export class AvatarPlayoutDeliveryCoordinator {
  private readonly clientInstanceId: string;
  private readonly reportAck: (ack: PlayoutDeliveryAck) => Promise<void>;
  private readonly reportDiagnostic: AvatarPlayoutDeliveryCoordinatorOptions["reportDiagnostic"];
  private readonly pendingDeliveries = new Map<string, PlayoutDeliveryReady>();
  private readonly reportedDeliveryIds = new Set<string>();
  private playbackReady = false;
  private playbackFailure: PlaybackFailure | null = null;

  constructor(options: AvatarPlayoutDeliveryCoordinatorOptions) {
    this.clientInstanceId = options.clientInstanceId;
    this.reportAck = options.reportAck;
    this.reportDiagnostic = options.reportDiagnostic;
  }

  /**
   * 标记当前助手音轨已完成 attach/play。
   */
  markPlaybackReady(): void {
    this.playbackReady = true;
    this.playbackFailure = null;
    this.flushPendingDeliveries();
  }

  /**
   * 标记当前音轨播放不可用，并为已经等待的交付发送 typed failed 回执。
   *
   * Args:
   *   failureCode: 稳定的播放错误码。
   *   failureMessage: 可展示给用户的错误说明。
   */
  markPlaybackUnavailable(failureCode: string, failureMessage: string): void {
    this.playbackReady = false;
    this.playbackFailure = {
      code: failureCode,
      message: failureMessage
    };
    this.flushPendingDeliveries();
  }

  /**
   * 接收 Backend 的单次语音交付准备事件。
   *
   * Args:
   *   delivery: Backend 已经确认首帧发布的交付身份。
   */
  handleDeliveryReady(delivery: PlayoutDeliveryReady): void {
    if (this.reportedDeliveryIds.has(delivery.delivery_id)) {
      return;
    }
    this.pendingDeliveries.set(delivery.delivery_id, delivery);
    this.trimPendingDeliveries();
    this.flushPendingDeliveries();
  }

  /**
   * 记录 Backend 已经收口的播放失败事件，避免等待或回传另一份回执。
   */
  handleDeliveryFailed(deliveryId: string): void {
    this.pendingDeliveries.delete(deliveryId);
    this.rememberReportedDelivery(deliveryId);
  }

  private flushPendingDeliveries(): void {
    if (!this.playbackReady && this.playbackFailure === null) {
      return;
    }

    const deliveries = [...this.pendingDeliveries.values()];
    this.pendingDeliveries.clear();
    for (const delivery of deliveries) {
      if (this.reportedDeliveryIds.has(delivery.delivery_id)) {
        continue;
      }
      this.rememberReportedDelivery(delivery.delivery_id);
      const failure = this.playbackFailure;
      const ack: PlayoutDeliveryAck = failure
        ? {
            ...delivery,
            client_instance_id: this.clientInstanceId,
            outcome: "failed",
            failure_code: failure.code,
            failure_message: failure.message
          }
        : {
            ...delivery,
            client_instance_id: this.clientInstanceId,
            outcome: "playing"
          };
      void this.sendAck(ack);
    }
  }

  private trimPendingDeliveries(): void {
    while (this.pendingDeliveries.size > PLAYOUT_DELIVERY_LEDGER_LIMIT) {
      const oldestDeliveryId = this.pendingDeliveries.keys().next().value as string | undefined;
      if (oldestDeliveryId === undefined) {
        return;
      }
      this.pendingDeliveries.delete(oldestDeliveryId);
      this.reportDiagnostic("warn", "avatar.playout_delivery_pending_evicted", {
        deliveryId: oldestDeliveryId,
        limit: PLAYOUT_DELIVERY_LEDGER_LIMIT
      });
    }
  }

  private rememberReportedDelivery(deliveryId: string): void {
    this.reportedDeliveryIds.add(deliveryId);
    while (this.reportedDeliveryIds.size > PLAYOUT_DELIVERY_LEDGER_LIMIT) {
      const oldestDeliveryId = this.reportedDeliveryIds.values().next().value as string | undefined;
      if (oldestDeliveryId === undefined) {
        return;
      }
      this.reportedDeliveryIds.delete(oldestDeliveryId);
      this.reportDiagnostic("warn", "avatar.playout_delivery_reported_evicted", {
        deliveryId: oldestDeliveryId,
        limit: PLAYOUT_DELIVERY_LEDGER_LIMIT
      });
    }
  }

  private async sendAck(ack: PlayoutDeliveryAck): Promise<void> {
    try {
      await this.reportAck(ack);
    } catch (error) {
      this.reportDiagnostic("warn", "avatar.playout_delivery_ack_failed", {
        deliveryId: ack.delivery_id,
        message: error instanceof Error ? error.message : "语音播放回执发送失败。"
      });
    }
  }
}
