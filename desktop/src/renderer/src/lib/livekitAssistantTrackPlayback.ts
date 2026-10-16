import {
  AssistantAudioPlayback,
  type AssistantAudioPlaybackSink
} from "./assistantAudioPlayback";
import { getLiveKitRoomErrorReason } from "./livekitRoomErrors";
import type { LiveKitRoomHandlers } from "./livekitRoomTypes";

/**
 * LiveKitAssistantTrackPlayback 管理助手远端音轨播放资源。
 */
export class LiveKitAssistantTrackPlayback {
  private assistantAudioPlayback: AssistantAudioPlaybackSink | null;
  private hasActiveAssistantAudioTrack = false;
  private playbackAttempt = 0;

  /**
   * 初始化助手音轨播放管理器。
   *
   * Args:
   *   assistantAudioPlayback: 测试注入或外部复用的播放 sink。
   */
  constructor(assistantAudioPlayback: AssistantAudioPlaybackSink | null = null) {
    this.assistantAudioPlayback = assistantAudioPlayback;
  }

  /**
   * playTrack 播放 backend 发布的助手远端音频轨。
   *
   * @param track LiveKit TrackSubscribed 事件中的远端音轨。
   * @param handlers 用于报告当前音轨播放状态的回调。
   * @returns 当前播放尝试完成后的 Promise；旧尝试的迟到结果直接吸收。
   */
  async playTrack(track: unknown, handlers: LiveKitRoomHandlers): Promise<void> {
    const attempt = ++this.playbackAttempt;
    const playback = this.ensurePlayback();
    // 新音轨到达前只释放已经接管过的旧播放资源，避免首条音轨产生无意义 release。
    if (this.hasActiveAssistantAudioTrack) {
      playback.release();
      this.hasActiveAssistantAudioTrack = false;
    }

    try {
      // playTrack 可能异步等待浏览器 play；一旦交给播放 sink，就视为当前连接已接管该音轨。
      this.hasActiveAssistantAudioTrack = true;
      await playback.playTrack(track);
      if (attempt !== this.playbackAttempt) return;
      handlers.onAssistantPlaybackReady?.();
    } catch (error) {
      // 被取消或换轨后的 Promise 不再拥有播放器，不能清理已接管的新音轨。
      if (attempt !== this.playbackAttempt) return;
      playback.release();
      this.hasActiveAssistantAudioTrack = false;
      const message = getLiveKitRoomErrorReason(error, "助手音频播放失败。");
      handlers.onAssistantPlaybackUnavailable?.("audio_play_failed", message);
      handlers.onStatusChange("failed", message);
    }
  }

  /**
   * 释放当前助手音频播放资源，并让在途 play 结果失效。
   * @returns 无返回值。
   */
  release(): void {
    this.playbackAttempt += 1;
    if (!this.hasActiveAssistantAudioTrack) {
      return;
    }

    this.assistantAudioPlayback?.release();
    this.hasActiveAssistantAudioTrack = false;
  }

  private ensurePlayback(): AssistantAudioPlaybackSink {
    if (this.assistantAudioPlayback === null) {
      this.assistantAudioPlayback = new AssistantAudioPlayback();
    }

    return this.assistantAudioPlayback;
  }
}
