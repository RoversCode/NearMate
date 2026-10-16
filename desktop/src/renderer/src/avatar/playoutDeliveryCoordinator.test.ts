import { describe, expect, it, vi } from "vitest";
import { AvatarPlayoutDeliveryCoordinator } from "./playoutDeliveryCoordinator";

const delivery = {
  delivery_id: "123e4567-e89b-42d3-a456-426614174000",
  request_id: "request-a",
  generation_id: "generation-a"
};

function createCoordinator() {
  const reportAck = vi.fn(async () => undefined);
  const reportDiagnostic = vi.fn();
  const coordinator = new AvatarPlayoutDeliveryCoordinator({
    clientInstanceId: "123e4567-e89b-42d3-a456-426614174001",
    reportAck,
    reportDiagnostic
  });
  return { coordinator, reportAck, reportDiagnostic };
}

describe("AvatarPlayoutDeliveryCoordinator", () => {
  it("媒体先可播时，delivery 到达后立即回执 playing", async () => {
    const { coordinator, reportAck } = createCoordinator();

    coordinator.markPlaybackReady();
    coordinator.handleDeliveryReady(delivery);
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledWith({
      ...delivery,
      client_instance_id: "123e4567-e89b-42d3-a456-426614174001",
      outcome: "playing"
    });
  });

  it("delivery 先到时会等待真实 attach/play 成功再回执", async () => {
    const { coordinator, reportAck } = createCoordinator();

    coordinator.handleDeliveryReady(delivery);
    expect(reportAck).not.toHaveBeenCalled();

    coordinator.markPlaybackReady();
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledTimes(1);
    expect(reportAck).toHaveBeenCalledWith(expect.objectContaining({ outcome: "playing" }));
  });

  it("已知播放失败时会为后续 delivery 返回 typed failed 回执", async () => {
    const { coordinator, reportAck } = createCoordinator();

    coordinator.markPlaybackUnavailable("audio_play_failed", "autoplay denied");
    coordinator.handleDeliveryReady(delivery);
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledWith({
      ...delivery,
      client_instance_id: "123e4567-e89b-42d3-a456-426614174001",
      outcome: "failed",
      failure_code: "audio_play_failed",
      failure_message: "autoplay denied"
    });
  });

  it("重复 delivery 事件不会发送第二次回执", async () => {
    const { coordinator, reportAck } = createCoordinator();

    coordinator.markPlaybackReady();
    coordinator.handleDeliveryReady(delivery);
    coordinator.handleDeliveryReady(delivery);
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledTimes(1);
  });

  it("回执接口失败只进入诊断，不伪造已播放或自动重试", async () => {
    const reportAck = vi.fn(async () => {
      throw new Error("backend unavailable");
    });
    const reportDiagnostic = vi.fn();
    const coordinator = new AvatarPlayoutDeliveryCoordinator({
      clientInstanceId: "123e4567-e89b-42d3-a456-426614174001",
      reportAck,
      reportDiagnostic
    });

    coordinator.markPlaybackReady();
    coordinator.handleDeliveryReady(delivery);
    await Promise.resolve();
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledTimes(1);
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.playout_delivery_ack_failed",
      expect.objectContaining({ deliveryId: delivery.delivery_id })
    );
  });

  it("待处理 delivery 超过 256 条时淘汰最早记录并写诊断", async () => {
    const { coordinator, reportAck, reportDiagnostic } = createCoordinator();

    for (let index = 0; index < 257; index += 1) {
      coordinator.handleDeliveryReady(makeDelivery(index));
    }
    coordinator.markPlaybackReady();
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledTimes(256);
    expect(reportAck).not.toHaveBeenCalledWith(
      expect.objectContaining({ delivery_id: "delivery-0" })
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.playout_delivery_pending_evicted",
      { deliveryId: "delivery-0", limit: 256 }
    );
  });

  it("已报告账本只保留最近 256 条且保留范围内不重复回执", async () => {
    const { coordinator, reportAck, reportDiagnostic } = createCoordinator();
    coordinator.markPlaybackReady();

    for (let index = 0; index < 257; index += 1) {
      coordinator.handleDeliveryReady(makeDelivery(index));
    }
    coordinator.handleDeliveryReady(makeDelivery(256));
    await Promise.resolve();

    expect(reportAck).toHaveBeenCalledTimes(257);
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.playout_delivery_reported_evicted",
      { deliveryId: "delivery-0", limit: 256 }
    );
  });
});

function makeDelivery(index: number) {
  return {
    delivery_id: `delivery-${index}`,
    request_id: `request-${index}`,
    generation_id: `generation-${index}`
  };
}
