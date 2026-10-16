import { describe, expect, it, vi } from "vitest";
import {
  createMaterialDeliverySoundPlayer,
  readAvatarMaterialDeliveryFeedback
} from "./avatarMaterialDeliveryFeedback";

describe("avatarMaterialDeliveryFeedback", () => {
  it("接纳不带音效的处理中反馈", () => {
    expect(readAvatarMaterialDeliveryFeedback({
      operation: "add",
      kind: "progress",
      addedCount: 0,
      submittedCount: 40,
      text: "正在检查 40 项…"
    })).toEqual({
      operation: "add",
      kind: "progress",
      addedCount: 0,
      submittedCount: 40,
      text: "正在检查 40 项…"
    });
  });

  it("拒绝携带上传音效的处理中反馈", () => {
    expect(readAvatarMaterialDeliveryFeedback({
      operation: "add",
      kind: "progress",
      addedCount: 0,
      submittedCount: 1,
      text: "正在检查 1 项…",
      sound: "upload"
    })).toBeNull();
  });

  it("接纳删除成功反馈并拒绝错误的删除音效协议", () => {
    expect(readAvatarMaterialDeliveryFeedback({
      operation: "remove",
      kind: "success",
      text: "“报告.pdf”已删除",
      sound: "delete"
    })).toEqual({
      operation: "remove",
      kind: "success",
      text: "“报告.pdf”已删除",
      sound: "delete"
    });
    expect(readAvatarMaterialDeliveryFeedback({
      operation: "remove",
      kind: "success",
      text: "已删除",
      sound: "upload"
    })).toBeNull();
  });

  it("连续删除复用同一音频实例并从头播放", () => {
    const audio = {
      currentTime: 12,
      pause: vi.fn(),
      play: vi.fn(async () => undefined)
    };
    const createAudio = vi.fn(() => audio);
    const player = createMaterialDeliverySoundPlayer({
      createAudio,
      reportPlaybackError: vi.fn()
    });

    player.play("delete");
    audio.currentTime = 8;
    player.play("delete");

    expect(createAudio).toHaveBeenCalledOnce();
    expect(audio.pause).toHaveBeenCalledTimes(2);
    expect(audio.currentTime).toBe(0);
    expect(audio.play).toHaveBeenCalledTimes(2);
  });

  it("音效播放失败只上报诊断", async () => {
    const playbackError = new Error("autoplay blocked");
    const reportPlaybackError = vi.fn();
    const player = createMaterialDeliverySoundPlayer({
      createAudio: () => ({
        currentTime: 0,
        pause: vi.fn(),
        play: vi.fn(async () => { throw playbackError; })
      }),
      reportPlaybackError
    });

    player.play("delete");
    await Promise.resolve();

    expect(reportPlaybackError).toHaveBeenCalledWith(playbackError);
  });
});
